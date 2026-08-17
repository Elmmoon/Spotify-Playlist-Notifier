"""
backfill_playlist_from_channel.py

One-off / on-demand utility that scans the full message history of a Discord
channel for Spotify track links (both plain open.spotify.com URLs and
spotify:track:... URIs, including ones embedded inside Discord's own link
embeds) and adds any tracks that aren't already in the target playlist.

This is meant to run *once* (or whenever you want to "catch up" the playlist
with links people have posted), not as a long-running bot like notify.py.

--------------------------------------------------------------------------
Spotify auth:
This script is meant to be run locally (not headless on Render like
notify.py), so it handles Spotify auth itself via spotipy's built-in
token cache:

- First run: spotipy opens a browser tab for you to approve the
  playlist-modify scopes, then caches the resulting access/refresh token
  to a local file (SPOTIPY_CACHE_PATH, default ".cache-backfill").
- Every subsequent run: spotipy reads that cache file and silently
  refreshes the access token as needed - no browser, no manual token
  copy/paste, no env var required.

This uses a separate cache file from notify.py/get_refresh_token.py (and
requests a wider scope, including playlist-modify-*), so it won't clobber
or depend on the refresh token notify.py uses in production.

Note: SPOTIPY_REDIRECT_URI must be a URI you can actually reach from the
machine you're running this on (e.g. http://127.0.0.1:8080/callback), and
it must be registered in your Spotify app's dashboard.
--------------------------------------------------------------------------

IMPORTANT - Discord side:
- The bot needs the "Message Content Intent" enabled in the Discord
  Developer Portal (same as notify.py already requires).
- The bot needs "Read Message History" and "View Channel" permissions in
  the target channel.

Usage:
    python backfill_playlist_from_channel.py
"""

import os
import re
import logging

import discord
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
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
RAW_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "")

# Where spotipy caches this script's own OAuth token (separate from
# notify.py's token/refresh-token handling).
SPOTIPY_CACHE_PATH = os.getenv("SPOTIPY_CACHE_PATH", ".cache-backfill")


def extract_playlist_id(raw_id: str) -> str:
    if "spotify.com/playlist/" in raw_id:
        return raw_id.split("playlist/")[1].split("?")[0]
    return raw_id


SPOTIFY_PLAYLIST_ID = extract_playlist_id(RAW_PLAYLIST_ID)

# Matches open.spotify.com/track/<id>, open.spotify.com/intl-xx/track/<id>,
# and spotify:track:<id>. Spotify track IDs are 22 base62 characters.
SPOTIFY_TRACK_REGEX = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?track/|spotify:track:)([A-Za-z0-9]{22})"
)

# --- Spotify API Setup ---
# open_browser=True (the default) is intentional here: this script runs
# locally, so spotipy can pop a browser tab on first run and then rely on
# its own cache file for every run after that.
auth_manager = SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope="playlist-modify-public playlist-modify-private playlist-read-private playlist-read-collaborative",
    cache_path=SPOTIPY_CACHE_PATH,
)

sp = spotipy.Spotify(auth_manager=auth_manager)

# --- Discord Client Setup ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def extract_track_ids(text: str):
    if not text:
        return []
    return SPOTIFY_TRACK_REGEX.findall(text)


def get_existing_track_ids(playlist_id: str) -> set:
    """Fetch all track IDs currently in the playlist so we don't add duplicates."""
    existing = set()
    results = sp.playlist_items(playlist_id, fields="items.track.id,next")
    while results:
        for item in results.get("items", []):
            track = item.get("track")
            if track and track.get("id"):
                existing.add(track["id"])
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    return existing


def add_tracks_in_batches(playlist_id: str, track_ids: list, batch_size: int = 100):
    """Spotify's add-items endpoint accepts at most 100 URIs per call."""
    for i in range(0, len(track_ids), batch_size):
        batch = track_ids[i:i + batch_size]
        uris = [f"spotify:track:{tid}" for tid in batch]
        sp.playlist_add_items(playlist_id, uris)
        logging.info(f"Added batch of {len(batch)} track(s) to playlist.")


@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user}")

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except Exception as e:
            logging.error(f"Could not access channel {CHANNEL_ID}: {e}")
            await client.close()
            return

    logging.info("Scanning full channel history for Spotify track links... (this may take a while)")

    found_ids = set()
    message_count = 0

    async for message in channel.history(limit=None, oldest_first=True):
        message_count += 1

        # Links typed directly in the message content
        found_ids.update(extract_track_ids(message.content))

        # Links Discord auto-embedded (title/url/description often repeat the link)
        for embed in message.embeds:
            if embed.url:
                found_ids.update(extract_track_ids(embed.url))
            if embed.description:
                found_ids.update(extract_track_ids(embed.description))

        if message_count % 500 == 0:
            logging.info(f"...scanned {message_count} messages so far, {len(found_ids)} unique tracks found")

    logging.info(f"Finished scanning {message_count} messages. Found {len(found_ids)} unique Spotify track link(s).")

    if not found_ids:
        logging.info("No Spotify track links found in this channel. Nothing to do.")
        await client.close()
        return

    logging.info("Fetching current playlist contents to skip duplicates...")
    try:
        existing_ids = get_existing_track_ids(SPOTIFY_PLAYLIST_ID)
    except Exception as e:
        logging.error(f"Failed to fetch existing playlist tracks: {e}")
        await client.close()
        return

    new_ids = [tid for tid in found_ids if tid not in existing_ids]
    logging.info(
        f"{len(new_ids)} new track(s) to add "
        f"(skipping {len(found_ids) - len(new_ids)} already in the playlist)."
    )

    if new_ids:
        try:
            add_tracks_in_batches(SPOTIFY_PLAYLIST_ID, new_ids)
            logging.info(f"Done. Added {len(new_ids)} track(s) to the playlist.")
        except Exception as e:
            logging.error(f"Error adding tracks to playlist: {e}")
    else:
        logging.info("Nothing new to add.")

    await client.close()


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)