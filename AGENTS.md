# AGENTS.md — Developer & AI Agent Guidelines

This document provides system architecture, design decisions, and strict operational guidelines for AI coding assistants and developers working on the **Navidrome Telegram Bot** project.

---

## 🎯 1. Project Mission & Architecture

The Navidrome Telegram Bot is an automated companion service for self-hosted [Navidrome](https://www.navidrome.org/) and Subsonic music servers.

### Key Capabilities:
1. **Multi-Platform Music Fetching**: Downloads tracks, albums, and playlists from Spotify (`spotdl`), YouTube / YouTube Music (`yt-dlp`), and Amazon Music (via headless Playwright Chromium API interception).
2. **Clean Studio Audio Engine**: Replaces noisy YouTube music videos and fan edits with pure studio releases via YouTube Music (`ytmusicapi`).
3. **In-Chat Audio Previews**: Generates fast 30-second audio preview snippets with waveforms so users can hear songs inside Telegram before downloading.
4. **Album & Playlist Multi-Track Downloader**: Automatically detects album/playlist searches, downloads every track as a distinct, tagged `.mp3` file, and generates `.m3u` playlists — strictly prohibiting 1-hour jukebox files.
5. **Two-Tier Storage Lifecycle**:
   - `downloads/` (Staging): Temporary staging folder for new downloads, exploratory playlists, and automated discoveries.
   - `library/` (Permanent): Permanent music collection.
   - **Auto-Promotion**: Any track starred (❤️) or rated 4–5⭐ in Navidrome moves from `downloads/` to `library/`, and `.m3u` relative references are updated seamlessly.
6. **Daily Personalized Discovery**: Background scheduler and `/discover` command that analyzes Navidrome listening history (SQLite DB) and fetches 1 fresh recommendation per day into `Daily Discovery.m3u`.
7. **Storage Cleanup & Space Management**: Detects 1⭐ rated tracks, tracks in `Delete` or `Trash` playlists, or tracks unplayed for >14 days in `downloads/`. Sends interactive Telegram confirmation cards before deleting.
8. **Modern Telegram UI**: Persistent Reply Keyboard, native slash command menu, and inline action buttons.

---

## 🎵 2. Audio Content & Search Rules (CRITICAL)

The user has strict requirements regarding audio quality and presentation:

### Rule 1: NEVER Download or Recommend Modified Music Videos
- **Strictly Disallowed**: No music videos with spoken film dialogue, actor intros, sound effects, or movie cutscenes.
- **Strictly Filtered Out**:
  - `(Official Music Video)`, `(Official Video)`, `(Video Song)`, `(Full Video)`
  - `Lofi`, `Lo-Fi Flip`, `Slowed + Reverb`, `Slowed and Reverb`, `8D Audio`
  - `Nightcore`, `Sped Up`, `Speed Up`, `Bass Boosted`
  - `Teaser`, `Trailer`, `Promo`, `Reaction`, `Status`, `Shorts`
  - `Jukebox`, `Audio Jukebox`, `Full Album`, `All Songs`, `Non Stop`, `Compilation`
- **Allowed Exception**: Only include modified versions if the user *explicitly* included that keyword in their search query (e.g. user typed `"kesariya lofi"`).

### Rule 2: Pure Studio Audio Sources & Duration Limits
- Primary search and discovery MUST query **YouTube Music** (`ytmusicapi` with `filter="songs"` and `get_watch_playlist`).
- YouTube Music serves authentic studio recordings provided directly by record labels (Sony Music, T-Series, Warner, Universal, etc.) and artist `- Topic` channels.
- **Duration Limits for Single Songs**:
  - Minimum: `45 seconds` (filters out short teasers/previews).
  - Maximum: `10 minutes` (600 seconds) — anything 10+ minutes is a multi-song jukebox/compilation and must NOT be downloaded as a single song.

### Rule 3: Search Presentation (Up to 10 Complete Options + Audio Previews)
- When a user searches for a song, return **up to 10 options**.
- In the Telegram message body, display the **complete, untruncated song title** and **all singer/artist name(s)**, along with duration and album name:
  ```text
  1️⃣ Full Song Title
     🎤 Singer 1, Singer 2 • ⏱️ 4:22 • 💿 Album Name
  ```
- Underneath the message, provide two rows of quick buttons:
  - `[ ⬇️ 1️⃣ ] ... [ ⬇️ 🔟 ]`: Download the full song directly to `downloads/`.
  - `[ 🎧 1️⃣ ] ... [ 🎧 🔟 ]`: **Hear a 30s preview first!**
- When a user taps `[ 🎧 # ]`, the bot generates a 30s audio sample and sends it directly with Telegram's inline playable waveform, plus a `[ ⬇️ Download Full Song ]` button below the audio snippet.

### Rule 4: Albums & Playlists (No Jukeboxes)
- When a query contains `playlist`, `album`, `ost`, or `soundtrack` (e.g. `"dhurandar playlist"`), the bot searches YouTube Music Albums & Playlists.
- It presents the album option: e.g. `💿 Dhurandhar The Revenge (14 tracks)`.
- When downloaded, **each song is downloaded as an individual `.mp3` file** with its own tags and artwork, and a `.m3u` playlist is created. It NEVER downloads 45-minute stitched jukeboxes.

### Rule 5: Suggested Songs / Daily Discovery
- Recommendations generated via `/discover` or the daily background scheduler MUST query YouTube Music official radio mixes (`get_watch_playlist`).
- Discard any candidates already in `downloads/` or `library/`, and discard any non-studio tracks.
- Always display the full song title, all singers, duration, album, and the seed track it was based on.

---

## 🔒 3. Secrets & Database Safety Rules

### Rule 1: Zero Secrets in Git
- Real bot tokens (`TELEGRAM_BOT_TOKEN`), user Telegram IDs (`ALLOWED_USERS`), and local filesystem paths MUST NEVER be hardcoded in code or committed to Git.
- All secrets reside strictly in `.env`.
- `.env` and `test.py` are explicitly listed in `.gitignore`.
- Whenever a new environment variable is introduced, document it with placeholder values in `.env.example`.

### Rule 2: Navidrome SQLite Database Safety
- Navidrome's database (`navidrome.db`) runs in SQLite WAL (Write-Ahead Logging) mode.
- Mount the volume containing `navidrome.db` and open SQLite connections using URI read-only mode:
  ```python
  sqlite3.connect(f"file:{NAVIDROME_DB_PATH}?mode=ro", uri=True)
  ```
- Never execute write operations (`INSERT`, `UPDATE`, `DELETE`, `DROP`) directly on Navidrome's database.

---

## 🔄 4. Git & Workflow Standards

1. **Before pushing**:
   - Run `git status` to ensure `.env` and temporary files are NOT tracked.
   - Run `python3 -m py_compile bot.py` to ensure zero syntax errors.
   - Rebuild or restart the Docker container (`docker compose up -d --build`) to verify runtime startup.
2. **Commit Messages**:
   - Use Conventional Commits format (`feat(...)`, `fix(...)`, `chore(...)`, `docs(...)`).
3. **Communication**:
   - Always explain to the user clearly what was changed, why, and how it behaves before proposing commits or pushes.
   - Keep this `AGENTS.md` updated as new user decisions and features are introduced.
