# 🎶 Spotify Discord Notifier

A lightweight, automated Discord bot that monitors a Spotify playlist in real time and posts a formatted notification message to a designated Discord channel whenever new tracks are added. Built using **Python**, **discord.py**, **spotipy**, and **Flask**.

Deployed on **Render** (as a free Web Service) and kept online 24/7 using **cron-job.org**.

Also includes a standalone **backfill script** that scans a channel's entire message history for Spotify track links and adds any it finds straight to the playlist — handy for catching the playlist up on links people posted before the bot existed.

---

## 📋 Features

- 🎧 **Live Playlist Monitoring:** Periodically checks Spotify playlists for newly added songs.
- 💬 **Rich Discord Embeds:** Automatically posts track details including song title, artist name, direct Spotify link, and album artwork.
- ⚡ **24/7 Uptime (Set & Forget):** Runs on Render with a lightweight background Flask endpoint pinged every 5 minutes by `cron-job.org` to prevent idle sleeping.
- 🔐 **Headless OAuth Authentication:** Utilizes a persistent Spotify Refresh Token so server deployments never require manual browser authentication or interactive terminal input.
- 🔎 **Channel Backfill:** One-off script that scans full channel history for Spotify track links (including auto-embeds) and adds any missing tracks to the playlist, skipping ones already there.

---

## 🛠️ Prerequisites

Before getting started, make sure you have:

1. **Python 3.10+** installed on your local machine.
2. A **Discord Developer Account** with a registered Bot application & token ([Discord Developer Portal](https://discord.com/developers/applications)).
3. A **Spotify Developer Account** with a registered App ([Spotify Developer Dashboard](https://developer.spotify.com/dashboard)).
4. A **GitHub Account** to host your code repository.
5. Free accounts on [Render](https://render.com) and [cron-job.org](https://cron-job.org).

---

## 📁 Repository Structure

```text
.
├── notify.py                          # Main bot application (Discord bot + Flask keep-alive webserver)
├── backfill_playlist_from_channel.py  # One-off script: scans channel history for Spotify links and adds them to the playlist
├── get_refresh_token.py               # One-time helper script to generate Spotify Refresh Token
├── requirements.txt                   # Python dependencies
├── Procfile                           # Web server process configuration for cloud host
├── .env.example                       # Example environment variables template
└── README.md                          # Documentation
```

---

## ⚙️ Configuration & Environment Variables

| Variable Name | Description | Example |
| :--- | :--- | :--- |
| `DISCORD_TOKEN` | Discord Bot application secret token | `MTUzODkxNT...` |
| `DISCORD_CHANNEL_ID` | Numerical ID of the Discord channel for updates | `123456789012345678` |
| `SPOTIFY_PLAYLIST_ID` | Spotify Playlist ID or full open URL | `https://open.spotify.com/playlist/0FM1...` |
| `SPOTIPY_CLIENT_ID` | Spotify App Client ID | `e878b520806e46a...` |
| `SPOTIPY_CLIENT_SECRET` | Spotify App Client Secret | `a891b93e70fe17...` |
| `SPOTIPY_REDIRECT_URI` | Authorized Redirect URI configured in Spotify Dashboard | `http://127.0.0.1:8080/callback` |
| `SPOTIFY_REFRESH_TOKEN` | Stored long-lived Spotify OAuth refresh token (used by `notify.py`, read-only scopes) | `AQB3x8Z...` |
| `SPOTIPY_CACHE_PATH` | *(Optional)* Cache file path used by `backfill_playlist_from_channel.py` for its own OAuth token | `.cache-backfill` |
| `PORT` | Web server port (automatically populated on Render) | `8080` |

---

## 🚀 Setup & Deployment Guide

### Step 1: Spotify Developer Application Setup

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in.
2. Click **Create an App**.
3. Fill in the App Name and Description, then click **Edit Settings**.
4. Add `http://127.0.0.1:8080/callback` under **Redirect URIs** and click **Save**.
5. Copy your **Client ID** and **Client Secret**.

---

### Step 2: Generate Spotify Refresh Token (Local)

Because server deployments (like Render) run in a headless cloud environment without a web browser, generate a permanent Refresh Token locally first:

1. Clone your project repository locally:
   ```bash
   git clone https://github.com/Elmmoon/Spotify-Playlist-Notifier.git
   cd Spotify-Playlist-Notifier
   ```
2. Install local dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file locally with your credentials:
   ```env
   SPOTIPY_CLIENT_ID=your_spotify_client_id
   SPOTIPY_CLIENT_SECRET=your_spotify_client_secret
   SPOTIPY_REDIRECT_URI=http://127.0.0.1:8080/callback
   ```
4. Run the helper script to authenticate via your web browser:
   ```bash
   python get_refresh_token.py
   ```
5. Authorize the application in the browser tab that opens.
6. Copy the **Refresh Token** printed in your terminal window.

---

### Step 3: Deploy to Render

1. Push your project code to a **GitHub repository** (ensure `.env` and `__pycache__` are listed in `.gitignore`).
2. Log in to [Render](https://render.com) and click **New +** → **Web Service**.
3. Connect your GitHub repository.
4. Configure service settings:
   - **Name:** `spotify-playlist-notifier`
   - **Environment:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python notify.py`
   - **Instance Type:** `Free`
5. Click **Environment** under your service settings and add all required keys:
   - `DISCORD_TOKEN`
   - `DISCORD_CHANNEL_ID`
   - `SPOTIFY_PLAYLIST_ID`
   - `SPOTIPY_CLIENT_ID`
   - `SPOTIPY_CLIENT_SECRET`
   - `SPOTIPY_REDIRECT_URI` (`http://127.0.0.1:8080/callback`)
   - `SPOTIFY_REFRESH_TOKEN` (the token generated in Step 2)
6. Click **Deploy Web Service**.
7. Once deployment finishes, copy your live Web Service URL (e.g., `https://spotify-playlist-notifier.onrender.com`).

---

### Step 4: Keep Service Awake via cron-job.org

Free instances on Render spin down after 15 minutes of inbound HTTP inactivity. Set up a free external pinging job to keep the service running 24/7:

1. Log in to [cron-job.org](https://cron-job.org/).
2. Navigate to **Console** → **Cron Jobs** → **Create Cron Job**.
3. Configure the job settings:
   - **Title:** `Spotify Discord Bot Keep-Alive`
   - **Address (URL):** `https://spotify-playlist-notifier.onrender.com`
   - **Execution Schedule:** Every `5 minutes`
   - **Request Method:** `GET`
4. Click **Create**.
5. Click **Test run** next to the created job to confirm it receives a `200 OK` response with `"Bot is online!"`.

---

## 🔐 How Spotify Authentication Works

Because cloud hosting platforms (like Render) operate in "headless" environments without a browser, `notify.py` uses a persistent **Two-Phase OAuth 2.0 Flow**.

### Phase 1: Local Authorization (One-Time Setup)
1. Running `get_refresh_token.py` locally triggers a browser redirect to Spotify.
2. Upon approval, Spotify provides an authorization code which is exchanged for:
   - **Access Token:** Valid for 60 minutes.
   - **Refresh Token:** Permanent credential.

### Phase 2: Cloud Execution
1. The bot runs on Render with `open_browser=False`.
2. It uses the `SPOTIFY_REFRESH_TOKEN` from your environment variables to authenticate.
3. Whenever the 60-minute Access Token expires, `spotipy` automatically uses the Refresh Token to fetch a brand new Access Token in the background without requiring user intervention.

`backfill_playlist_from_channel.py` works differently since it's meant to be run locally/interactively rather than deployed headlessly: it lets `spotipy` manage its own OAuth flow and token cache directly (see next section), so there's no manual refresh-token copy/paste step for it.

---

## 🔁 Backfilling the Playlist from Channel History

`backfill_playlist_from_channel.py` is a standalone, run-it-when-you-need-it script (not a long-running bot) that:

1. Connects to Discord and reads the **entire** message history of `DISCORD_CHANNEL_ID`.
2. Extracts Spotify track links from both raw message text and Discord's auto-generated link embeds — matches `open.spotify.com/track/<id>` (including localized `intl-xx` URLs) and `spotify:track:<id>` URIs.
3. Fetches the playlist's current contents so it can skip tracks that are already there.
4. Adds all newly found tracks to `SPOTIFY_PLAYLIST_ID` in batches of 100 (Spotify's per-request limit).

### Spotify Auth for the Backfill Script

Adding tracks requires write scopes (`playlist-modify-public playlist-modify-private`), which the read-only `SPOTIFY_REFRESH_TOKEN` used by `notify.py` doesn't have. Rather than juggling a second refresh token by hand, this script handles its own auth:

- **First run:** since it's run locally, `spotipy` opens a browser tab for you to log in and approve the modify scopes.
- **Every run after that:** `spotipy` reads its cached token from `SPOTIPY_CACHE_PATH` (default `.cache-backfill`) and silently refreshes it as needed — no browser, no manual token copying.

This cache file is separate from anything `notify.py`/`get_refresh_token.py` use, so it won't interfere with your production bot's credentials. Make sure `.cache-backfill` (or whatever path you set) is in `.gitignore` — it contains a live OAuth token.

### Requirements

- The bot needs the **Message Content Intent** enabled in the Discord Developer Portal (same requirement as `notify.py`).
- The bot needs **View Channel** and **Read Message History** permissions in the target channel.
- `SPOTIPY_REDIRECT_URI` must be reachable from the machine running the script and registered in your Spotify app's dashboard.

### Usage

```bash
python backfill_playlist_from_channel.py
```

Expected log output:
```text
2026-08-17 22:10:03 [INFO] Logged in as Spotify Notifier#1234
2026-08-17 22:10:03 [INFO] Scanning full channel history for Spotify track links... (this may take a while)
2026-08-17 22:10:41 [INFO] Finished scanning 1287 messages. Found 46 unique Spotify track link(s).
2026-08-17 22:10:42 [INFO] Fetching current playlist contents to skip duplicates...
2026-08-17 22:10:43 [INFO] 12 new track(s) to add (skipping 34 already in the playlist).
2026-08-17 22:10:44 [INFO] Added batch of 12 track(s) to playlist.
2026-08-17 22:10:44 [INFO] Done. Added 12 track(s) to the playlist.
```

For very large channels, scanning the full history can take a while — the script logs progress every 500 messages so you can see it's still working.

---

## 🛠️ Local Development & Testing

To test the bot script locally before pushing updates:

```bash
python notify.py
```

Expected log output:
```text
2026-08-17 18:08:55 [INFO] Bot connected to Discord as Spotify Notifier (1538915272424169614)
2026-08-17 18:08:56 [INFO] Successfully pre-loaded 8 existing tracks.
2026-08-17 18:09:56 [INFO] Checking Spotify playlist for changes...
2026-08-17 18:09:56 [INFO] No new tracks found.
```

---

## 📄 License

This project is open-source and available under the [MIT License](LICENSE).