import os
from threading import Thread
import discord
from discord.ext import commands, tasks
from flask import Flask
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv

load_dotenv()

# --- Flask Keep-Alive Web Server ---
app = Flask("")


@app.route("/")
def home():
    return "Bot is online!"


def run_webserver():
    # Render assigns dynamic PORT numbers via environment variable
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run_webserver)
    t.daemon = True
    t.start()


# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
RAW_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "")

# Extract clean 22-character Spotify ID if a full URL was provided
def extract_playlist_id(raw_id: str) -> str:
    if "spotify.com/playlist/" in raw_id:
        return raw_id.split("playlist/")[1].split("?")[0]
    return raw_id

SPOTIFY_PLAYLIST_ID = extract_playlist_id(RAW_PLAYLIST_ID)

# Spotify API Setup (Read-Only Scopes)
cache_file_path = os.path.join(os.path.dirname(__file__), ".cache")

auth_manager = SpotifyClientCredentials(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
)
sp = spotipy.Spotify(auth_manager=auth_manager)

# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Set to store track IDs that have already been posted
seen_track_ids = set()


def get_playlist_tracks():
    """Fetches all tracks reliably using playlist_items and handles pagination."""
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
        print(f"[Spotify API Error]: {e}")
    return all_items


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    
    # Pre-load existing tracks on startup so it doesn't post old songs
    try:
        initial_items = get_playlist_tracks()
        for item in initial_items:
            track = item.get("track") or item.get("item")
            if track and track.get("id"):
                seen_track_ids.add(track["id"])
        print(f"Pre-loaded {len(seen_track_ids)} existing tracks from playlist.")
    except Exception as e:
        print(f"Error during initial track load: {e}")

    # Start loop task
    if not check_playlist_changes.is_running():
        check_playlist_changes.start()


@tasks.loop(minutes=1)
async def check_playlist_changes():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    try:
        items = get_playlist_tracks()
        
        for item in items:
            track = item.get("track") or item.get("item")
            if not track or not track.get("id"):
                continue

            track_id = track["id"]

            if track_id not in seen_track_ids:
                seen_track_ids.add(track_id)

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
                print(f"Posted new track: {track_name} by {artist_name}")

    except Exception as e:
        print(f"Error checking Spotify playlist: {e}")


@check_playlist_changes.before_loop
async def before_check():
    await bot.wait_until_ready()


if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)