"""Animehighbo: safe publisher for explicitly authorized direct media feeds.

Only direct video links on configured allow-listed hosts are downloaded.
Torrent pages, HTML pages, magnets, and unapproved hosts are rejected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

LOG = logging.getLogger("animehighbo")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".webm", ".mov"}
USER_AGENT = "AnimehighboPublisher/1.0 (+authorized-media-only)"


def load_config(path: str = "config.json") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise RuntimeError(
            "config.json is missing. Copy config.example.json to config.json "
            "and add feeds and host allow-lists for content you may republish."
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config.get("feeds"), list):
        raise ValueError("'feeds' must be a list")
    if not isinstance(config.get("allowed_download_hosts"), list):
        raise ValueError("'allowed_download_hosts' must be a list")
    config.setdefault("max_file_size_mb", 45)
    config.setdefault("download_dir", "downloads")
    config.setdefault("state_file", "data/state.json")
    config.setdefault("dry_run", True)
    config.setdefault("mode", "links")
    config.setdefault("poll_limit_per_feed", 30)
    config.setdefault("request_timeout_seconds", 30)
    return config


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str, limit: int = 30) -> list[dict]:
    """Parse common RSS and Atom formats, preferring explicit media enclosures."""
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter():
        if local_name(node.tag) not in {"item", "entry"}:
            continue
        title = ""
        page_link = ""
        media_link = ""
        for child in list(node):
            name = local_name(child.tag)
            if name == "title" and not title:
                title = "".join(child.itertext()).strip()
            elif name == "enclosure" and not media_link:
                media_link = child.attrib.get("url", "").strip()
            elif name in {"content", "thumbnail"} and not media_link:
                candidate = child.attrib.get("url", "").strip()
                if Path(urlparse(candidate).path).suffix.lower() in VIDEO_EXTENSIONS:
                    media_link = candidate
            elif name == "link":
                href = child.attrib.get("href", "").strip()
                rel = child.attrib.get("rel", "alternate").lower()
                candidate = href or (child.text or "").strip()
                if candidate and rel == "enclosure" and not media_link:
                    media_link = candidate
                elif candidate and not page_link:
                    page_link = candidate
        link = media_link or page_link
        if title and link:
            items.append({"title": title, "url": link})
        if len(items) >= limit:
            break
    return items


def episode_details(title: str) -> tuple[str, str]:
    """Return a readable anime title and an episode label without guessing."""
    patterns = [
        r"(?i)\b(?:episode|ep)\s*[-.#:]?\s*(\d{1,4}(?:\.\d+)?)\b",
        r"第\s*(\d{1,4}(?:\.\d+)?)\s*[話话集期]",
        r"(?i)\bS\d{1,2}\s*E(\d{1,4})\b",
    ]
    episode = ""
    clean = title.strip()
    for pattern in patterns:
        match = re.search(pattern, clean)
        if match:
            episode = match.group(1)
            clean = re.sub(pattern, " ", clean).strip(" -|:[]()")
            break
    return (re.sub(r"\s+", " ", clean) or title.strip(), episode)


def parse_episode_feed(xml_text: str, limit: int = 30) -> list[dict]:
    """Parse RSS/Atom episode pages and poster image URLs, not video enclosures."""
    root = ET.fromstring(xml_text)
    entries = []
    for node in root.iter():
        if local_name(node.tag) not in {"item", "entry"}:
            continue
        title, page_url, image_url = "", "", ""
        for child in list(node):
            name = local_name(child.tag)
            if name == "title" and not title:
                title = "".join(child.itertext()).strip()
            elif name == "link":
                candidate = (child.attrib.get("href") or child.text or "").strip()
                rel = child.attrib.get("rel", "alternate").lower()
                if candidate and rel == "alternate" and not page_url:
                    page_url = candidate
                elif candidate and rel == "enclosure":
                    kind = child.attrib.get("type", "").lower()
                    if kind.startswith("image/") and not image_url:
                        image_url = candidate
            elif name in {"thumbnail", "image"} and not image_url:
                image_url = child.attrib.get("url", "").strip()
            elif name == "enclosure" and not image_url:
                kind = child.attrib.get("type", "").lower()
                if kind.startswith("image/"):
                    image_url = child.attrib.get("url", "").strip()
            elif name in {"description", "encoded", "summary", "content"} and not image_url:
                html = "".join(child.itertext())
                found = re.search(r"<img\b[^>]*?\bsrc=['\"]([^'\"]+)", html, re.I)
                if found:
                    image_url = found.group(1).strip()
        if title and page_url and urlparse(page_url).scheme in {"http", "https"}:
            entries.append({
                "title": title,
                "url": page_url,
                "image_url": image_url,
            })
        if len(entries) >= limit:
            break
    return entries


def page_metadata(page_url: str, timeout: int = 20) -> dict:
    """Read Open Graph/Twitter metadata from the episode page as a poster fallback."""
    response = requests.get(
        page_url, timeout=timeout, headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
    )
    response.raise_for_status()
    final = urlparse(response.url)
    if final.scheme != "https":
        raise ValueError("Episode page redirected to a non-HTTPS URL.")
    html = response.text[:2_000_000]
    values = {}
    for match in re.finditer(r'<meta\b[^>]*>', html, re.I):
        tag = match.group(0)
        key_match = re.search(r"(?:property|name)=['\"]([^'\"]+)['\"]", tag, re.I)
        content = re.search(r"\bcontent=['\"]([^'\"]+)['\"]", tag, re.I)
        if key_match and content:
            values.setdefault(key_match.group(1).lower(), content.group(1).strip())
    image = values.get("og:image") or values.get("twitter:image") or values.get("twitter:image:src") or ""
    title = values.get("og:title") or values.get("twitter:title") or ""
    from urllib.parse import urljoin
    return {
        "title": title,
        "image_url": urljoin(response.url, image) if image else "",
    }


def download_poster(image_url: str, timeout: int = 20, max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, str]:
    """Download a small HTTPS poster image suitable for Telegram sendPhoto."""
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Poster URL must be a public HTTPS URL.")
    with requests.get(
        image_url, stream=True, timeout=timeout,
        headers={"User-Agent": USER_AGENT}, allow_redirects=True,
    ) as response:
        response.raise_for_status()
        final = urlparse(response.url)
        if final.scheme != "https":
            raise ValueError("Poster redirected to a non-HTTPS URL.")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise ValueError("Poster URL did not return an image.")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("Poster image exceeds the 8 MB limit.")
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Poster image exceeded the 8 MB limit.")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise ValueError("Poster image is empty.")
        return data, content_type


def telegram_send_episode(token: str, chat_id: str, image_data: bytes, image_type: str,
                          title: str, episode: str, page_url: str) -> str:
    """Publish a poster with anime title, known episode number, and a watch link button."""
    if not token or not chat_id:
        raise RuntimeError("Telegram bot token or channel ID is missing.")
    from urllib.parse import urlparse as _urlparse
    if _urlparse(page_url).scheme != "https":
        raise ValueError("Episode watch link must use HTTPS.")
    anime_title, inferred_episode = episode_details(title)
    episode = episode or inferred_episode
    caption = f"🎬 {anime_title}"
    caption += f"\n📺 قسمت {episode}" if episode else "\n📺 شمارهٔ قسمت در منبع مشخص نشده"
    caption += f"\n\n🔗 لینک تماشا: {page_url}\n\nAnimehighbo"
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(image_type, "jpg")
    endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
    response = requests.post(
        endpoint,
        data={
            "chat_id": chat_id,
            "caption": caption[:1024],
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "▶️ مشاهده قسمت", "url": page_url}]]}),
        },
        files={"photo": (f"anime-poster.{extension}", image_data, image_type)},
        timeout=(30, 60),
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or result.get("ok") is not True:
        description = result.get("description", "Unexpected Telegram API response.") if isinstance(result, dict) else "Unexpected Telegram API response."
        raise RuntimeError(f"Telegram sendPhoto failed: {description}")
    return str(result.get("result", {}).get("message_id", ""))


def run_link_mode(config: dict, state: dict, dry_run: bool, token: str, chat_id: str) -> int:
    """Publish episode links with downloaded posters, without downloading episode videos."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    published_count = 0
    for feed in config["feeds"]:
        feed_url = str(feed.get("url", "")).strip()
        feed_name = str(feed.get("name", "Anime feed")).strip()
        parsed = urlparse(feed_url)
        if parsed.scheme != "https" or not parsed.hostname:
            LOG.error("Skipping feed with invalid HTTPS URL: %s", feed_name)
            continue
        try:
            response = session.get(feed_url, timeout=int(config["request_timeout_seconds"]), allow_redirects=True)
            response.raise_for_status()
            entries = parse_episode_feed(response.text, int(config["poll_limit_per_feed"]))
        except Exception as exc:
            LOG.exception("Episode feed %s failed: %s", feed_name, exc)
            continue

        for entry in reversed(entries):
            page_url = entry["url"]
            identity = hashlib.sha256(page_url.rstrip("/").encode("utf-8")).hexdigest()
            if identity in state["published"]:
                continue
            try:
                metadata = {}
                image_url = entry.get("image_url", "")
                if not image_url:
                    metadata = page_metadata(page_url, int(config["request_timeout_seconds"]))
                    image_url = metadata.get("image_url", "")
                if not image_url:
                    LOG.warning("No poster image found; skipping episode: %s", entry["title"])
                    continue
                from urllib.parse import urljoin
                image_url = urljoin(page_url, image_url)
                title = entry["title"]
                if metadata.get("title") and len(title) < 4:
                    title = metadata["title"]
                anime_title, episode = episode_details(title)
                if dry_run:
                    LOG.info("[DRY RUN] Episode link: %s | episode=%s | poster=%s | %s",
                             anime_title, episode or "unknown", image_url, page_url)
                    continue
                image_data, image_type = download_poster(image_url, int(config["request_timeout_seconds"]))
                message_id = telegram_send_episode(token, chat_id, image_data, image_type, title, episode, page_url)
                state["published"][identity] = {
                    "title": anime_title,
                    "episode": episode or None,
                    "url": page_url,
                    "poster_url": image_url,
                    "feed": feed_name,
                    "message_id": message_id,
                    "published_at": int(time.time()),
                }
                save_state(config["state_file"], state)
                published_count += 1
                LOG.info("Published episode link: %s | episode=%s", anime_title, episode or "unknown")
            except Exception as exc:
                LOG.exception("Failed to publish episode link %s: %s", entry["title"], exc)
    LOG.info("Link-mode run complete. Published %d episode link(s).", published_count)
    return published_count


def allowed_media_url(url: str, allowed_hosts: list[str]) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = {str(h).lower().strip().rstrip(".") for h in allowed_hosts}
        if parsed.scheme != "https" or not host or not allowed:
            return False
        if host not in allowed:
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.path.lower().endswith(".torrent") or parsed.scheme == "magnet":
            return False
        return Path(unquote(parsed.path)).suffix.lower() in VIDEO_EXTENSIONS
    except (ValueError, TypeError):
        return False


def load_state(path: str) -> dict:
    state_path = Path(path)
    if not state_path.exists():
        return {"published": {}, "seen": {}}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("State file must contain a JSON object")
    state.setdefault("published", {})
    state.setdefault("seen", {})
    return state


def save_state(path: str, state: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(title: str, url: str) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    stem = re.sub(r"[^\w .()-]+", "_", title, flags=re.UNICODE).strip(" ._")
    stem = re.sub(r"\s+", " ", stem)[:100] or "anime-video"
    return stem + suffix


def download_media(url: str, title: str, config: dict) -> Path:
    if not allowed_media_url(url, config["allowed_download_hosts"]):
        raise ValueError(f"Rejected non-allow-listed or non-direct media URL: {url}")
    target_dir = Path(config["download_dir"])
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / safe_filename(title, url)
    temp_path = final_path.with_suffix(final_path.suffix + ".part")
    max_bytes = int(config["max_file_size_mb"]) * 1024 * 1024
    timeout = int(config["request_timeout_seconds"])

    try:
        with requests.get(
            url, stream=True, timeout=timeout,
            headers={"User-Agent": USER_AGENT}, allow_redirects=False
        ) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                raise ValueError("Redirected media URLs are rejected; configure the final authorized host.")
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError("Media exceeds configured size limit.")
            written = 0
            with temp_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError("Media exceeded configured size limit during download.")
                    output.write(chunk)
        if temp_path.stat().st_size < 1024:
            raise ValueError("Downloaded file is too small to be a valid video.")
        with temp_path.open("rb") as check:
            header = check.read(12)
        suffix = final_path.suffix.lower()
        is_matroska = header.startswith(bytes.fromhex("1A45DFA3"))
        is_mp4_family = len(header) >= 8 and header[4:8] == b"ftyp"
        if suffix in {".mkv", ".webm"} and not is_matroska:
            raise ValueError("Downloaded content does not have a Matroska/WebM file signature.")
        if suffix in {".mp4", ".mov"} and not is_mp4_family:
            raise ValueError("Downloaded content does not have an MP4/MOV file signature.")
        temp_path.replace(final_path)
        return final_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def telegram_method_for_file(filename: str) -> str:
    """Use Telegram's video method only for MP4; keep other containers as documents."""
    return "sendVideo" if Path(filename).suffix.lower() == ".mp4" else "sendDocument"


def telegram_upload_and_send(token: str, chat_id: str, path: Path, caption: str) -> str:
    """Upload a locally verified, authorized media file to a Telegram chat/channel."""
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret is not configured.")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID secret is not configured.")
    method = telegram_method_for_file(path.name)
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    media_field = "video" if method == "sendVideo" else "document"
    payload = {"chat_id": chat_id, "caption": caption[:1024]}
    if method == "sendVideo":
        payload["supports_streaming"] = "true"

    with path.open("rb") as media:
        response = requests.post(
            endpoint,
            data=payload,
            files={media_field: (path.name, media, "application/octet-stream")},
            timeout=(30, 180),
        )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or result.get("ok") is not True:
        description = result.get("description", "Unexpected Telegram API response.") if isinstance(result, dict) else "Unexpected Telegram API response."
        raise RuntimeError(f"Telegram {method} failed: {description}")
    message = result.get("result", {})
    return str(message.get("message_id", ""))

def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config()
    state = load_state(config["state_file"])
    dry_run_setting = os.getenv("ANIMEHIGHBO_DRY_RUN", os.getenv("ANIMEHIGH_DRY_RUN", "")).strip().lower()
    if dry_run_setting:
        dry_run = dry_run_setting not in {"0", "false", "no", "off"}
    else:
        dry_run = bool(config["dry_run"])
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not dry_run and (not token or not chat_id):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets before live publishing.")
    if not config["feeds"]:
        LOG.warning("No feeds configured. Nothing to process.")
        return 0

    if config.get("mode", "links") == "links":
        if not dry_run and (not token or not chat_id):
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets before live publishing.")
        run_link_mode(config, state, dry_run, token, chat_id)
        return 0

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    published_count = 0
    for feed in config["feeds"]:
        feed_url = str(feed.get("url", "")).strip()
        feed_name = str(feed.get("name", "Authorized feed")).strip()
        parsed_feed_url = urlparse(feed_url)
        if parsed_feed_url.scheme != "https" or not parsed_feed_url.hostname:
            LOG.error("Skipping feed with invalid HTTPS URL: %s", feed_name)
            continue
        try:
            response = session.get(
                feed_url,
                timeout=int(config["request_timeout_seconds"]),
                allow_redirects=True,
            )
            response.raise_for_status()
            entries = parse_feed(response.text, int(config["poll_limit_per_feed"]))
        except Exception as exc:
            LOG.exception("Feed %s failed: %s", feed_name, exc)
            continue

        # Oldest-first keeps backlogs from being starved by a constant stream of new entries.
        for entry in reversed(entries):
            url = entry["url"]
            if not allowed_media_url(url, config["allowed_download_hosts"]):
                LOG.info("Skipping non-direct or non-allow-listed entry: %s", entry["title"])
                continue
            identity = hashlib.sha256(url.encode("utf-8")).hexdigest()
            if identity in state["published"]:
                continue
            if dry_run:
                LOG.info("[DRY RUN] Would download and publish: %s", entry["title"])
                continue
            media_path = None
            try:
                media_path = download_media(url, entry["title"], config)
                file_hash = sha256_file(media_path)
                if file_hash in state["seen"]:
                    LOG.info("Skipping duplicate file hash: %s", entry["title"])
                    state["published"][identity] = {"duplicate_of": state["seen"][file_hash]}
                    save_state(config["state_file"], state)
                    continue
                caption = f"🎬 {entry['title']}\n\nAnimehighbo"
                message_id = telegram_upload_and_send(token, chat_id, media_path, caption)
                state["seen"][file_hash] = identity
                state["published"][identity] = {
                    "title": entry["title"],
                    "feed": feed_name,
                    "file_sha256": file_hash,
                    "message_id": message_id,
                    "published_at": int(time.time()),
                }
                save_state(config["state_file"], state)
                published_count += 1
                LOG.info("Published: %s", entry["title"])
            except Exception as exc:
                LOG.exception("Failed to process %s: %s", entry["title"], exc)
            finally:
                if media_path and media_path.exists():
                    media_path.unlink(missing_ok=True)

    LOG.info("Run complete. Published %d item(s).", published_count)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as error:
        logging.basicConfig(level=logging.INFO)
        logging.error("%s", error)
        raise SystemExit(1)
