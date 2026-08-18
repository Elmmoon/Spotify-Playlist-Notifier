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

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Suppress noisy HTTP logs from libraries
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('discord').setLevel(logging.WARNING)

# --- Flask Keep-Alive Web Server ---
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

# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RAW_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
RAW_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "")

# --- Fail fast with clear errors instead of cryptic crashes deep in library code ---
if not DISCORD_TOKEN:
    logging.error("DISCORD_TOKEN is not set. Set it in your environment or .env file.")
    sys.exit(1)

if not RAW_CHANNEL_ID:
    logging.error("DISCORD_CHANNEL_ID is not set. Set it in your environment or .env file.")
    sys.exit(1)

try:
    CHANNEL_ID = int(RAW_CHANNEL_ID)
except ValueError:
    logging.error(f"DISCORD_CHANNEL_ID must be an integer, got: {RAW_CHANNEL_ID!r}")
    sys.exit(1)

def extract_playlist_id(raw_id: str) -> str:
    if "playlist/" in raw_id:
        return raw_id.split("playlist/")[1].split("?")[0]
    return raw_id

SPOTIFY_PLAYLIST_ID = extract_playlist_id(RAW_PLAYLIST_ID)

# --- Spotify API Setup ---
auth_manager = SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope="playlist-read-private playlist-read-collaborative",
    open_browser=False
)

refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN")
if refresh_token:
    try:
        auth_manager.refresh_access_token(refresh_token)
    except Exception as e:
        logging.error(f"Failed to refresh Spotify access token at startup: {e}")
        logging.error("Check that SPOTIFY_REFRESH_TOKEN is valid and not revoked.")
        sys.exit(1)

sp = spotipy.Spotify(auth_manager=auth_manager)

# --- Globals & Buffer ---
seen_track_ids = set()
last_snapshot_id = None
song_queue = asyncio.Queue()  # Unbounded buffer of {"track": <dict>, "retries": <int>}
preload_complete = False  # becomes True once initial playlist state is seeded

MAX_SEND_RETRIES = 3  # cap on re-queueing a track after a send failure
MAX_CHANNEL_RETRIES = 3  # cap on re-queueing a track if the channel is unreachable

# --- Blocking Spotify API Helper Functions ---
def fetch_playlist_snapshot_blocking():
    try:
        playlist_meta = sp.playlist(SPOTIFY_PLAYLIST_ID, fields="snapshot_id")
        return playlist_meta.get("snapshot_id")
    except Exception as e:
        logging.error(f"Spotify API Metadata Error: {e}")
        return None

def fetch_all_playlist_tracks_blocking():
    """Returns the full list of playlist items, or None if the fetch failed
    partway through pagination. Returning None (rather than the partial list
    gathered so far) lets callers distinguish "failed fetch" from "genuinely
    empty playlist" and avoid treating a truncated read as complete."""
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

# --- Async Wrappers ---
async def get_playlist_snapshot():
    return await asyncio.to_thread(fetch_playlist_snapshot_blocking)

async def get_playlist_tracks():
    return await asyncio.to_thread(fetch_all_playlist_tracks_blocking)


async def preload_playlist_state() -> bool:
    """Seed last_snapshot_id and seen_track_ids from the current playlist
    WITHOUT queueing anything for Discord (this is a silent baseline, not a
    diff). Returns True on success, False on failure (e.g. rate-limited by
    Spotify) so the caller can retry later instead of treating it as fatal."""
    global last_snapshot_id

    snapshot = await get_playlist_snapshot()
    items = await get_playlist_tracks()

    if snapshot is None or items is None:
        logging.warning("Pre-load fetch failed (snapshot or tracks unavailable).")
        return False

    last_snapshot_id = snapshot
    for item in items:
        track = item.get("track")
        if track and track.get("id"):
            seen_track_ids.add(track["id"])

    logging.info(
        f"Pre-load successful: {len(seen_track_ids)} existing tracks seeded "
        f"(snapshot={last_snapshot_id})."
    )
    return True


# --- CONSUMER: Discord Pusher (Zero-Overhead loop) ---
async def discord_pusher():
    await bot.wait_until_ready()
    logging.info("Discord pusher task started and waiting for songs...")

    while not bot.is_closed():
        # Suspends entirely until the Producer adds a song to the queue
        item = await song_queue.get()
        track = item["track"]
        retries = item.get("retries", 0)

        channel = bot.get_channel(CHANNEL_ID)
        if not channel:
            # discord.py's cache is kept in sync by gateway events (channel
            # deletes, the bot being kicked, etc.), so a cache miss here is a
            # real, not stale, signal -- no need for an extra fetch_channel()
            # API call. Cap retries so a permanently invalid channel (deleted,
            # bot kicked) can't loop forever and silently swallow every track.
            if retries < MAX_CHANNEL_RETRIES:
                logging.warning(
                    f"Discord channel {CHANNEL_ID} not found "
                    f"(attempt {retries + 1}/{MAX_CHANNEL_RETRIES}). Retrying in 10s..."
                )
                await song_queue.put({"track": track, "retries": retries + 1})
            else:
                logging.error(
                    f"Channel {CHANNEL_ID} still unreachable after {MAX_CHANNEL_RETRIES} "
                    f"attempts. Dropping track '{track.get('name', 'Unknown Title')}'."
                )
            song_queue.task_done()
            await asyncio.sleep(10)
            continue

        try:
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

        except Exception as e:
            if retries < MAX_SEND_RETRIES:
                logging.error(
                    f"Consumer error pushing track (attempt {retries + 1}/{MAX_SEND_RETRIES}): {e}"
                )
                await song_queue.put({"track": track, "retries": retries + 1})
            else:
                # Give up on this track rather than jamming the queue forever.
                track_id = track.get("id", "unknown")
                logging.error(
                    f"Dropping track {track_id} after {MAX_SEND_RETRIES} failed attempts: {e}"
                )

        finally:
            song_queue.task_done()
            # Enforce strict pacing to completely avoid Discord rate limits
            await asyncio.sleep(2.5)


# --- PRODUCER: Spotify Watcher ---
@tasks.loop(minutes=2)
async def check_playlist_changes():
    global last_snapshot_id, preload_complete
    logging.info(
        f"Checking Spotify playlist for changes... "
        f"[tracks_known={len(seen_track_ids)}, queue_size={song_queue.qsize()}, "
        f"last_snapshot={last_snapshot_id}]"
    )

    # If initial pre-load never succeeded at startup (e.g. Spotify rate-limited
    # us), retry it here on the normal 2-minute cadence instead of giving up.
    # This is a silent seed, not a diff, so it must NOT fall through into the
    # change-detection logic below on the same cycle (that would compare
    # against the snapshot we just set and correctly find "no changes" anyway,
    # but returning early keeps the two code paths cleanly separated).
    if not preload_complete:
        logging.warning("Initial pre-load has not completed yet. Retrying now...")
        preload_complete = await preload_playlist_state()
        if not preload_complete:
            logging.warning(
                "Pre-load still failing (likely Spotify rate-limiting). "
                "Will retry again on the next 2-minute cycle."
            )
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
            # Fetch failed (possibly partway through pagination). Do NOT advance
            # last_snapshot_id here, or a truncated read would be treated as
            # complete and any tracks past the failure point would be lost forever.
            logging.warning("Track fetch failed; skipping this cycle without updating snapshot.")
            return

        new_tracks_found = 0

        for item in items:
            track = item.get("track")
            if not track or not track.get("id"):
                continue

            track_id = track["id"]

            if track_id not in seen_track_ids:
                seen_track_ids.add(track_id)
                new_tracks_found += 1

                # Push the track object into the queue for the Consumer
                await song_queue.put({"track": track, "retries": 0})

        last_snapshot_id = current_snapshot

        if new_tracks_found > 0:
            logging.info(f"Producer pushed {new_tracks_found} new tracks into the buffer.")
        else:
            logging.info("Snapshot changed, but no new unique tracks were added.")

    except Exception as e:
        logging.error(f"Error checking Spotify playlist loop: {e}")

@check_playlist_changes.before_loop
async def before_check():
    await bot.wait_until_ready()


# --- Discord Bot Setup & Initialization ---
intents = discord.Intents.default()
# Note: message_content is a privileged intent and isn't used anywhere in this
# bot (no on_message handler, no prefix commands read message text), so it's
# left disabled to avoid requesting privileges the bot doesn't need.

MAX_PRELOAD_ATTEMPTS = 5
PRELOAD_RETRY_DELAY_SECONDS = 15

class SpotifyBot(commands.Bot):
    async def setup_hook(self):
        """
        setup_hook runs exactly once during bot startup.
        It is immune to the WebSocket reconnection issues of on_ready.
        """
        global preload_complete
        logging.info("Running initialization and pre-loading data...")

        # 1. Try to pre-load existing tracks so we don't spam the channel on
        #    startup. A short burst of retries here covers quick transient
        #    errors, but if Spotify is still unreachable (e.g. we're
        #    rate-limited) we do NOT block startup or shut the bot down --
        #    check_playlist_changes will keep retrying pre-load every 2
        #    minutes until it succeeds.
        for attempt in range(1, MAX_PRELOAD_ATTEMPTS + 1):
            preload_complete = await preload_playlist_state()
            if preload_complete:
                break
            logging.warning(f"Pre-load attempt {attempt}/{MAX_PRELOAD_ATTEMPTS} failed.")
            if attempt < MAX_PRELOAD_ATTEMPTS:
                await asyncio.sleep(PRELOAD_RETRY_DELAY_SECONDS)

        if not preload_complete:
            logging.warning(
                "Could not pre-load playlist state at startup (likely rate-limited "
                "by Spotify). Bot will keep running and retry pre-load automatically "
                "every 2 minutes via the normal poll cycle."
            )

        # 2. Start the Consumer background task
        self.loop.create_task(discord_pusher())

        # 3. Start the Producer loop regardless of pre-load outcome -- it
        #    will retry pre-load itself on its first tick(s) if needed.
        check_playlist_changes.start()

# Instantiate the bot using our custom class
bot = SpotifyBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # on_ready is now purely for letting us know it successfully connected/reconnected
    logging.info(f"Bot successfully connected to Discord as {bot.user.name} ({bot.user.id})")


if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)