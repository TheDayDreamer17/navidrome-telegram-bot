import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from ytmusicapi import YTMusic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Replacer")

NAVIDROME_DB_PATH = Path(os.getenv("NAVIDROME_DB_PATH", "/navidrome_data/navidrome.db"))
DIMPAL_DOWNLOADS = Path("/music/dimpal/downloads")
BACKUP_DIR = Path("/tmp/dimpal_backup")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

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
    r"\bnon\s*stop\b",
    r"\bjukebox\b",
    r"\bcompilation\b",
    r"\blive\s+(performance|concert|stage|show|recording|garba|video|session|version)\b",
    r"\b(live\s+at|live\s+in)\b",
    r"\bconcert\s+version\b",
    r"\bstage\s+show\b",
    r"\bground\s+garba\b",
    r"\bnavratri\s+live\b",
]

CHANNELS_TO_STRIP = [
    r"T-Series Apna Punjab",
    r"T-Series",
    r"Sony Music India",
    r"Sony Music Malayalam",
    r"SonyMusicIndiaVEVO",
    r"DarshanRavalVEVO",
    r"Saregama Music",
    r"Saregama Telugu",
    r"Zee Music",
    r"Tips",
    r"White Hill Dhaakad",
    r"Universal Records",
    r"Artiste First",
    r"KaanPhod Music",
    r"TheLyricStudio",
    r"desibeats",
    r"Vibe Bird",
    r"Kappa TV",
    r"Mr\. BALI",
]


def clean_display_title(title: str) -> str:
    cleaned = re.sub(r'[\(\[\{]\s*(?:official\s+)?audio(?:\s+song)?\s*[\)\]\}]', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(r'\|\s*(?:official\s+)?audio(?:\s+song)?', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def is_clean_studio_track(title: str, artists: str = "") -> bool:
    combined = f"{title.lower()} {artists.lower()}"
    for pattern in BAD_TITLE_PATTERNS:
        if re.search(pattern, combined):
            return False
    return True


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def extract_clean_search_query(filename: str, current_artist: str, current_title: str) -> str:
    stem = filename.replace(".mp3", "")
    for ch in CHANNELS_TO_STRIP:
        stem = re.sub(rf"^{ch}\s*[-–—:|]\s*", "", stem, flags=re.IGNORECASE)

    # Remove generic video tags
    stem = re.sub(r"[\(\[\{].*?(?:video|lyric|lyrics|official|audio|full|hustle|season|hd|mv|version|starring|ft\.|feat).*?[\)\]\}]", "", stem, flags=re.IGNORECASE)

    # Split on pipe, colon, em-dash, star
    parts = re.split(r"[｜|:：★]", stem)
    clean_q = parts[0].strip()
    if len(parts) > 1 and len(clean_q) < 18:
        second = parts[1].strip()
        # Only take second part if not an actor/director credit
        if not re.search(r"\b(ranveer|ritwik|shreya|hrithik|emraan|randeep|sunny|akshay|vidya|nani|sai)\b", second, flags=re.I):
            clean_q = f"{clean_q} {second}"

    clean_q = re.sub(r"\b(full video|official video|music video|lyric video|lyrics|video song|song|mv|hd)\b", "", clean_q, flags=re.IGNORECASE)
    clean_q = re.sub(r"\s+", " ", clean_q).strip(" -_")
    return clean_q


def resolve_best_studio_track(query: str, yt: YTMusic) -> tuple[str | None, str, str, str, str]:
    """Resolves query to YouTube Music official ATV studio master."""
    clean_q = re.sub(r"[\(\[\{].*?[\)\]\}]", "", query).strip()
    try:
        results = yt.search(clean_q or query, filter="songs", limit=15)
        # 1. Strictly prioritize Official Audio Track (ATV)
        for r in results:
            vid = r.get("videoId")
            title = r.get("title") or ""
            artists = ", ".join([a["name"] for a in r.get("artists", [])]) if r.get("artists") else ""
            dur = r.get("duration") or ""
            album = r.get("album", {}).get("name") if r.get("album") else ""
            if vid and r.get("videoType") == "MUSIC_VIDEO_TYPE_ATV" and is_clean_studio_track(title, artists):
                return vid, title, artists, album, dur

        # 2. Clean studio track fallback
        for r in results:
            vid = r.get("videoId")
            title = r.get("title") or ""
            artists = ", ".join([a["name"] for a in r.get("artists", [])]) if r.get("artists") else ""
            dur = r.get("duration") or ""
            album = r.get("album", {}).get("name") if r.get("album") else ""
            if vid and is_clean_studio_track(title, artists):
                return vid, title, artists, album, dur
    except Exception as e:
        logger.warning(f"Error resolving studio track for '{query}': {e}")
    return None, "", "", "", ""


def download_and_tag(video_id: str, out_path: Path) -> bool:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "--extractor-args", "youtube:player_client=android,web",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--embed-metadata",
        "-o", str(out_path),
        url,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 100000


def update_m3u_playlists(folder: Path, old_name: str, new_name: str):
    for m3u in folder.glob("*.m3u"):
        try:
            lines = m3u.read_text(encoding="utf-8").splitlines()
            updated = []
            changed = False
            for line in lines:
                stripped = line.strip()
                if stripped == old_name or stripped.endswith(f"/{old_name}"):
                    updated.append(new_name if folder == DIMPAL_DOWNLOADS else f"downloads/{new_name}")
                    changed = True
                else:
                    updated.append(line)
            if changed:
                m3u.write_text("\n".join(updated) + "\n", encoding="utf-8")
                logger.info(f"Updated reference in {m3u.name}: {old_name} -> {new_name}")
        except Exception as e:
            logger.warning(f"Could not update {m3u}: {e}")


def main():
    if not NAVIDROME_DB_PATH.exists():
        logger.error("navidrome.db not found!")
        return

    conn = sqlite3.connect(f"file:{NAVIDROME_DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, path, title, artist, duration
        FROM media_file
        WHERE library_id = 3 AND missing = 0 AND path LIKE 'downloads/%'
        """
    ).fetchall()
    conn.close()

    yt = YTMusic()
    targets = []
    video_indicators = [
        "lyric video", "lyrics", "official video", "music video", "full video",
        "video song", "hustle", "mv", "season 3", "extended mix"
    ]

    for r in rows:
        path, title, artist = r[1], r[2] or "", r[3] or ""
        fname = Path(path).name
        comb = f"{path} {title} {artist}".lower()
        if any(ind in comb for ind in video_indicators):
            targets.append((fname, artist, title, r[4]))

    logger.info(f"Found {len(targets)} candidate tracks to replace with clean studio masters.")

    success_count = 0
    skipped_count = 0

    for idx, (old_fname, old_artist, old_title, old_dur) in enumerate(targets, 1):
        old_path = DIMPAL_DOWNLOADS / old_fname
        if not old_path.exists():
            logger.warning(f"[{idx}/{len(targets)}] File not found on disk: {old_fname}")
            skipped_count += 1
            continue

        query = extract_clean_search_query(old_fname, old_artist, old_title)
        logger.info(f"\n[{idx}/{len(targets)}] Processing: {old_fname}")
        logger.info(f" -> Query: '{query}'")

        vid, new_title, new_artist, new_album, new_dur = resolve_best_studio_track(query, yt)
        if not vid:
            logger.warning(f" -> Could not resolve clean studio track for '{query}'. Skipping.")
            skipped_count += 1
            continue

        logger.info(f" -> Matched Studio Master: '{new_artist} - {new_title}' ({new_dur}) [VID: {vid}]")

        clean_filename = sanitize_filename(f"{new_artist} - {new_title}.mp3")
        target_path = DIMPAL_DOWNLOADS / clean_filename
        tmp_target = Path(f"/tmp/temp_dl_{vid}.mp3")
        tmp_target.unlink(missing_ok=True)

        ok = download_and_tag(vid, tmp_target)
        if not ok:
            logger.error(f" -> Download failed for {vid}")
            skipped_count += 1
            continue

        # Backup old file
        backup_dest = BACKUP_DIR / old_fname
        shutil.copy2(str(old_path), str(backup_dest))

        # Atomic replace or new file
        if target_path == old_path:
            shutil.move(str(tmp_target), str(target_path))
        else:
            old_path.unlink(missing_ok=True)
            shutil.move(str(tmp_target), str(target_path))
            update_m3u_playlists(DIMPAL_DOWNLOADS, old_fname, clean_filename)
            update_m3u_playlists(DIMPAL_DOWNLOADS.parent, old_fname, clean_filename)

        success_count += 1
        logger.info(f" -> Successfully replaced with clean master: {clean_filename}")
        time.sleep(1)

    logger.info(f"\n==========================================")
    logger.info(f"Completed! Successfully replaced: {success_count}/{len(targets)}, Skipped: {skipped_count}")
    logger.info(f"Old file backups preserved at: {BACKUP_DIR}")


if __name__ == "__main__":
    main()

