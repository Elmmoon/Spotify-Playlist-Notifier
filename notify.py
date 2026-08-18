import os
import sys
import logging
import asyncio
from threading import Thread
import discord
from discord.ext import commands, tasks
from flask import Flask
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('discord').setLevel(logging.WARNING)

app = Flask("")

@app.route('/')
def home():
    logging.info("Received HTTP ping from cron-job")
    return "Bot is online!"

def run_webserver():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_webserver)
    t.daemon = True
    t.start()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RAW_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
RAW_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "")
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")

# Fail fast on bad config instead of crashing deep in library code
if not DISCORD_TOKEN:
    logging.error("DISCORD_TOKEN is not set.")
    sys.exit(1)

if not RAW_CHANNEL_ID:
    logging.error("DISCORD_CHANNEL_ID is not set.")
    sys.exit(1)

try:
    CHANNEL_ID = int(RAW_CHANNEL_ID)
except ValueError:
    logging.error(f"DISCORD_CHANNEL_ID must be an integer, got: {RAW_CHANNEL_ID!r}")
    sys.exit(1)

if not RAW_PLAYLIST_ID:
    logging.error("SPOTIFY_PLAYLIST_ID is not set.")
    sys.exit(1)

if not SPOTIPY_CLIENT_ID:
    logging.error("SPOTIPY_CLIENT_ID is not set.")
    sys.exit(1)

if not SPOTIPY_CLIENT_SECRET:
    logging.error("SPOTIPY_CLIENT_SECRET is not set.")
    sys.exit(1)

if not SPOTIPY_REDIRECT_URI:
    logging.error("SPOTIPY_REDIRECT_URI is not set.")
    sys.exit(1)

def extract_playlist_id(raw_id: str) -> str:
    if "playlist/" in raw_id:
        return raw_id.split("playlist/")[1].split("?")[0]
    return raw_id

SPOTIFY_PLAYLIST_ID = extract_playlist_id(RAW_PLAYLIST_ID)

auth_manager = SpotifyOAuth(
    client_id=SPOTIPY_CLIENT_ID,
    client_secret=SPOTIPY_CLIENT_SECRET,
    redirect_uri=SPOTIPY_REDIRECT_URI,
    scope="playlist-read-private playlist-read-collaborative",
    open_browser=False
)

refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN")
if refresh_token:
    try:
        auth_manager.refresh_access_token(refresh_token)
    except Exception as e:
        logging.error(f"Failed to refresh Spotify access token: {e}")
        sys.exit(1)

sp = spotipy.Spotify(auth_manager=auth_manager)

# --- State ---
seen_track_ids = set()
last_snapshot_id = None
preload_complete = False
consecutive_preload_failures = 0

MAX_SEND_RETRIES = 3
MAX_CHANNEL_RETRIES = 3
SEND_RETRY_BASE_DELAY = 0.5
SEND_RETRY_MAX_DELAY = 4
SEND_PUSH_DELAY = 0.1
PRELOAD_FAILURE_ALERT_THRESHOLD = 10
preload_failure_alerted = False

song_queue = asyncio.Queue()  # {"track": <dict>, "retries": <int>}

# --- Helpers ---
def extract_track(item: dict):
    """Return the track from common playlist item shapes."""
    return item.get("track") or item.get("item")

# --- Spotify calls ---
def fetch_playlist_snapshot_blocking():
    try:
        playlist_meta = sp.playlist(SPOTIFY_PLAYLIST_ID, fields="snapshot_id")
        return playlist_meta.get("snapshot_id")
    except Exception as e:
        logging.error(f"Spotify API Metadata Error: {e}")
        return None

def fetch_all_playlist_tracks_blocking():
    """Return None when the fetch fails."""
    all_items = []
    try:
        results = sp.playlist_items(SPOTIFY_PLAYLIST_ID)
        while results:
            all_items.extend(results.get("items", []))
            if results.get("next"):
                results = sp.next(results)
            else:
                break
        return all_items
    except Exception as e:
        logging.error(f"Spotify API Tracks Error: {e}")
        return None

# --- Async wrappers ---
async def get_playlist_snapshot():
    return await asyncio.to_thread(fetch_playlist_snapshot_blocking)

async def get_playlist_tracks():
    return await asyncio.to_thread(fetch_all_playlist_tracks_blocking)


async def preload_playlist_state() -> bool:
    """Seed existing playlist state without sending anything to Discord."""
    global last_snapshot_id

    items = await get_playlist_tracks()
    snapshot = await get_playlist_snapshot()

    if snapshot is None or items is None:
        logging.warning("Playlist preload failed.")
        return False

    last_snapshot_id = snapshot
    for item in items:
        track = extract_track(item)
        if track and track.get("id"):
            seen_track_ids.add(track["id"])

    logging.info(
        f"Pre-load successful: {len(seen_track_ids)} existing tracks seeded "
        f"(snapshot={last_snapshot_id})."
    )
    return True


# --- Discord consumer ---
async def discord_pusher():
    await bot.wait_until_ready()
    logging.info("Discord pusher started.")

    while not bot.is_closed():
        item = await song_queue.get()
        track = item["track"]
        retries = item.get("retries", 0)

        try:
            channel = bot.get_channel(CHANNEL_ID)
            if not channel:
                raise RuntimeError(f"Discord channel {CHANNEL_ID} not found")

            track_name = track.get("name") or "Unknown Title"
            artists = track.get("artists") or []
            artist_name = artists[0]["name"] if artists else "Unknown Artist"
            track_url = (track.get("external_urls") or {}).get("spotify", "")
            images = (track.get("album") or {}).get("images") or []
            thumbnail_url = images[0]["url"] if images else None

            embed = discord.Embed(
                title="🎶 New Song Added to Playlist!",
                description=f"**[{track_name}]({track_url})**\nby **{artist_name}**",
                color=discord.Color.green()
            )
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)

            await channel.send(embed=embed)
            logging.info(f"Pushed to Discord: '{track_name}'")

        except (discord.Forbidden, discord.NotFound) as e:
            track_id = track.get("id", "unknown")
            logging.error(f"Permanent Discord error for track {track_id}; dropping: {e}")

        except Exception as e:
            if retries < MAX_SEND_RETRIES:
                delay = min(SEND_RETRY_BASE_DELAY * (2 ** retries), SEND_RETRY_MAX_DELAY)
                logging.warning(
                    f"Discord send failed for '{track.get('name', 'Unknown Title')}' "
                    f"(attempt {retries + 1}/{MAX_SEND_RETRIES}); retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)
                await song_queue.put({"track": track, "retries": retries + 1})
            else:
                track_id = track.get("id", "unknown")
                logging.error(f"Dropping track {track_id} after {MAX_SEND_RETRIES} failed attempts: {e}")

        song_queue.task_done()
        await asyncio.sleep(SEND_PUSH_DELAY)

# --- PRODUCER: Spotify watcher ---
@tasks.loop(minutes=2)
async def check_playlist_changes():
    global last_snapshot_id, preload_complete, consecutive_preload_failures, preload_failure_alerted
    logging.info(
        f"Checking playlist [tracks={len(seen_track_ids)}, queue={song_queue.qsize()}, "
        f"snapshot={last_snapshot_id}]"
    )

    if not preload_complete:
        logging.warning("Playlist preload incomplete; retrying.")
        preload_complete = await preload_playlist_state()

        if preload_complete:
            consecutive_preload_failures = 0
            preload_failure_alerted = False
        else:
            consecutive_preload_failures += 1
            logging.warning(f"Playlist preload failed ({consecutive_preload_failures} consecutive).")
            if (
                consecutive_preload_failures >= PRELOAD_FAILURE_ALERT_THRESHOLD
                and not preload_failure_alerted
            ):
                preload_failure_alerted = True
                channel = bot.get_channel(CHANNEL_ID)
                if channel:
                    try:
                        await channel.send(
                            "⚠️ I can't load the Spotify playlist right now. "
                            "Please check the playlist ID and Spotify credentials."
                        )
                    except Exception as e:
                        logging.error(f"Failed to send preload alert: {e}")
        return

    try:
        current_snapshot = await get_playlist_snapshot()

        if not current_snapshot:
            logging.warning("Could not retrieve current snapshot ID. Skipping this cycle.")
            return

        if current_snapshot == last_snapshot_id:
            logging.info(f"No changes detected (snapshot still {current_snapshot}). Skipping full track scan.")
            return

        logging.info(
            f"Playlist change detected (snapshot {last_snapshot_id} -> {current_snapshot})! "
            f"Fetching updated tracks..."
        )
        items = await get_playlist_tracks()

        if items is None:
            logging.warning("Track fetch failed; keeping the current snapshot.")
            return

        new_tracks_found = 0

        for item in items:
            track = extract_track(item)
            if not track or not track.get("id"):
                continue

            track_id = track["id"]

            if track_id not in seen_track_ids:
                seen_track_ids.add(track_id)
                new_tracks_found += 1
                await song_queue.put({"track": track, "retries": 0})

        last_snapshot_id = current_snapshot

        if new_tracks_found > 0:
            logging.info(f"Queued {new_tracks_found} new tracks.")
        else:
            logging.info("Snapshot changed; no new tracks found.")

    except Exception as e:
        logging.error(f"Error checking Spotify playlist loop: {e}")

@check_playlist_changes.before_loop
async def before_check():
    await bot.wait_until_ready()


# --- Discord bot setup ---
intents = discord.Intents.default()

MAX_PRELOAD_ATTEMPTS = 2
PRELOAD_RETRY_DELAY_SECONDS = 10

class SpotifyBot(commands.Bot):
    async def setup_hook(self):
        global preload_complete
        logging.info("Running initialization and pre-loading data...")

        for attempt in range(1, MAX_PRELOAD_ATTEMPTS + 1):
            preload_complete = await preload_playlist_state()
            if preload_complete:
                break
            logging.warning(f"Preload attempt {attempt}/{MAX_PRELOAD_ATTEMPTS} failed.")
            if attempt < MAX_PRELOAD_ATTEMPTS:
                await asyncio.sleep(PRELOAD_RETRY_DELAY_SECONDS)

        if not preload_complete:
            logging.warning("Preload incomplete; poll loop will keep retrying.")

        self.loop.create_task(discord_pusher())
        check_playlist_changes.start()

bot = SpotifyBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info(f"Bot successfully connected to Discord as {bot.user.name} ({bot.user.id})")


if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)