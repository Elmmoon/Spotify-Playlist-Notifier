import os
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
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
RAW_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "")

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
    auth_manager.refresh_access_token(refresh_token)

sp = spotipy.Spotify(auth_manager=auth_manager)

# --- Globals & Buffer ---
seen_track_ids = set()
last_snapshot_id = None
song_queue = asyncio.Queue() # Unbounded buffer

# --- Blocking Spotify API Helper Functions ---
def fetch_playlist_snapshot_blocking():
    try:
        playlist_meta = sp.playlist(SPOTIFY_PLAYLIST_ID, fields="snapshot_id")
        return playlist_meta.get("snapshot_id")
    except Exception as e:
        logging.error(f"Spotify API Metadata Error: {e}")
        return None

def fetch_all_playlist_tracks_blocking():
    all_items = []
    try:
        results = sp.playlist_items(SPOTIFY_PLAYLIST_ID)
        while results:
            all_items.extend(results.get("items", []))
            if results.get("next"):
                results = sp.next(results)
            else:
                break
    except Exception as e:
        logging.error(f"Spotify API Tracks Error: {e}")
    return all_items

# --- Async Wrappers ---
async def get_playlist_snapshot():
    return await asyncio.to_thread(fetch_playlist_snapshot_blocking)

async def get_playlist_tracks():
    return await asyncio.to_thread(fetch_all_playlist_tracks_blocking)


# --- CONSUMER: Discord Pusher (Zero-Overhead loop) ---
async def discord_pusher():
    await bot.wait_until_ready()
    logging.info("Discord pusher task started and waiting for songs...")
    
    while not bot.is_closed():
        # Suspends entirely until the Producer adds a song to the queue
        track = await song_queue.get()
        
        channel = bot.get_channel(CHANNEL_ID)
        if not channel:
            logging.warning("Discord channel not found. Retrying in 10s...")
            await song_queue.put(track)
            await asyncio.sleep(10)
            continue
            
        try:
            track_name = track.get("name", "Unknown Title")
            artists = track.get("artists", [])
            artist_name = artists[0]["name"] if artists else "Unknown Artist"
            track_url = track.get("external_urls", {}).get("spotify", "")
            images = track.get("album", {}).get("images", [])
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
            logging.error(f"Consumer error pushing to Discord: {e}")
            await song_queue.put(track)
            
        finally:
            song_queue.task_done()
            # Enforce strict pacing to completely avoid Discord rate limits
            await asyncio.sleep(2.5)


# --- PRODUCER: Spotify Watcher ---
@tasks.loop(minutes=2)
async def check_playlist_changes():
    global last_snapshot_id
    logging.info("Checking Spotify playlist for changes...")

    try:
        current_snapshot = await get_playlist_snapshot()
        
        if not current_snapshot:
            logging.warning("Could not retrieve current snapshot ID. Skipping this cycle.")
            return

        if current_snapshot == last_snapshot_id:
            logging.info("No changes detected (Snapshot ID matches). Skipping full track scan.")
            return

        logging.info("Playlist change detected via snapshot ID! Fetching updated tracks...")
        items = await get_playlist_tracks()
        new_tracks_found = 0

        for item in items:
            track = item.get("track") or item.get("item")
            if not track or not track.get("id"):
                continue

            track_id = track["id"]

            if track_id not in seen_track_ids:
                seen_track_ids.add(track_id)
                new_tracks_found += 1
                
                # Push the track object into the queue for the Consumer
                await song_queue.put(track)

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
intents.message_content = True

class SpotifyBot(commands.Bot):
    async def setup_hook(self):
        """
        setup_hook runs exactly once during bot startup.
        It is immune to the WebSocket reconnection issues of on_ready.
        """
        global last_snapshot_id
        logging.info("Running initialization and pre-loading data...")
        
        # 1. Pre-load existing tracks so we don't spam the channel on startup
        try:
            last_snapshot_id = await get_playlist_snapshot()
            initial_items = await get_playlist_tracks()
            
            for item in initial_items:
                track = item.get("track") or item.get("item")
                if track and track.get("id"):
                    seen_track_ids.add(track["id"])
                    
            logging.info(f"Successfully pre-loaded {len(seen_track_ids)} existing tracks (Snapshot: {last_snapshot_id}).")
        except Exception as e:
            logging.error(f"Error during initial track load: {e}")

        # 2. Start the Consumer background task
        self.loop.create_task(discord_pusher())

        # 3. Start the Producer loop
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