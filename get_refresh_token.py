import os
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv

load_dotenv()

sp_oauth = SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope="playlist-read-private playlist-read-collaborative",
    open_browser=True
)

token_info = sp_oauth.get_access_token(as_dict=True)
print("\n=== COPY YOUR REFRESH TOKEN ===")
print(token_info['refresh_token'])
print("===============================\n")