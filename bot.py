import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Set up logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Parse user configurations: TELEGRAM_USER_ID:DOWNLOAD_DIR
# Example: 12345678:/music/user1/downloads,87654321:/music/user2/downloads
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

USER_CONFIGS: dict[int, Path] = {}
if ALLOWED_USERS_RAW:
    for entry in ALLOWED_USERS_RAW.split(","):
        entry = entry.strip()
        if ":" in entry:
            uid_str, path_str = entry.split(":", 1)
            try:
                USER_CONFIGS[int(uid_str.strip())] = Path(path_str.strip())
            except ValueError:
                logger.error(f"Invalid user ID in ALLOWED_USERS entry: {entry}")

if not USER_CONFIGS:
    logger.warning("No ALLOWED_USERS configured! Bot will reject all requests.")
else:
    logger.info(f"Loaded {len(USER_CONFIGS)} authorized user(s).")

# Allowed audio file extensions
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"}


def get_user_paths(user_id: int) -> tuple[Path, Path, Path]:
    """Returns (downloads_dir, library_dir, user_root) for a user."""
    downloads_dir = USER_CONFIGS.get(user_id)
    if not downloads_dir:
        raise ValueError(f"User {user_id} not authorized")
    user_root = downloads_dir.parent
    library_dir = user_root / "library"
    return downloads_dir, library_dir, user_root


def list_audio_files(directory: Path) -> set[Path]:
    """Returns a set of all audio file paths currently existing in directory."""
    if not directory.exists():
        return set()
    return {
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    }


def sanitize_filename(name: str) -> str:
    """Sanitizes strings for safe filename usage."""
    return re.sub(r'[/\\:*?"<>|]', "_", name).strip()


def normalize_text(text: str) -> str:
    """Lowercases and strips special chars for lenient comparison."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def create_m3u_playlist(playlist_name: str, target_dir: Path, audio_entries: list[str]):
    """Creates or overwrites an .m3u playlist in target_dir using UTF-8 encoding."""
    if not audio_entries:
        return
    safe_name = sanitize_filename(playlist_name)
    m3u_path = target_dir / f"{safe_name}.m3u"
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for entry in audio_entries:
            f.write(f"{entry}\n")
    logger.info(f"Generated playlist at: {m3u_path} with {len(audio_entries)} entries.")


def get_spotify_playlist_title(url: str) -> str:
    """Fetches the playlist name using spotdl metadata extraction."""
    try:
        cmd = ["spotdl", "save", url, "--save-file", "/tmp/temp_meta.spotdl"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        p = Path("/tmp/temp_meta.spotdl")
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            p.unlink(missing_ok=True)
            if isinstance(data, list) and data and "playlist_name" in data[0]:
                return data[0]["playlist_name"]
    except Exception as e:
        logger.warning(f"Could not extract Spotify playlist title: {e}")
    return "Spotify Playlist"


async def scrape_amazon_music_playlist(url: str) -> tuple[str, list[dict]]:
    """
    Launches headless Chromium via Playwright, navigates to the Amazon Music URL,
    and intercepts API responses from skill.music.a2z.com/api/ to extract the exact
    playlist title and complete tracklist.
    """
    logger.info(f"[Amazon] Starting headless Chromium to scrape: {url}")
    tracks: list[dict] = []
    playlist_title = "Amazon Music Playlist"
    captured_data: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        async def handle_response(response):
            if "skill.music.a2z.com/api" in response.url:
                try:
                    text = await response.text()
                    if "showHome" in response.url or "showLibraryPlaylist" in response.url or "showPlaylist" in response.url:
                        data = json.loads(text)
                        captured_data.append(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            for _ in range(20):
                if captured_data:
                    break
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"[Amazon] Page navigation timeout or error: {e}")
        finally:
            await browser.close()

    for item in captured_data:
        try:
            if "header" in item:
                header = item["header"]
                t = header.get("title") or header.get("headerTitle")
                if t and isinstance(t, str):
                    playlist_title = t.strip()

            def extract_tracks_recursive(obj):
                if isinstance(obj, dict):
                    if ("primaryText" in obj or "title" in obj) and ("secondaryText" in obj or "artist" in obj):
                        title = obj.get("primaryText") or obj.get("title")
                        artist = obj.get("secondaryText") or obj.get("artist")
                        if isinstance(title, str) and isinstance(artist, str) and len(title) > 1:
                            if not any(t["title"] == title and t["artist"] == artist for t in tracks):
                                tracks.append({"title": title.strip(), "artist": artist.strip()})
                    for v in obj.values():
                        extract_tracks_recursive(v)
                elif isinstance(obj, list):
                    for elem in obj:
                        extract_tracks_recursive(elem)

            extract_tracks_recursive(item)
        except Exception as e:
            logger.warning(f"[Amazon] Failed to parse captured payload: {e}")

    logger.info(f"[Amazon] Extracted '{playlist_title}' with {len(tracks)} track(s).")
    return playlist_title, tracks


def find_existing_track_for_query(query: str, downloads_dir: Path, library_dir: Path) -> str | None:
    """Checks if an audio file matching query already exists in downloads/ or library/."""
    q_norm = normalize_text(query)
    for d, prefix in [(downloads_dir, ""), (library_dir, "../library/")]:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                f_norm = normalize_text(f.stem)
                if q_norm in f_norm or f_norm in q_norm:
                    rel_path = f.relative_to(d).as_posix()
                    return f"{prefix}{rel_path}" if prefix else rel_path
    return None


def search_youtube_candidates(query: str, limit: int = 4) -> list[dict]:
    """Uses yt-dlp to search YouTube Music/YouTube for queries."""
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--default-search", "ytsearch",
        f"ytsearch{limit}:{query} audio",
        "--flat-playlist",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode != 0:
            return []
        candidates = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    candidates.append(json.loads(line))
                except Exception:
                    pass
        return candidates
    except Exception as e:
        logger.error(f"Search failed for '{query}': {e}")
        return []


def download_specific_track(video_id: str, target_dir: Path) -> tuple[bool, str, list[str]]:
    """Downloads a YouTube video as an mp3 using yt-dlp."""
    target_dir.mkdir(parents=True, exist_ok=True)
    before_files = list_audio_files(target_dir)
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--add-metadata",
        "--embed-thumbnail",
        "-o", str(target_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
        yt_url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        after_files = list_audio_files(target_dir)
        new_files = [f.name for f in (after_files - before_files)]
        if new_files:
            return True, new_files[0], new_files
        return proc.returncode == 0, "", []
    except Exception as e:
        logger.error(f"Error downloading {video_id}: {e}")
        return False, str(e), []


def download_ytsearch_single(track_query: str, target_dir: Path) -> list[str]:
    """Directly searches and downloads the best YouTube match for track_query."""
    target_dir.mkdir(parents=True, exist_ok=True)
    before_files = list_audio_files(target_dir)
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--add-metadata",
        "--embed-thumbnail",
        "--default-search", "ytsearch",
        "-o", str(target_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
        f"ytsearch1:{track_query}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        after_files = list_audio_files(target_dir)
        return [f.name for f in (after_files - before_files)]
    except Exception as e:
        logger.error(f"Failed to download single track for '{track_query}': {e}")
        return []


async def download_worker(url: str, user_id: int, status_msg) -> str:
    """Processes music downloads for Spotify, YouTube, or Amazon Music URLs."""
    downloads_dir, library_dir, user_root = get_user_paths(user_id)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    library_dir.mkdir(parents=True, exist_ok=True)

    is_spotify = "spotify.com" in url
    is_amazon = "music.amazon" in url or "amazon.com" in url
    is_playlist = ("playlist" in url) or ("album" in url) or ("list=" in url)

    # 1. Amazon Music Playlist via Playwright Scraper
    if is_amazon:
        await status_msg.edit_text("🔍 Launching headless browser to parse Amazon Music playlist...")
        playlist_title, amazon_tracks = await scrape_amazon_music_playlist(url)

        if not amazon_tracks:
            return (
                "❌ Could not extract any tracks from Amazon Music.\n"
                "Please verify the playlist is public or shared."
            )

        total_tracks = len(amazon_tracks)
        await status_msg.edit_text(
            f"📋 <b>Found '{playlist_title}'</b> ({total_tracks} tracks).\n"
            f"⚡ Downloading tracks into <code>downloads/</code>..."
        )

        m3u_entries: list[str] = []
        new_downloads_count = 0

        for idx, t in enumerate(amazon_tracks, 1):
            q = f"{t['artist']} - {t['title']}"
            existing = find_existing_track_for_query(q, downloads_dir, library_dir)
            if existing:
                m3u_entries.append(existing)
                continue

            if idx % 3 == 0:
                try:
                    await status_msg.edit_text(
                        f"⏳ Downloading '{playlist_title}': <b>{idx}/{total_tracks}</b> tracks...\n"
                        f"Current: <i>{q}</i>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            cands = search_youtube_candidates(q, limit=2)
            downloaded = False
            if cands:
                vid = cands[0].get("id")
                if vid:
                    ok, fname, new_f = download_specific_track(vid, downloads_dir)
                    if ok and fname:
                        m3u_entries.append(fname)
                        new_downloads_count += len(new_f)
                        downloaded = True

            if not downloaded:
                new_f = download_ytsearch_single(q, downloads_dir)
                if new_f:
                    m3u_entries.append(new_f[0])
                    new_downloads_count += len(new_f)

        create_m3u_playlist(playlist_title, downloads_dir, m3u_entries)
        return (
            f"✅ <b>Amazon Playlist Imported!</b>\n\n"
            f"📁 <b>Playlist:</b> {playlist_title}\n"
            f"🎵 <b>Total tracks:</b> {len(m3u_entries)} / {total_tracks}\n"
            f"📥 <b>Newly downloaded:</b> {new_downloads_count}\n"
            f"📄 Playlist saved to <code>downloads/{sanitize_filename(playlist_title)}.m3u</code>"
        )

    # 2. Spotify or YouTube
    before_files = list_audio_files(downloads_dir)
    await status_msg.edit_text(f"⏳ Downloading from {'Spotify' if is_spotify else 'YouTube'}...")

    if is_spotify:
        cmd = [
            "spotdl", "download", url,
            "--output", str(downloads_dir / "{artist} - {title}.{output-ext}"),
        ]
    else:
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--add-metadata",
            "--embed-thumbnail",
            "-o", str(downloads_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
            url,
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    after_files = list_audio_files(downloads_dir)
    new_files = list(after_files - before_files)

    if proc.returncode != 0 and not new_files:
        err_excerpt = (stderr.decode(errors="ignore") or stdout.decode(errors="ignore"))[-300:]
        logger.error(f"Download failed: {err_excerpt}")
        return f"❌ Download failed:\n<code>{err_excerpt}</code>"

    if is_playlist and new_files:
        playlist_name = "Playlist"
        if is_spotify:
            playlist_name = get_spotify_playlist_title(url)
        create_m3u_playlist(playlist_name, downloads_dir, [f.name for f in new_files])

    count = len(new_files)
    if count == 0:
        return "⚠️ Completed, but no new audio files were detected (might already exist)."
    elif count == 1:
        return f"✅ Successfully downloaded:\n<b>{new_files[0].name}</b>"
    else:
        return f"✅ Successfully downloaded <b>{count}</b> tracks into <code>downloads/</code>."


async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming URL messages."""
    user = update.effective_user
    if not user or user.id not in USER_CONFIGS:
        if update.message:
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    text = update.message.text.strip() if update.message and update.message.text else ""
    if not (text.startswith("http://") or text.startswith("https://")):
        return

    status_msg = await update.message.reply_text("🔍 Processing request...")
    result = await download_worker(text, user.id, status_msg)
    await status_msg.edit_text(result, parse_mode="HTML")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command."""
    await update.message.reply_text(
        "👋 Welcome to Navidrome Music Bot!\n"
        "Send me a Spotify, YouTube, or Amazon Music playlist link to download it directly to your music server."
    )


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set! Exiting.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_message))

    logger.info("Bot online and polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()