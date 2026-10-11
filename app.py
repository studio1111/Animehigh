"""Animehigh: safe publisher for explicitly authorized direct media feeds.

Only direct video links on configured allow-listed hosts are downloaded.
Torrent pages, HTML pages, magnets, and unapproved hosts are rejected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

LOG = logging.getLogger("animehigh")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".webm", ".mov"}
USER_AGENT = "AnimehighPublisher/1.0 (+authorized-media-only)"


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
    config.setdefault("max_file_size_mb", 1800)
    config.setdefault("download_dir", "downloads")
    config.setdefault("state_file", "data/state.json")
    config.setdefault("dry_run", True)
    config.setdefault("poll_limit_per_feed", 30)
    config.setdefault("request_timeout_seconds", 30)
    return config


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str, limit: int = 30) -> list[dict]:
    """Parse common RSS and Atom formats without executing feed content."""
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter():
        if local_name(node.tag) not in {"item", "entry"}:
            continue
        title = ""
        link = ""
        for child in list(node):
            name = local_name(child.tag)
            if name == "title" and not title:
                title = "".join(child.itertext()).strip()
            elif name == "enclosure" and not link:
                link = child.attrib.get("url", "").strip()
            elif name in {"content", "thumbnail"} and not link:
                link = child.attrib.get("url", "").strip()
            elif name == "link":
                href = child.attrib.get("href", "").strip()
                rel = child.attrib.get("rel", "alternate").lower()
                candidate = href or (child.text or "").strip()
                if candidate and (rel == "enclosure" or not link):
                    link = candidate
        if title and link:
            items.append({"title": title, "url": link})
        if len(items) >= limit:
            break
    return items


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

    with requests.get(
        url, stream=True, timeout=timeout,
        headers={"User-Agent": USER_AGENT}, allow_redirects=False
    ) as response:
        response.raise_for_status()
        if response.status_code in (301, 302, 303, 307, 308):
            raise ValueError("Redirected media URLs are rejected; configure the final authorized host.")
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
        temp_path.unlink(missing_ok=True)
        raise ValueError("Downloaded file is too small to be a valid video.")
    temp_path.replace(final_path)
    return final_path


def rubika_request(token: str, method: str, payload: dict, timeout: int = 30) -> dict:
    if not token:
        raise RuntimeError("RUBIKA_TOKEN secret is not configured.")
    url = f"https://botapi.rubika.ir/v3/{token}/{method}"
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected Rubika response for {method}.")
    status = str(result.get("status", "")).upper()
    if status and status not in {"OK", "SUCCESS"}:
        raise RuntimeError(f"Rubika {method} failed with status: {status}")
    return result


def rubika_upload_and_send(token: str, chat_id: str, path: Path, caption: str) -> str:
    """Upload a locally verified file using the documented Rubika Bot API."""
    upload_result = rubika_request(token, "requestSendFile", {"type": "File"})
    upload_data = upload_result.get("data", upload_result)
    upload_url = upload_data.get("upload_url") if isinstance(upload_data, dict) else None
    if not upload_url:
        raise RuntimeError("Rubika did not return upload_url for requestSendFile.")

    with path.open("rb") as media:
        response = requests.post(
            upload_url,
            files={"file": (path.name, media, "application/octet-stream")},
            timeout=180,
        )
    response.raise_for_status()
    uploaded = response.json()
    upload_data = uploaded.get("data", uploaded) if isinstance(uploaded, dict) else {}
    file_id = upload_data.get("file_id") if isinstance(upload_data, dict) else None
    if not file_id:
        raise RuntimeError("Rubika upload did not return file_id.")

    sent = rubika_request(
        token, "sendFile",
        {"chat_id": chat_id, "file_id": file_id, "text": caption},
        timeout=60,
    )
    send_data = sent.get("data", sent)
    message_id = send_data.get("message_id", "") if isinstance(send_data, dict) else ""
    return str(message_id)


def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config()
    state = load_state(config["state_file"])
    dry_run = config["dry_run"] or os.getenv("ANIMEHIGH_DRY_RUN", "true").lower() in {
        "1", "true", "yes", "on"
    }
    token = os.getenv("RUBIKA_TOKEN", "")
    chat_id = os.getenv("RUBIKA_CHAT_ID", "")

    if not dry_run and (not token or not chat_id):
        raise RuntimeError("Set RUBIKA_TOKEN and RUBIKA_CHAT_ID secrets before live publishing.")
    if not config["feeds"]:
        LOG.warning("No feeds configured. Nothing to process.")
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
                    continue
                caption = f"🎬 {entry['title']}\n\nAnimehigh"
                message_id = rubika_upload_and_send(token, chat_id, media_path, caption)
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
