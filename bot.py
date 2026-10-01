import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
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
            import json
            data = json.loads(p.read_text(encoding="utf-8"))
            p.unlink(missing_ok=True)
            if isinstance(data, list) and data and "playlist_name" in data[0]:
                return data[0]["playlist_name"]
    except Exception as e:
        logger.warning(f"Could not extract Spotify playlist title: {e}")
    return "Spotify Playlist"


async def download_worker(url: str, user_id: int, status_msg) -> str:
    """Processes music downloads for Spotify or YouTube URLs."""
    downloads_dir, library_dir, user_root = get_user_paths(user_id)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    library_dir.mkdir(parents=True, exist_ok=True)

    is_spotify = "spotify.com" in url
    is_playlist = ("playlist" in url) or ("album" in url) or ("list=" in url)

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
        "Send me a Spotify, YouTube, or YouTube Music link to download it directly to your music server."
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