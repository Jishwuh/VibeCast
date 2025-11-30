VibeCast
=================

Features
- Slash command search with YouTube autocomplete (YouTube Data API v3).
- Plays audio from YouTube, Spotify (tracks resolved via YouTube), and SoundCloud using yt-dlp + FFmpeg.
- Per-guild queue, basic playback controls, and role-gated admin actions.

Prerequisites
- Python 3.10+
- FFmpeg installed and on PATH.
- Discord bot token.
- YouTube Data API key (for autocomplete).
- Optional: Spotify Client ID/Secret for better Spotify track metadata.

Setup
1) Create and activate a virtual environment:
   python -m venv .venv
   .\.venv\Scripts\activate
2) Install dependencies:
   pip install -r requirements.txt
3) Fill config.json with your tokens/keys. You can also store secrets in a .env file (DISCORD_TOKEN, YOUTUBE_API_KEY, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET) and leave config values blank to prefer env.
4) Run the bot:
   python bot.py

Getting a YouTube API key
1) Go to Google Cloud Console: https://console.cloud.google.com/
2) Create/select a project.
3) Enable the YouTube Data API v3 (APIs & Services -> Library -> search “YouTube Data API v3” -> Enable).
4) Create credentials (APIs & Services -> Credentials -> Create Credentials -> API key).
5) Copy the API key and place it in config.json (youtube_api_key) or .env (YOUTUBE_API_KEY).

Getting Spotify API credentials
1) Go to https://developer.spotify.com/dashboard/ and log in.
2) Create an app; give it a name and description.
3) Open the app and copy the Client ID and Client Secret.
4) Put them in config.json (spotify_client_id/spotify_client_secret) or .env (SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET).
5) Ensure enable_spotify is true in config.json if you want Spotify queries resolved.

Commands (slash only)
- /join, /leave, /play <query|url>, /pause, /resume, /skip, /stop, /queue, /nowplaying, /volume <0-100>, /clear
- /search: Slash command with autocomplete suggestions from YouTube; selecting a result will play it.

Notes
- Queues are in-memory per guild. Bot disconnects after idle periods.
- Admin-only commands are restricted to roles in allowed_roles from config.json. If none are set, the caller is allowed.
- Lavalink is not used; audio streams directly via yt-dlp + FFmpeg.
