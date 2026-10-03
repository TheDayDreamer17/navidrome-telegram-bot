from __future__ import annotations
import os
import re
import time
import json
import shutil
import random
import sqlite3
import logging
import asyncio
import subprocess
from pathlib import Path

from curl_cffi import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from ytmusicapi import YTMusic

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MusicBot")

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NAVIDROME_DB_PATH = Path(os.getenv("NAVIDROME_DB_PATH", "/navidrome_data/navidrome.db"))
UNPLAYED_EXPIRY_DAYS = int(os.getenv("UNPLAYED_EXPIRY_DAYS", "14"))
DAILY_INTERVAL_SECONDS = int(os.getenv("DAILY_INTERVAL_SECONDS", "86400"))

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"}

# Map user_id -> downloads Path (e.g. /music/vismay/downloads)
USER_DIR_MAP: dict[int, Path] = {}
for entry in ALLOWED_USERS_RAW.split(","):
    if ":" in entry:
        uid, path = entry.split(":", 1)
        dl_path = Path(path.strip())
        USER_DIR_MAP[int(uid.strip())] = dl_path

logger.info(f"Loaded {len(USER_DIR_MAP)} authorized user(s).")

URL_REGEX = re.compile(r"https?://[^\s]+")

# --- Persistent Main Keyboard ---
MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🎧 Discover Song"), KeyboardButton("📊 Library Status")],
        [KeyboardButton("🧹 Cleanup & Sync"), KeyboardButton("❓ Help Guide")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# --- Quick Action Inline Keyboard ---
HELP_INLINE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🎧 Discover New Song", callback_data="cmd:discover"),
        InlineKeyboardButton("📊 Library Status", callback_data="cmd:status"),
    ],
    [
        InlineKeyboardButton("🧹 Cleanup & Sync", callback_data="cmd:cleanup"),
        InlineKeyboardButton("❓ Help & Guide", callback_data="cmd:help"),
    ],
])


def get_user_paths(user_id: int) -> tuple[Path, Path, Path]:
    """Returns (user_root, downloads_dir, library_dir) for a user."""
    downloads_dir = USER_DIR_MAP[user_id]
    user_root = downloads_dir.parent
    library_dir = user_root / "library"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    library_dir.mkdir(parents=True, exist_ok=True)
    return user_root, downloads_dir, library_dir


def list_audio_files(directory: Path) -> set[Path]:
    if not directory.exists():
        return set()
    return {
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    }


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def create_m3u_playlist(playlist_name: str, target_dir: Path, audio_entries: list[str]):
    clean_name = sanitize_filename(playlist_name) or "New Playlist"
    m3u_file = target_dir / f"{clean_name}.m3u"
    logger.info(f"Writing .m3u playlist: '{m3u_file}' with {len(audio_entries)} tracks.")
    with open(m3u_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for entry in audio_entries:
            f.write(f"{entry}\n")


def append_to_m3u_playlist(playlist_name: str, target_dir: Path, audio_entry: str):
    clean_name = sanitize_filename(playlist_name) or "Daily Discovery"
    m3u_file = target_dir / f"{clean_name}.m3u"
    existing = []
    if m3u_file.exists():
        existing = [
            line.strip()
            for line in m3u_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if audio_entry not in existing:
        existing.append(audio_entry)
    create_m3u_playlist(clean_name, target_dir, existing)


def update_m3u_references_on_move(user_root: Path, downloads_dir: Path, old_filename: str, new_rel_from_downloads: str):
    """Updates .m3u files when a track moves between downloads/ and library/."""
    for folder in (downloads_dir, user_root):
        if not folder.exists():
            continue
        for m3u in folder.glob("*.m3u"):
            try:
                lines = m3u.read_text(encoding="utf-8").splitlines()
                updated = []
                changed = False
                for line in lines:
                    stripped = line.strip()
                    if stripped == old_filename or stripped.endswith(f"/{old_filename}"):
                        updated.append(new_rel_from_downloads if folder == downloads_dir else f"library/{old_filename}")
                        changed = True
                    else:
                        updated.append(line)
                if changed:
                    m3u.write_text("\n".join(updated) + "\n", encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not update m3u {m3u}: {e}")


def remove_from_m3u_playlists(user_root: Path, downloads_dir: Path, filename: str):
    """Removes deleted track references from all .m3u playlists."""
    for folder in (downloads_dir, user_root):
        if not folder.exists():
            continue
        for m3u in folder.glob("*.m3u"):
            try:
                lines = m3u.read_text(encoding="utf-8").splitlines()
                filtered = [
                    line for line in lines
                    if line.strip() != filename and not line.strip().endswith(f"/{filename}")
                ]
                if len(filtered) != len(lines):
                    m3u.write_text("\n".join(filtered) + "\n", encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not clean m3u {m3u}: {e}")


# --- Spotify Metadata Resolver ---
def get_spotify_playlist_title(url: str) -> str:
    try:
        clean_url = url.split("?")[0]
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(clean_url, headers=headers, impersonate="chrome120", timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                return sanitize_filename(og_title["content"].strip())
            if soup.title and soup.title.string:
                title = soup.title.string.split("|")[0].replace("- playlist by", "").strip()
                return sanitize_filename(title)
    except Exception as e:
        logger.error(f"[Spotify] Could not extract playlist title: {e}")
    return "Spotify Playlist"


# --- Amazon Music Headless Skill API Interceptor ---
async def scrape_amazon_music_playlist(url: str) -> tuple[str, list[str]]:
    """
    Launches headless Chromium, intercepts Amazon Music's internal Skill API
    JSON response (showHome / showLibraryPlaylist / showCatalogPlaylist),
    extracts the playlist title + tracks, and closes Chromium immediately.
    """
    tracks: list[str] = []
    playlist_title = "Amazon Playlist"
    api_event = asyncio.Event()

    logger.info(f"[Amazon Headless] Spawning Chromium & intercepting Skill API for: {url}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
        )
        page = await context.new_page()

        async def handle_response(response):
            nonlocal playlist_title, tracks
            if "skill.music.a2z.com/api/" not in response.url:
                return
            try:
                data = await response.json()
                for method in data.get("methods", []):
                    template = method.get("template") or {}
                    widgets = template.get("widgets") or []
                    if not widgets:
                        continue

                    header = template.get("headerText") or {}
                    if isinstance(header, dict) and header.get("text"):
                        playlist_title = header["text"].strip()
                    elif isinstance(header, str) and header.strip():
                        playlist_title = header.strip()

                    for widget in widgets:
                        for item in widget.get("items") or []:
                            song = (item.get("primaryText") or "").strip()
                            artist = (item.get("secondaryText1") or "").strip()
                            if song and artist:
                                entry = f"{artist} - {song}"
                            elif song:
                                entry = song
                            else:
                                continue
                            if entry not in tracks:
                                tracks.append(entry)

                    if tracks:
                        api_event.set()
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            try:
                await asyncio.wait_for(api_event.wait(), timeout=18.0)
            except asyncio.TimeoutError:
                logger.warning("[Amazon Headless] Timed out waiting for Skill API payload.")

            if playlist_title == "Amazon Playlist":
                raw_title = await page.title()
                if raw_title:
                    playlist_title = re.split(r"\||-", raw_title)[0].strip()

            logger.info(f"[Amazon Headless] Extracted {len(tracks)} tracks for '{playlist_title}'.")
        except Exception as e:
            logger.error(f"[Amazon Headless] Scraper error: {e}")
        finally:
            await browser.close()

    return sanitize_filename(playlist_title) or "Amazon Playlist", tracks


# --- Existing Library Matching ---
def find_existing_track_for_query(query: str, downloads_dir: Path, library_dir: Path) -> str | None:
    """
    Checks if a track matching `Artist - Title` already exists in downloads/ or library/.
    Returns the .m3u entry relative to downloads_dir if found, else None.
    """
    parts = [p.strip() for p in query.split(" - ", 1)]
    song_norm = normalize_text(parts[-1])
    if not song_norm or len(song_norm) < 3:
        return None

    for f in list_audio_files(downloads_dir):
        if song_norm in normalize_text(f.stem):
            return f.name

    for f in list_audio_files(library_dir):
        if song_norm in normalize_text(f.stem):
            return f"../library/{f.name}"

    return None


# --- Clean Audio / Studio Track Filter Patterns ---
BAD_TITLE_PATTERNS = [
    r"\b(official\s+)?music\s+video\b",
    r"\bofficial\s+video\b",
    r"\bvideo\s+song\b",
    r"\bfull\s+video\b",
    r"\blyric(s)?\s+video\b",
    r"\blo-?fi(\s+flip|\s+mix|\s+remix)?\b",
    r"\bslowed(\s*\+\s*reverb)?\b",
    r"\breverb\b",
    r"\b8d(\s+audio)?\b",
    r"\bnightcore\b",
    r"\bspeed\s*up\b",
    r"\bsped\s*up\b",
    r"\bbass\s*boosted\b",
    r"\bteaser\b",
    r"\btrailer\b",
    r"\bpromo\b",
    r"\breaction\b",
    r"\bstatus\b",
    r"\bshorts\b",
    # Jukeboxes & Multi-song Compilations
    r"\b(full\s+)?(audio\s+|video\s+)?jukebox\b",
    r"\b(full\s+)?album\s+jukebox\b",
    r"\bfull\s+album\b",
    r"\ball\s+songs\b",
    r"\bnon\s*stop\b",
    r"\bjukebox\b",
    r"\bcompilation\b",
]


def is_clean_studio_track(title: str, artists: str = "", user_query: str = "") -> bool:
    """Filters out noisy music videos, lofi flips, slowed+reverb, and promos unless specifically asked for."""
    combined = f"{title.lower()} {artists.lower()}"
    query_lower = user_query.lower()
    for pattern in BAD_TITLE_PATTERNS:
        if re.search(pattern, query_lower):
            continue
        if re.search(pattern, combined):
            return False
    return True


def clean_display_title(title: str) -> str:
    """Removes bracketed fluff like '[Official Audio]' or '(Audio)' from song titles."""
    cleaned = re.sub(r'[\(\[\{]\s*(?:official\s+)?audio(?:\s+song)?\s*[\)\]\}]', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(r'\|\s*(?:official\s+)?audio(?:\s+song)?', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


# --- YouTube Music Search & Download Helpers ---
def search_youtube_candidates(query: str, limit: int = 10) -> list[dict]:
    """
    Searches YouTube Music for official studio songs, filtering out modified music videos.
    Returns up to 'limit' (default 10) results with full song title, singers, album, and duration.
    """
    candidates: list[dict] = []

    # 1. Primary: YouTube Music API (Clean Official Studio Songs)
    try:
        yt = YTMusic()
        results = yt.search(query, filter="songs", limit=max(25, limit * 2))
        for r in results:
            vid = r.get("videoId")
            title = (r.get("title") or "").strip()
            artists = ", ".join([a["name"] for a in r.get("artists", [])]) or "Unknown Artist"
            dur = r.get("duration") or "Unknown"
            album = r.get("album", {}).get("name") if r.get("album") else ""

            if not vid or not title:
                continue

            if not is_clean_studio_track(title, artists, query):
                continue

            # Filter out short teasers (< 45s) and long jukeboxes/compilations (> 10 mins)
            if dur and ":" in dur:
                parts = dur.split(":")
                try:
                    if len(parts) == 2:
                        m, s = int(parts[0]), int(parts[1])
                        if m < 1 and s < 45:
                            continue
                        if m >= 10:  # 10 minutes or longer = jukebox / compilation
                            continue
                    elif len(parts) >= 3:  # 1 hour or longer
                        continue
                except ValueError:
                    pass

            candidates.append({
                "id": vid,
                "title": clean_display_title(title),
                "artists": artists,
                "channel": artists,
                "album": album,
                "duration": dur,
            })
            if len(candidates) >= limit:
                break
    except Exception as e:
        logger.warning(f"[Search] YTMusic search failed, falling back to yt-dlp: {e}")

    if candidates:
        return candidates

    # 2. Fallback: yt-dlp search with audio filter
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--dump-single-json",
        "--flat-playlist",
        f"ytsearch25:{query} official audio",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0 and proc.stdout:
        try:
            data = json.loads(proc.stdout)
            for entry in data.get("entries", []):
                vid = entry.get("id")
                title = (entry.get("title") or "").strip()
                channel = (entry.get("channel") or entry.get("uploader") or "Unknown Artist").replace(" - Topic", "").strip()
                duration = int(entry.get("duration") or 0)
                mins, secs = divmod(duration, 60)
                dur_str = f"{mins}:{secs:02d}" if duration else "Unknown"

                if not vid or not title or duration < 45 or duration > 600:
                    continue
                if not is_clean_studio_track(title, channel, query):
                    continue

                candidates.append({
                    "id": vid,
                    "title": clean_display_title(title),
                    "artists": channel,
                    "channel": channel,
                    "album": "",
                    "duration": dur_str,
                })
                if len(candidates) >= limit:
                    break
        except Exception:
            pass

    return candidates


def download_specific_track(video_id: str, target_dir: Path) -> tuple[bool, str, list[str]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    before = list_audio_files(target_dir)
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--embed-metadata",
        "-o", str(target_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
        url,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return False, proc.stderr[-300:], []
    after = list_audio_files(target_dir)
    new_files = [f.name for f in (after - before)]
    return True, "Track downloaded successfully!", new_files


def extract_audio_preview(video_id: str) -> Path | None:
    """Extracts a fast 30s audio sample using ffmpeg for Telegram in-chat preview."""
    out_path = Path(f"/tmp/preview_{video_id}.mp3")
    if out_path.exists() and out_path.stat().st_size > 10000:
        return out_path
    out_path.unlink(missing_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--downloader", "ffmpeg",
        "--downloader-args", "ffmpeg_i:-ss 00:00:30 -t 30",
        "-o", str(out_path),
        url,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=35)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 10000:
            return out_path
    except Exception as e:
        logger.error(f"Failed to generate audio preview for {video_id}: {e}")
    return None


def search_albums_and_playlists(query: str, limit: int = 5) -> list[dict]:
    """Searches YouTube Music for official Albums and Playlists."""
    clean_q = re.sub(r'\b(playlist|album|songs|full|ost|soundtrack)\b', '', query, flags=re.IGNORECASE).strip()
    if not clean_q:
        clean_q = query

    results = []
    yt = YTMusic()
    try:
        albums = yt.search(clean_q, filter="albums", limit=limit)
        for a in albums:
            browse_id = a.get("browseId")
            title = a.get("title", "Unknown Album")
            artists = ", ".join([ar["name"] for ar in a.get("artists", [])]) or "Various Artists"
            year = a.get("year", "")
            type_ = a.get("type", "Album")
            if browse_id:
                results.append({
                    "kind": "album",
                    "id": browse_id,
                    "title": title,
                    "artists": artists,
                    "year": year,
                    "type": type_,
                })
    except Exception as e:
        logger.warning(f"[Search] Album search error: {e}")

    try:
        playlists = yt.search(clean_q, filter="playlists", limit=limit)
        for p in playlists:
            browse_id = p.get("browseId")
            title = p.get("title", "Unknown Playlist")
            author = p.get("author", "")
            count = p.get("itemCount")
            if browse_id:
                results.append({
                    "kind": "playlist",
                    "id": browse_id,
                    "title": title,
                    "artists": author,
                    "count": count,
                    "type": "Playlist",
                })
    except Exception as e:
        logger.warning(f"[Search] Playlist search error: {e}")

    return results


async def download_album_or_playlist_job(kind: str, browse_id: str, user_id: int, status_msg) -> str:
    """Downloads all tracks from an album/playlist as separate songs and generates an .m3u playlist."""
    user_root, downloads_dir, library_dir = get_user_paths(user_id)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    library_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    yt = YTMusic()

    if kind == "album":
        data = await loop.run_in_executor(None, yt.get_album, browse_id)
    else:
        data = await loop.run_in_executor(None, yt.get_playlist, browse_id)

    if not data or not data.get("tracks"):
        return "❌ Could not fetch album or playlist tracks."

    collection_title = data.get("title") or "Album Playlist"
    tracks = data.get("tracks", [])
    total_tracks = len(tracks)

    import html
    await status_msg.edit_text(
        f"💿 <b>Found {kind.capitalize()}: '{html.escape(collection_title)}'</b> ({total_tracks} tracks).\n"
        f"⚡ Downloading individual studio songs into <code>downloads/</code>...",
        parse_mode="HTML"
    )

    m3u_entries = []
    new_downloads_count = 0

    for idx, t in enumerate(tracks, 1):
        vid = t.get("videoId")
        title = t.get("title") or f"Track {idx}"
        artists = ", ".join([a["name"] for a in t.get("artists", [])]) if t.get("artists") else ""
        q = f"{artists} - {title}" if artists else title

        existing = find_existing_track_for_query(q, downloads_dir, library_dir)
        if existing:
            m3u_entries.append(existing)
            continue

        if idx % 2 == 0 or idx == 1 or idx == total_tracks:
            try:
                await status_msg.edit_text(
                    f"⏳ Downloading <b>{html.escape(collection_title)}</b>: <b>{idx}/{total_tracks}</b> tracks...\n"
                    f"Current: <i>{html.escape(q)}</i>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        if vid:
            ok, _, new_f = await loop.run_in_executor(None, download_specific_track, vid, downloads_dir)
            if ok and new_f:
                m3u_entries.append(new_f[0])
                new_downloads_count += len(new_f)
                continue

        # Fallback to single search
        new_f = await loop.run_in_executor(None, download_ytsearch_single, q, downloads_dir)
        if new_f:
            m3u_entries.append(new_f[0])
            new_downloads_count += len(new_f)

    create_m3u_playlist(collection_title, downloads_dir, m3u_entries)
    return (
        f"✅ <b>{kind.capitalize()} Downloaded Successfully!</b>\n\n"
        f"💿 <b>{html.escape(collection_title)}</b>\n"
        f"🎵 <b>Total tracks:</b> {len(m3u_entries)} / {total_tracks}\n"
        f"📥 <b>Newly downloaded:</b> {new_downloads_count} songs\n"
        f"📜 Playlist saved to <code>downloads/{sanitize_filename(collection_title)}.m3u</code>\n\n"
        f"💡 <i>Tip: No jukeboxes! Each song is saved separately and tagged.</i>"
    )


def download_ytsearch_single(track_query: str, target_dir: Path) -> list[str]:
    """Downloads 1 track via ytsearch1 and returns any newly created file names."""
    before = list_audio_files(target_dir)
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--embed-metadata",
        "-o", str(target_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
        f"ytsearch1:{track_query}",
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    after = list_audio_files(target_dir)
    return [f.name for f in (after - before)]


# --- Direct URL Download Dispatcher ---
async def download_direct_url_job(url: str, user_id: int, status_msg=None) -> tuple[bool, str]:
    _, downloads_dir, library_dir = get_user_paths(user_id)
    before_files = list_audio_files(downloads_dir)
    loop = asyncio.get_running_loop()

    # 1. SPOTIFY
    if "spotify.com" in url:
        clean_spotify_url = url.split("?")[0]
        is_playlist = "playlist" in clean_spotify_url or "album" in clean_spotify_url
        pl_name = await loop.run_in_executor(None, get_spotify_playlist_title, clean_spotify_url) if is_playlist else ""

        cmd = [
            "spotdl",
            "download",
            clean_spotify_url,
            "--output", str(downloads_dir / "{artist} - {title}.{output-ext}"),
            "--format", "mp3",
            "--bitrate", "320k",
        ]
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True),
        )
        if proc.returncode != 0:
            return False, proc.stderr[-300:]

        if is_playlist:
            after_files = list_audio_files(downloads_dir)
            new_files = [f.name for f in (after_files - before_files)]
            if new_files:
                create_m3u_playlist(pl_name, downloads_dir, sorted(new_files))
            return True, f"Downloaded Spotify playlist '{pl_name}' ({len(new_files)} songs) and synced .m3u!"

        return True, "Spotify track synced successfully!"

    # 2. AMAZON MUSIC
    elif "music.amazon." in url:
        if status_msg:
            try:
                await status_msg.edit_text("🔎 Extracting tracks from Amazon Music playlist...")
            except Exception:
                pass

        pl_name, tracks = await scrape_amazon_music_playlist(url)
        if not tracks:
            return False, "Could not extract tracks from Amazon Music. Verify the playlist is public/shared."

        logger.info(f"[Amazon] Processing {len(tracks)} songs for playlist '{pl_name}'...")
        playlist_entries: list[str] = []
        downloaded_count = 0
        reused_count = 0

        for i, track in enumerate(tracks, 1):
            if status_msg and (i == 1 or i % 3 == 0 or i == len(tracks)):
                try:
                    await status_msg.edit_text(
                        f"⬇️ Syncing Amazon playlist '{pl_name}' ({i}/{len(tracks)}):\n🎵 {track}"
                    )
                except Exception:
                    pass

            existing_entry = find_existing_track_for_query(track, downloads_dir, library_dir)
            if existing_entry:
                logger.info(f"[Amazon] [{i}/{len(tracks)}] Already in library: '{track}' -> {existing_entry}")
                if existing_entry not in playlist_entries:
                    playlist_entries.append(existing_entry)
                reused_count += 1
                continue

            logger.info(f"[Amazon] [{i}/{len(tracks)}] Downloading via YouTube: '{track}'")
            new_names = await loop.run_in_executor(None, download_ytsearch_single, track, downloads_dir)
            if new_names:
                for name in new_names:
                    if name not in playlist_entries:
                        playlist_entries.append(name)
                downloaded_count += len(new_names)
            else:
                fallback_entry = find_existing_track_for_query(track, downloads_dir, library_dir)
                if fallback_entry and fallback_entry not in playlist_entries:
                    playlist_entries.append(fallback_entry)

        if not playlist_entries:
            after_files = list_audio_files(downloads_dir)
            playlist_entries = sorted(f.name for f in (after_files - before_files))

        if playlist_entries:
            create_m3u_playlist(pl_name, downloads_dir, playlist_entries)

        return (
            True,
            f"Synced Amazon playlist '{pl_name}' ({len(playlist_entries)} tracks in .m3u: "
            f"{downloaded_count} new, {reused_count} already in library)!",
        )

    # 3. YOUTUBE / YOUTUBE MUSIC
    else:
        is_playlist = "list=" in url or "/playlist" in url
        if is_playlist:
            title_cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", url]
            title_proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(title_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True),
            )
            pl_name = "YouTube Playlist"
            if title_proc.returncode == 0 and title_proc.stdout:
                try:
                    pl_name = json.loads(title_proc.stdout).get("title") or "YouTube Playlist"
                except Exception:
                    pass

            clean_pl_name = sanitize_filename(pl_name)
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--embed-thumbnail",
                "--embed-metadata",
                "--yes-playlist",
                "-o", str(downloads_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
                url,
            ]
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True),
            )
            if proc.returncode != 0:
                return False, proc.stderr[-300:]

            after_files = list_audio_files(downloads_dir)
            new_files = [f.name for f in (after_files - before_files)]
            if new_files:
                create_m3u_playlist(clean_pl_name, downloads_dir, sorted(new_files))

            return True, f"Downloaded playlist '{clean_pl_name}' ({len(new_files)} tracks) with .m3u synced!"
        else:
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--embed-thumbnail",
                "--embed-metadata",
                "--no-playlist",
                "-o", str(downloads_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
                url,
            ]
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True),
            )
            if proc.returncode != 0:
                return False, proc.stderr[-300:]
            return True, "Track downloaded successfully!"


# --- Navidrome Database Integration (Promotion, Cleanup & Discovery) ---
def get_navidrome_conn() -> sqlite3.Connection | None:
    if not NAVIDROME_DB_PATH.exists():
        logger.warning(f"[Navidrome DB] Not found at {NAVIDROME_DB_PATH}")
        return None
    try:
        conn = sqlite3.connect(f"file:{NAVIDROME_DB_PATH}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"[Navidrome DB] Failed to open DB: {e}")
        return None


def get_navidrome_library_id(conn: sqlite3.Connection, user_root: Path) -> int | None:
    cur = conn.execute("SELECT id, path FROM library")
    for row in cur.fetchall():
        if Path(row["path"]).resolve() == user_root.resolve() or row["path"].rstrip("/") == str(user_root).rstrip("/"):
            return int(row["id"])
    return None


def promote_starred_to_library(user_id: int) -> list[str]:
    """
    Moves songs in downloads/ that are Starred (❤️) or rated 4-5⭐ into library/.
    Updates any .m3u playlists accordingly.
    """
    user_root, downloads_dir, library_dir = get_user_paths(user_id)
    conn = get_navidrome_conn()
    if not conn:
        return []

    promoted: list[str] = []
    try:
        lib_id = get_navidrome_library_id(conn, user_root)
        if lib_id is None:
            return []

        query = """
            SELECT DISTINCT m.path, m.title, m.artist
            FROM media_file m
            JOIN annotation a ON a.item_id = m.id AND a.item_type = 'media_file'
            WHERE m.library_id = ?
              AND m.missing = 0
              AND m.path LIKE 'downloads/%'
              AND (a.starred = 1 OR a.rating >= 4)
        """
        rows = conn.execute(query, (lib_id,)).fetchall()
        for row in rows:
            rel_path = row["path"]
            src_file = user_root / rel_path
            if not src_file.exists():
                continue

            dest_file = library_dir / src_file.name
            logger.info(f"[Lifecycle] Promoting Starred/4-5⭐ track to library: {src_file.name}")
            shutil.move(str(src_file), str(dest_file))
            update_m3u_references_on_move(user_root, downloads_dir, src_file.name, f"../library/{src_file.name}")
            promoted.append(f"{row['artist']} - {row['title']}" if row["artist"] else src_file.stem)
    except Exception as e:
        logger.error(f"[Lifecycle] Error promoting tracks: {e}")
    finally:
        conn.close()

    return promoted


def find_cleanup_candidates(user_id: int) -> list[dict]:
    """
    Finds tracks eligible for deletion:
    1. Rated 1⭐ in Navidrome
    2. Added to a 'Delete' or 'Trash' playlist in Navidrome
    3. In downloads/ older than UNPLAYED_EXPIRY_DAYS with 0 plays and not Starred / rated >= 4
    """
    user_root, downloads_dir, _ = get_user_paths(user_id)
    conn = get_navidrome_conn()
    candidates: dict[str, dict] = {}

    if conn:
        try:
            lib_id = get_navidrome_library_id(conn, user_root)
            if lib_id is not None:
                # 1. 1-star rated songs
                q_one_star = """
                    SELECT m.path, m.title, m.artist
                    FROM media_file m
                    JOIN annotation a ON a.item_id = m.id AND a.item_type = 'media_file'
                    WHERE m.library_id = ? AND m.missing = 0 AND a.rating = 1
                """
                for r in conn.execute(q_one_star, (lib_id,)).fetchall():
                    full_path = user_root / r["path"]
                    if full_path.exists():
                        candidates[str(full_path)] = {
                            "path": full_path,
                            "label": f"{r['artist']} - {r['title']}" if r["artist"] else full_path.stem,
                            "reason": "1⭐ rating",
                        }

                # 2. Songs in 'Delete' or 'Trash' playlist
                q_trash_pl = """
                    SELECT m.path, m.title, m.artist, p.name AS pl_name
                    FROM playlist_tracks pt
                    JOIN playlist p ON p.id = pt.playlist_id
                    JOIN media_file m ON m.id = pt.media_file_id
                    WHERE m.library_id = ? AND m.missing = 0
                      AND LOWER(TRIM(p.name)) IN ('delete', 'trash', 'remove')
                """
                for r in conn.execute(q_trash_pl, (lib_id,)).fetchall():
                    full_path = user_root / r["path"]
                    if full_path.exists():
                        candidates[str(full_path)] = {
                            "path": full_path,
                            "label": f"{r['artist']} - {r['title']}" if r["artist"] else full_path.stem,
                            "reason": f"In '{r['pl_name']}' playlist",
                        }

                # 3. Unplayed songs in downloads/ older than UNPLAYED_EXPIRY_DAYS
                q_downloads = """
                    SELECT m.path, m.title, m.artist,
                           MAX(COALESCE(a.play_count, 0)) AS total_plays,
                           MAX(COALESCE(a.starred, 0)) AS is_starred,
                           MAX(COALESCE(a.rating, 0)) AS max_rating
                    FROM media_file m
                    LEFT JOIN annotation a ON a.item_id = m.id AND a.item_type = 'media_file'
                    WHERE m.library_id = ? AND m.missing = 0 AND m.path LIKE 'downloads/%'
                    GROUP BY m.id
                """
                cutoff = time.time() - (UNPLAYED_EXPIRY_DAYS * 86400)
                for r in conn.execute(q_downloads, (lib_id,)).fetchall():
                    full_path = user_root / r["path"]
                    if not full_path.exists() or str(full_path) in candidates:
                        continue
                    if r["is_starred"] or r["max_rating"] >= 4 or r["total_plays"] > 0:
                        continue
                    if full_path.stat().st_mtime < cutoff:
                        age_days = int((time.time() - full_path.stat().st_mtime) // 86400)
                        candidates[str(full_path)] = {
                            "path": full_path,
                            "label": f"{r['artist']} - {r['title']}" if r["artist"] else full_path.stem,
                            "reason": f"Unplayed for {age_days}d in downloads",
                        }
        except Exception as e:
            logger.error(f"[Cleanup] Error querying cleanup candidates: {e}")
        finally:
            conn.close()

    return list(candidates.values())


def discover_new_song_sync(user_id: int) -> tuple[bool, str]:
    """
    Picks a seed song from the user's Navidrome Starred / Top Played tracks,
    queries YouTube Music Radio Mix (RD<video_id>), filters out existing library songs,
    downloads 1 new song into downloads/, and adds it to 'Daily Discovery.m3u'.
    """
    user_root, downloads_dir, library_dir = get_user_paths(user_id)
    existing_files = list_audio_files(downloads_dir) | list_audio_files(library_dir)
    existing_norms = {normalize_text(f.stem) for f in existing_files}

    seed_tracks: list[str] = []
    conn = get_navidrome_conn()
    if conn:
        try:
            lib_id = get_navidrome_library_id(conn, user_root)
            if lib_id is not None:
                q_seeds = """
                    SELECT m.artist, m.title,
                           MAX(COALESCE(a.starred, 0)) AS is_starred,
                           MAX(COALESCE(a.rating, 0)) AS max_rating,
                           SUM(COALESCE(a.play_count, 0)) AS plays
                    FROM media_file m
                    LEFT JOIN annotation a ON a.item_id = m.id AND a.item_type = 'media_file'
                    WHERE m.library_id = ? AND m.missing = 0
                    GROUP BY m.id
                    ORDER BY is_starred DESC, max_rating DESC, plays DESC, RANDOM()
                    LIMIT 15
                """
                for r in conn.execute(q_seeds, (lib_id,)).fetchall():
                    artist = (r["artist"] or "").strip()
                    title = (r["title"] or "").strip()
                    if artist and title and artist.lower() != "unknown artist":
                        seed_tracks.append(f"{artist} - {title}")
                    elif title:
                        seed_tracks.append(title)
                    existing_norms.add(normalize_text(f"{artist} {title}"))
                    existing_norms.add(normalize_text(title))
        except Exception as e:
            logger.error(f"[Discover] DB query error: {e}")
        finally:
            conn.close()

    if not seed_tracks:
        seed_tracks = [f.stem for f in existing_files]

    if not seed_tracks:
        return False, "Your library is empty! Download a few songs first so I can learn your taste."

    top_pool = seed_tracks[: min(8, len(seed_tracks))]
    random.shuffle(top_pool)

    yt = YTMusic()
    for seed in top_pool:
        logger.info(f"[Discover] Trying seed track: '{seed}'")
        try:
            seed_results = yt.search(seed, filter="songs", limit=3)
            if not seed_results or not seed_results[0].get("videoId"):
                continue

            seed_vid = seed_results[0]["videoId"]
            watch = yt.get_watch_playlist(videoId=seed_vid, limit=30)
            raw_tracks = watch.get("tracks", [])
            if not raw_tracks:
                continue

            candidates = raw_tracks[1:] if len(raw_tracks) > 1 else raw_tracks
            random.shuffle(candidates)

            for entry in candidates:
                vid = entry.get("videoId")
                title = clean_display_title((entry.get("title") or "").strip())
                artists = ", ".join([a["name"] for a in entry.get("artists", [])]) or "Unknown Artist"
                dur = entry.get("length") or ""
                album = entry.get("album", {}).get("name") if entry.get("album") else ""

                if not vid or not title or vid == seed_vid:
                    continue
                if not is_clean_studio_track(title, artists):
                    continue

                norm_title = normalize_text(title)
                norm_full = normalize_text(f"{artists} {title}")
                if any(
                    norm_title in ex or ex in norm_title or norm_full in ex
                    for ex in existing_norms
                    if len(ex) >= 4
                ):
                    continue

                logger.info(f"[Discover] Selected clean recommendation: '{artists} - {title}' ({vid}) from seed '{seed}'")
                ok, err, new_files = download_specific_track(vid, downloads_dir)
                if ok and new_files:
                    for nf in new_files:
                        append_to_m3u_playlist("Daily Discovery", downloads_dir, nf)
                    album_str = f" • 💿 _{album}_" if album else ""
                    dur_str = f"⏱️ {dur}" if dur else ""
                    details = f"{dur_str}{album_str}".strip(" • ")
                    details_line = f"ℹ️ {details}\n" if details else ""
                    return (
                        True,
                        f"✨ **New Discovery Added!**\n\n"
                        f"🎧 **Based on your taste:** _{seed}_\n"
                        f"🎵 **Song:** **{title}**\n"
                        f"🎤 **Singer(s):** *{artists}*\n"
                        f"{details_line}"
                        f"📂 Added to `Daily Discovery.m3u` in `downloads/`\n\n"
                        f"💡 _Tip: ❤️ Star it in Navidrome to move to permanent `library/`, or rate 1⭐ to remove._",
                    )
        except Exception as e:
            logger.warning(f"[Discover] Error querying radio for seed '{seed}': {e}")
            continue

    return False, "Could not find a new recommendation right now. Try again later!"


# --- Telegram Lifecycle & Cleanup Prompt Helper ---
async def run_user_lifecycle_and_notify(app: Application, user_id: int, manual: bool = False):
    loop = asyncio.get_running_loop()

    # 1. Promote Starred / 4-5⭐ tracks from downloads/ to library/
    promoted = await loop.run_in_executor(None, promote_starred_to_library, user_id)
    if promoted:
        msg_lines = ["📦 **Promoted to Permanent Library (`library/`):**"]
        for p in promoted[:15]:
            msg_lines.append(f"• ❤️ {p}")
        await app.bot.send_message(chat_id=user_id, text="\n".join(msg_lines), parse_mode="Markdown")

    # 2. Check for cleanup candidates (1⭐, Delete playlist, or >14d unplayed in downloads/)
    candidates = await loop.run_in_executor(None, find_cleanup_candidates, user_id)
    if candidates:
        app.bot_data.setdefault("cleanup_Restore", {})[user_id] = candidates
        lines = ["🧹 **Songs Marked for Cleanup:**\n"]
        keyboard = []
        for idx, c in enumerate(candidates[:10]):
            lines.append(f"{idx + 1}. **{c['label']}** _({c['reason']})_")
            short_label = c["label"][:24]
            keyboard.append([
                InlineKeyboardButton(f"🗑️ Delete #{idx + 1}: {short_label}", callback_data=f"cdel:{idx}"),
                InlineKeyboardButton(f"❤️ Keep #{idx + 1}", callback_data=f"ckeep:{idx}"),
            ])
        if len(candidates) > 10:
            lines.append(f"\n_...and {len(candidates) - 10} more._")

        keyboard.append([
            InlineKeyboardButton(f"🗑️ Confirm Delete All ({len(candidates)})", callback_data="cdel_all"),
            InlineKeyboardButton("❌ Keep All for Now", callback_data="ccancel"),
        ])
        await app.bot.send_message(
            chat_id=user_id,
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    elif manual and not promoted:
        await app.bot.send_message(
            chat_id=user_id,
            text="✅ **Library Sync Complete!**\n• No new ❤️/4-5⭐ tracks in `downloads/` to move to `library/`.\n• No 1⭐ or expired unplayed tracks to clean up.",
            parse_mode="Markdown",
        )


# --- Telegram Command & Message Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in USER_DIR_MAP:
        await update.message.reply_text("Unauthorized account.")
        return

    welcome_text = (
        "🎵 **Welcome to Navidrome Music Bot!**\n\n"
        "You can manage your library using the **buttons below**, the **Menu button `[/]`**, or simply by sending messages.\n\n"
        "**📥 Download Music:**\n"
        "• **Song title** — Type any name (e.g. `Khalasi` or `Shape of You`) to search & pick\n"
        "• **Amazon Music** — Paste any playlist, album, or track link\n"
        "• **Spotify** — Paste any track, album, or playlist link\n"
        "• **YouTube / YT Music** — Paste any video or playlist link\n\n"
        "**⚡ Quick Actions:** Tap any button below or tap the Menu button `[/]`."
    )
    await update.message.reply_text(
        welcome_text,
        reply_markup=MAIN_REPLY_KEYBOARD,
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        "🎛️ **Quick Actions:**",
        reply_markup=HELP_INLINE_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in USER_DIR_MAP:
        await update.message.reply_text("Unauthorized account.")
        return

    help_text = (
        "📖 **Navidrome Music Bot Guide**\n\n"
        "**1. Quick Buttons (Bottom Keyboard & Menu `[/]`):**\n"
        "• 🎧 **Discover Song** (`/discover`) — Automatically picks 1 new song based on your listening history & downloads it to `downloads/`\n"
        "• 📊 **Library Status** (`/status`) — Shows song counts in `downloads/` vs permanent `library/`\n"
        "• 🧹 **Cleanup & Sync** (`/cleanup`) — Moves your ❤️/4-5⭐ tracks to `library/` and reviews 1⭐ or expired songs for deletion\n"
        "• ❓ **Help Guide** (`/help`) — Displays this menu\n\n"
        "**2. Two-Tier Library Lifecycle:**\n"
        "• **Staging (`downloads/`):** All new downloads and daily discoveries start here.\n"
        "• **Permanent (`library/`):** Star (❤️) or rate 4–5⭐ any song in your Navidrome player to automatically promote it to permanent storage.\n"
        "• **Safe Cleanup:** Rate a song 1⭐ or add it to a playlist named `Delete` to queue it for deletion (the bot will always ask you to confirm first!). Unplayed songs in `downloads/` older than 14 days are also reviewed."
    )
    await update.message.reply_text(
        help_text,
        reply_markup=MAIN_REPLY_KEYBOARD,
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        "🎛️ **One-Tap Actions:**",
        reply_markup=HELP_INLINE_KEYBOARD,
    )


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in USER_DIR_MAP:
        await update.message.reply_text("Unauthorized account.")
        return

    status_msg = await update.message.reply_text(
        "🎧 Analyzing your Navidrome listening history & finding a new track...",
        reply_markup=MAIN_REPLY_KEYBOARD,
    )
    loop = asyncio.get_running_loop()
    success, msg = await loop.run_in_executor(None, discover_new_song_sync, user_id)
    await status_msg.edit_text(msg, parse_mode="Markdown")


async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in USER_DIR_MAP:
        await update.message.reply_text("Unauthorized account.")
        return

    await update.message.reply_text(
        "🔄 Syncing `downloads/` ↔ `library/` and checking cleanup rules...",
        reply_markup=MAIN_REPLY_KEYBOARD,
        parse_mode="Markdown",
    )
    await run_user_lifecycle_and_notify(context.application, user_id, manual=True)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in USER_DIR_MAP:
        await update.message.reply_text("Unauthorized account.")
        return

    _, downloads_dir, library_dir = get_user_paths(user_id)
    dl_count = len(list_audio_files(downloads_dir))
    lib_count = len(list_audio_files(library_dir))
    m3u_count = len(list(downloads_dir.glob("*.m3u")))

    await update.message.reply_text(
        f"📊 **Your Library Status**\n\n"
        f"• 📥 **Staging (`downloads/`):** **{dl_count}** songs\n"
        f"• 🏛️ **Permanent (`library/`):** **{lib_count}** songs\n"
        f"• 📜 **Playlists (`.m3u`):** **{m3u_count}**\n\n"
        f"💡 _Tip: ❤️ Star or rate 4–5⭐ in Navidrome to promote to permanent `library/`.\n"
        f"⭐ Rate 1 star (or leave unplayed for {UNPLAYED_EXPIRY_DAYS}d) to queue for cleanup._",
        reply_markup=MAIN_REPLY_KEYBOARD,
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in USER_DIR_MAP:
        await update.message.reply_text(f"Unauthorized (ID: {user_id}).")
        return

    text = update.message.text.strip()

    # Route persistent keyboard button clicks
    if text == "🎧 Discover Song":
        await discover_command(update, context)
        return
    if text == "📊 Library Status":
        await status_command(update, context)
        return
    if text == "🧹 Cleanup & Sync":
        await cleanup_command(update, context)
        return
    if text in ("❓ Help Guide", "/help", "help"):
        await help_command(update, context)
        return

    match = URL_REGEX.search(text)

    # 1. Direct Links (Spotify, Amazon Music, YouTube)
    if match:
        clean_url = match.group(0)
        status_msg = await update.message.reply_text("⏳ Processing link... Please wait.", reply_markup=MAIN_REPLY_KEYBOARD)
        success, log = await download_direct_url_job(clean_url, user_id, status_msg=status_msg)
        if success:
            await status_msg.edit_text(f"✅ Done! {log}")
        else:
            await status_msg.edit_text(f"❌ Failed: {log}")
        return

    # 2. Text Search (Album/Playlist or Single Songs)
    lower_text = text.lower()
    is_album_query = any(k in lower_text for k in ["playlist", "album", "full album", "ost", "soundtrack"])

    # If user explicitly asked for an album/playlist, search collections first
    if is_album_query:
        status_msg = await update.message.reply_text(f"🔎 Searching for Album/Playlist '{text}'...", reply_markup=MAIN_REPLY_KEYBOARD)
        loop = asyncio.get_running_loop()
        collections = await loop.run_in_executor(None, search_albums_and_playlists, text, 4)

        if collections:
            context.user_data["collections"] = {c["id"]: c for c in collections}
            import html
            col_lines = [f"💿 <b>Albums / Playlists matching:</b> <i>{html.escape(text)}</i>\n"]
            col_keyboard = []
            for idx, c in enumerate(collections, 1):
                col_lines.append(f"<b>{idx}.</b> {html.escape(c['title'])} <i>({html.escape(c['artists'])})</i> [{c['type']}]")
                col_keyboard.append([
                    InlineKeyboardButton(f"⬇️ Download {c['type']} #{idx}: {c['title'][:25]}", callback_data=f"dl_col:{c['kind']}:{c['id']}")
                ])
            col_keyboard.append([InlineKeyboardButton("🎵 Search Single Songs Instead", callback_data=f"search_songs:{text[:30]}")])
            col_keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

            await status_msg.edit_text(
                "\n".join(col_lines),
                reply_markup=InlineKeyboardMarkup(col_keyboard),
                parse_mode="HTML"
            )
            return

    # 3. Standard Clean Studio Songs Search (Up to 10 Songs with Previews)
    status_msg = await update.message.reply_text(f"🔎 Searching YouTube Music for '{text}'...", reply_markup=MAIN_REPLY_KEYBOARD)
    loop = asyncio.get_running_loop()
    candidates = await loop.run_in_executor(None, search_youtube_candidates, text, 10)

    if not candidates:
        await status_msg.edit_text(f"❌ No clean studio tracks found for '{text}'.")
        return

    context.user_data["candidates"] = {c["id"]: c for c in candidates}

    import html
    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    msg_lines = [f"🔎 <b>Search Results for:</b> <i>{html.escape(text)}</i>\n"]
    keyboard = []
    dl_row1, dl_row2 = [], []
    prev_row1, prev_row2 = [], []

    for idx, c in enumerate(candidates):
        num = number_emojis[idx] if idx < len(number_emojis) else f"#{idx+1}"
        title_esc = html.escape(c["title"])
        singers = c.get("artists") or c.get("channel") or "Unknown Artist"
        singers_esc = html.escape(singers)
        dur_str = f" • ⏱️ {html.escape(c['duration'])}" if c.get("duration") else ""
        album_str = f" • 💿 <i>{html.escape(c['album'])}</i>" if c.get("album") else ""

        msg_lines.append(f"{num} <b>{title_esc}</b>\n   🎤 <i>{singers_esc}</i>{dur_str}{album_str}\n")

        # Download button
        target_dl = dl_row1 if idx < 5 else dl_row2
        target_dl.append(InlineKeyboardButton(f"⬇️ {num}", callback_data=f"dl:{c['id']}"))

        # Preview button
        target_prev = prev_row1 if idx < 5 else prev_row2
        target_prev.append(InlineKeyboardButton(f"🎧 {num}", callback_data=f"prev:{c['id']}"))

    msg_lines.append("<i>Tap ⬇️ to download full song, or 🎧 to hear a 30s preview first!</i>")

    keyboard.append(dl_row1)
    if dl_row2:
        keyboard.append(dl_row2)
    keyboard.append(prev_row1)
    if prev_row2:
        keyboard.append(prev_row2)
    keyboard.append([InlineKeyboardButton("❌ Cancel Search", callback_data="cancel")])

    await status_msg.edit_text(
        "\n".join(msg_lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if user_id not in USER_DIR_MAP:
        await query.edit_message_text("Unauthorized account.")
        return

    data = query.data
    user_root, downloads_dir, library_dir = get_user_paths(user_id)

    # --- Quick Action Inline Buttons ---
    if data == "cmd:discover":
        loop = asyncio.get_running_loop()
        await query.message.reply_text("🎧 Analyzing your Navidrome listening history & finding a new track...")
        success, msg = await loop.run_in_executor(None, discover_new_song_sync, user_id)
        await query.message.reply_text(msg, parse_mode="Markdown")
        return

    if data == "cmd:status":
        dl_count = len(list_audio_files(downloads_dir))
        lib_count = len(list_audio_files(library_dir))
        m3u_count = len(list(downloads_dir.glob("*.m3u")))
        await query.message.reply_text(
            f"📊 **Your Library Status**\n\n"
            f"• 📥 **Staging (`downloads/`):** **{dl_count}** songs\n"
            f"• 🏛️ **Permanent (`library/`):** **{lib_count}** songs\n"
            f"• 📜 **Playlists (`.m3u`):** **{m3u_count}**\n\n"
            f"💡 _Tip: ❤️ Star or rate 4–5⭐ in Navidrome to promote to permanent `library/`._",
            parse_mode="Markdown",
        )
        return

    if data == "cmd:cleanup":
        await query.message.reply_text("🔄 Syncing `downloads/` ↔ `library/` and checking cleanup rules...", parse_mode="Markdown")
        await run_user_lifecycle_and_notify(context.application, user_id, manual=True)
        return

    if data == "cmd:help":
        await help_command(update, context)
        return

    # --- Search Single Songs Fallback from Album view ---
    if data.startswith("search_songs:"):
        search_query = data.split(":", 1)[1]
        loop = asyncio.get_running_loop()
        candidates = await loop.run_in_executor(None, search_youtube_candidates, search_query, 10)
        if not candidates:
            await query.edit_message_text(f"❌ No clean studio tracks found for '{search_query}'.")
            return
        context.user_data["candidates"] = {c["id"]: c for c in candidates}
        import html
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        msg_lines = [f"🔎 <b>Search Results for:</b> <i>{html.escape(search_query)}</i>\n"]
        keyboard, dl1, dl2, pr1, pr2 = [], [], [], [], []
        for idx, c in enumerate(candidates):
            num = number_emojis[idx] if idx < len(number_emojis) else f"#{idx+1}"
            title_esc = html.escape(c["title"])
            singers = c.get("artists") or c.get("channel") or "Unknown Artist"
            dur_str = f" • ⏱️ {html.escape(c['duration'])}" if c.get("duration") else ""
            msg_lines.append(f"{num} <b>{title_esc}</b>\n   🎤 <i>{html.escape(singers)}</i>{dur_str}\n")
            target_dl = dl1 if idx < 5 else dl2
            target_dl.append(InlineKeyboardButton(f"⬇️ {num}", callback_data=f"dl:{c['id']}"))
            target_pr = pr1 if idx < 5 else pr2
            target_pr.append(InlineKeyboardButton(f"🎧 {num}", callback_data=f"prev:{c['id']}"))
        keyboard.append(dl1)
        if dl2: keyboard.append(dl2)
        keyboard.append(pr1)
        if pr2: keyboard.append(pr2)
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.edit_message_text("\n".join(msg_lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # --- Album / Playlist Download Selection ---
    if data.startswith("dl_col:"):
        _, kind, browse_id = data.split(":", 2)
        status_msg = await query.message.reply_text("⏳ Preparing album download...", reply_markup=MAIN_REPLY_KEYBOARD)
        result = await download_album_or_playlist_job(kind, browse_id, user_id, status_msg)
        await status_msg.edit_text(result, parse_mode="HTML")
        return

    # --- 30s Audio Preview Callback ---
    if data.startswith("prev:"):
        video_id = data.split(":", 1)[1]
        candidates = context.user_data.get("candidates", {})
        cand_info = candidates.get(video_id, {})
        title = cand_info.get("title", "Song Preview")
        singers = cand_info.get("artists") or cand_info.get("channel") or "Unknown Artist"

        import html
        title_esc = html.escape(title)
        singers_esc = html.escape(singers)

        status_msg = await query.message.reply_text(f"⏳ Generating 30s preview for <b>{title_esc}</b>...", parse_mode="HTML")
        loop = asyncio.get_running_loop()
        preview_file = await loop.run_in_executor(None, extract_audio_preview, video_id)

        if preview_file and preview_file.exists():
            await status_msg.delete()
            try:
                with open(preview_file, "rb") as audio_fh:
                    await context.bot.send_audio(
                        chat_id=user_id,
                        audio=audio_fh,
                        title=f"Sample: {title[:50]}",
                        performer=singers[:50],
                        duration=30,
                        caption=(
                            f"🎧 <b>Preview (30s):</b> <b>{title_esc}</b>\n"
                            f"🎤 <i>{singers_esc}</i>\n\n"
                            f"<i>Tap below if you want to download the complete song!</i>"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⬇️ Download Full Song to Library", callback_data=f"dl:{video_id}")]
                        ])
                    )
            finally:
                preview_file.unlink(missing_ok=True)
        else:
            await status_msg.edit_text(f"⚠️ Could not generate audio preview for <b>{title_esc}</b>.", parse_mode="HTML")
        return

    # --- Track Download Selection ---
    if data == "cancel":
        await query.edit_message_text("Download canceled.")
        return

    if data.startswith("dl:"):
        video_id = data.split(":", 1)[1]
        candidates = context.user_data.get("candidates", {})
        cand_info = candidates.get(video_id, {})
        title = cand_info.get("title", "Selected Track")
        singers = cand_info.get("artists") or cand_info.get("channel") or ""
        dur = cand_info.get("duration", "")
        album = cand_info.get("album", "")

        import html
        title_esc = html.escape(title)
        singers_esc = html.escape(singers)

        await query.edit_message_text(
            f"⬇️ Downloading: <b>{title_esc}</b> by <i>{singers_esc}</i>...",
            parse_mode="HTML"
        )
        loop = asyncio.get_running_loop()
        success, log, new_files = await loop.run_in_executor(None, download_specific_track, video_id, downloads_dir)

        if success and new_files:
            dur_line = f"⏱️ <b>Duration:</b> {html.escape(dur)}\n" if dur else ""
            album_line = f"💿 <b>Album:</b> <i>{html.escape(album)}</i>\n" if album else ""
            await query.edit_message_text(
                f"✅ <b>Downloaded Successfully!</b>\n\n"
                f"🎵 <b>Song:</b> <b>{title_esc}</b>\n"
                f"🎤 <b>Singer(s):</b> <i>{singers_esc}</i>\n"
                f"{dur_line}{album_line}"
                f"📂 Saved to <code>downloads/{html.escape(new_files[0])}</code>\n\n"
                f"💡 <i>Tip: ❤️ Star it in Navidrome to move to permanent <code>library/</code>.</i>",
                parse_mode="HTML"
            )
        elif success:
            await query.edit_message_text(
                f"✅ <b>{title_esc}</b> by <i>{singers_esc}</i> already exists in your library or downloads.",
                parse_mode="HTML"
            )
        else:
            await query.edit_message_text(
                f"❌ Failed to download <b>{title_esc}</b>:\n<code>{html.escape(log)}</code>",
                parse_mode="HTML"
            )
        return

    # --- Cleanup Confirmation Callbacks ---
    pending = context.application.bot_data.get("cleanup_Restore", {}).get(user_id, [])

    if data == "ccancel":
        context.application.bot_data.get("cleanup_Restore", {}).pop(user_id, None)
        await query.edit_message_text("👍 Cleanup skipped. No files were deleted.")
        return

    if data == "cdel_all":
        deleted_count = 0
        for item in pending:
            p: Path = item["path"]
            if p.exists():
                remove_from_m3u_playlists(user_root, downloads_dir, p.name)
                p.unlink(missing_ok=True)
                deleted_count += 1
        context.application.bot_data.get("cleanup_Restore", {}).pop(user_id, None)
        await query.edit_message_text(f"🗑️ Deleted {deleted_count} song(s) and updated playlists.")
        return

    if data.startswith("cdel:"):
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(pending):
            item = pending[idx]
            p: Path = item["path"]
            if p.exists():
                remove_from_m3u_playlists(user_root, downloads_dir, p.name)
                p.unlink(missing_ok=True)
                await query.message.reply_text(f"🗑️ Deleted: **{item['label']}**", parse_mode="Markdown")
            else:
                await query.message.reply_text(f"Already deleted: {item['label']}")
        return

    if data.startswith("ckeep:"):
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(pending):
            item = pending[idx]
            p: Path = item["path"]
            if p.exists() and p.parent == downloads_dir:
                dest = library_dir / p.name
                shutil.move(str(p), str(dest))
                update_m3u_references_on_move(user_root, downloads_dir, p.name, f"../library/{p.name}")
                await query.message.reply_text(
                    f"❤️ Moved **{item['label']}** to permanent `library/`!",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text(f"❤️ Kept **{item['label']}**.", parse_mode="Markdown")
        return


# --- Daily Automated Background Worker ---
async def daily_background_worker(app: Application):
    """Runs every 24h: promotes Starred tracks, checks cleanup, and adds 1 discovery track/day."""
    await asyncio.sleep(30)  # Initial warmup delay after container boot
    while True:
        logger.info("[Scheduler] Waiting for next daily cycle...")
        await asyncio.sleep(DAILY_INTERVAL_SECONDS)
        logger.info("[Scheduler] Running daily discovery & library lifecycle job...")
        loop = asyncio.get_running_loop()
        for uid in USER_DIR_MAP:
            try:
                await run_user_lifecycle_and_notify(app, uid, manual=False)
                ok, msg = await loop.run_in_executor(None, discover_new_song_sync, uid)
                if ok:
                    await app.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"[Scheduler] Error for user {uid}: {e}")


async def post_init(app: Application):
    for uid in USER_DIR_MAP:
        get_user_paths(uid)

    # Register Bot Commands with Telegram API so the native [/] Menu button appears
    commands = [
        BotCommand("discover", "🎧 Discover a new song based on your taste"),
        BotCommand("cleanup", "🧹 Sync library & review songs for cleanup"),
        BotCommand("status", "📊 View downloads vs library stats"),
        BotCommand("help", "❓ View help & command guide"),
        BotCommand("start", "🚀 Open main menu & quick action buttons"),
    ]
    try:
        await app.bot.set_my_commands(commands)
        logger.info("Successfully registered Telegram Bot Commands with Telegram API.")
    except Exception as e:
        logger.warning(f"Could not register Bot Commands: {e}")

    asyncio.create_task(daily_background_worker(app))
    logger.info("Daily discovery & cleanup background scheduler initialized.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("discover", discover_command))
    app.add_handler(CommandHandler("cleanup", cleanup_command))
    app.add_handler(CommandHandler("sync", cleanup_command))
    app.add_handler(CommandHandler("status", status_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_button))

    logger.info("Bot online with Spotify, YouTube, Amazon Skill API, Discovery, Lifecycle & Telegram Menu.")
    app.run_polling()


if __name__ == "__main__":
    main()