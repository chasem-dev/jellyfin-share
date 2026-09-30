#!/usr/bin/env python3
"""Local-only share manager and token-gated public media viewer."""

import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from preview import render_card

ROOTS = [Path(p) for p in os.environ.get("MEDIA_ROOTS", "/media/Movies:/media/TV").split(":") if p]
DATA = Path(os.environ.get("PORTAL_DATA", "/data"))
CACHE = Path(os.environ.get("PORTAL_CACHE", "/cache"))
METADATA_ROOT = Path(os.environ.get("JELLYFIN_METADATA_ROOT", "/var/lib/jellyfin/metadata/library"))
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
PUBLIC_HOST = os.environ.get("PUBLIC_BIND", "172.17.0.1")
PUBLIC_PORT = int(os.environ.get("PUBLIC_PORT", "8787"))
ADMIN_HOST = os.environ.get("ADMIN_BIND", "127.0.0.1")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8788"))
ADMIN_EXTERNAL_PORT = int(os.environ.get("ADMIN_EXTERNAL_PORT", str(ADMIN_PORT)))
ADMIN_ALLOWED_HOSTS = {host.strip() for host in os.environ.get("ADMIN_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()}
ADMIN_HOST_HEADERS = {f"{host}:{port}" for host in ADMIN_ALLOWED_HOSTS for port in {ADMIN_PORT, ADMIN_EXTERNAL_PORT}}
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,80}$")
SEGMENT_RE = re.compile(r"^segment_[0-9]{5,8}\.ts$")
catalog_lock = threading.Lock()
catalog_cache = (0.0, {})
poster_lock = threading.Lock()
poster_cache = (0.0, {})
encode_lock = threading.Lock()
encodes = {}
last_cleanup = 0.0


def db():
    connection = sqlite3.connect(DATA / "shares.db", timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""CREATE TABLE IF NOT EXISTS shares (
            id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
            token TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL,
            files_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, revoked_at INTEGER,
            scope_label TEXT NOT NULL DEFAULT ''
        )""")
        if "scope_label" not in {row[1] for row in connection.execute("PRAGMA table_info(shares)")}:
            connection.execute("ALTER TABLE shares ADD COLUMN scope_label TEXT NOT NULL DEFAULT ''")


def valid_media(path):
    try:
        resolved = Path(path).resolve(strict=True)
        return resolved.is_file() and resolved.suffix.lower() in VIDEO_EXT and any(resolved.is_relative_to(root.resolve()) for root in ROOTS if root.is_dir())
    except (OSError, ValueError):
        return False


def scan_catalog(force=False):
    global catalog_cache
    with catalog_lock:
        if not force and time.time() - catalog_cache[0] < 300:
            return catalog_cache[1]
        found = {}
        for root in ROOTS:
            if not root.is_dir():
                continue
            kind = "movie" if root.name.lower() == "movies" else "series"
            try:
                folders = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
            except OSError:
                continue
            for folder in folders:
                key = kind + ":" + folder.name.casefold()
                entry = found.setdefault(key, {"id": hashlib.sha256(key.encode()).hexdigest()[:24], "title": folder.name, "kind": kind, "files": []})
                try:
                    if kind == "movie":
                        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
                        if files:
                            entry["files"].append(str(max(files, key=lambda p: p.stat().st_size)))
                    else:
                        for current, dirs, files in os.walk(folder):
                            dirs[:] = [d for d in dirs if not d.startswith(".")]
                            for name in files:
                                if Path(name).suffix.lower() in VIDEO_EXT:
                                    entry["files"].append(str(Path(current) / name))
                except OSError:
                    continue
        catalog = {}
        for entry in found.values():
            entry["files"] = sorted(set(entry["files"]), key=episode_sort)
            if entry["files"]:
                catalog[entry["id"]] = entry
        catalog_cache = (time.time(), catalog)
        return catalog


def episode_sort(path):
    season, episode = episode_parts(path)
    return season if season is not None else 999, episode if episode is not None else 999, Path(path).name.casefold()


def episode_parts(path):
    video = Path(path)
    match = re.search(r"[Ss](\d{1,3})[Ee](\d{1,3})", video.name)
    if match:
        return int(match.group(1)), int(match.group(2))
    for parent in video.parents:
        match = re.fullmatch(r"(?:Season|Series|S)\s*0*(\d{1,3})", parent.name, re.IGNORECASE)
        if match:
            return int(match.group(1)), None
    return None, None


def episode_id(path):
    return hashlib.sha256(path.encode()).hexdigest()[:24]


def catalog_detail(entry):
    seasons = {}
    for index, path in enumerate(entry["files"]):
        season, _ = episode_parts(path)
        key = season if season is not None else 0
        seasons.setdefault(key, []).append({"id": episode_id(path), "label": file_label(path, "series", index)})
    return {"id": entry["id"], "title": entry["title"], "kind": entry["kind"], "count": len(entry["files"]), "seasons": [{"number": number, "label": f"Season {number}" if number else "Other episodes", "count": len(episodes), "episodes": episodes} for number, episodes in sorted(seasons.items())]}


def file_label(path, kind, number):
    if kind == "movie":
        return "Play movie"
    name = Path(path).stem
    match = re.search(r"[Ss](\d{1,3})[Ee](\d{1,3})", name)
    return f"Season {int(match.group(1))} · Episode {int(match.group(2))}" if match else f"Episode {number + 1}"


def poster_for(share):
    global poster_cache
    index_file = DATA / "poster-index.json"
    try:
        modified = index_file.stat().st_mtime_ns
    except OSError:
        index = {}
    else:
        with poster_lock:
            if modified != poster_cache[0]:
                try:
                    poster_cache = (modified, json.loads(index_file.read_text()))
                except (OSError, ValueError):
                    poster_cache = (modified, {})
            index = poster_cache[1]
    for file in share_files(share):
        video = Path(file)
        for candidate in (video, *video.parents):
            poster = index.get(str(candidate))
            if poster:
                try:
                    resolved = Path(poster).resolve(strict=True)
                    if resolved.is_file() and resolved.is_relative_to(METADATA_ROOT.resolve()) and resolved.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                        return resolved
                except (OSError, ValueError):
                    pass
        for parent in video.parents:
            if not any(parent.resolve().is_relative_to(root.resolve()) for root in ROOTS if root.is_dir()):
                break
            for name in ("poster.jpg", "poster.png", "poster.webp", "folder.jpg", "cover.jpg"):
                poster = parent / name
                try:
                    resolved = poster.resolve(strict=True)
                    if resolved.is_file() and resolved.is_relative_to(parent.resolve()):
                        return resolved
                except (OSError, ValueError):
                    pass
    return None


def viewer_html(share, token):
    title = share["title"]
    scope = share.get("scope_label") or ""
    display = f"{title} · {scope}" if scope else title
    description = f"Watch {scope.lower()} of {title} with this private link." if scope else f"Watch {title} with this private link."
    url = f"{PUBLIC_BASE}/s/{token}"
    image = f"{url}/preview.jpg"
    def meta(key, value, property=True):
        attribute = "property" if property else "name"
        return f'<meta {attribute}="{key}" content="{html.escape(str(value), quote=True)}">'
    tags = [
        meta("description", description, False),
        meta("og:title", display), meta("og:type", "video.movie" if share["kind"] == "movie" else "video.episode" if len(share_files(share)) == 1 else "video.tv_show"),
        meta("og:description", description), meta("og:url", url), meta("og:site_name", "Shared viewing"),
        meta("og:image", image), meta("og:image:secure_url", image), meta("og:image:type", "image/jpeg"),
        meta("og:image:width", 1200), meta("og:image:height", 630), meta("og:image:alt", f"Preview card for {display}"),
        meta("twitter:card", "summary_large_image", False), meta("twitter:title", display, False),
        meta("twitter:description", description, False), meta("twitter:image", image, False),
    ]
    template = (Path(__file__).parent / "static" / "viewer.html").read_text()
    return template.replace("<title>Watch shared video</title>", f"<title>{html.escape(display, quote=True)} · Shared viewing</title>" + "".join(tags), 1)


def subtitles_for(path):
    video = Path(path)
    try:
        candidates = [p for p in video.parent.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in {".srt", ".vtt"} and (p.stem == video.stem or p.stem.startswith(video.stem + "."))]
    except OSError:
        return []
    return sorted(candidates, key=lambda p: p.name.casefold())[:20]


def subtitle_details(path):
    codes = {"en", "eng", "es", "spa", "fr", "fre", "ja", "jpn", "de", "ger"}
    language = next((part for part in reversed(path.stem.lower().split(".")) if part in codes), "und")
    language = {"eng": "en", "spa": "es", "fre": "fr", "jpn": "ja", "ger": "de"}.get(language, language)
    label = {"en": "English", "es": "Spanish", "fr": "French", "ja": "Japanese", "de": "German"}.get(language, "Subtitles")
    if ".hi." in path.name.lower() or ".sdh." in path.name.lower():
        label += " (SDH)"
    return {"label": label, "language": language}


def lookup(token):
    if not TOKEN_RE.fullmatch(token):
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db() as connection:
        row = connection.execute("SELECT * FROM shares WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (digest, int(time.time()))).fetchone()
    return dict(row) if row else None


def share_files(share):
    return json.loads(share["files_json"])


def asset_path(share, index):
    files = share_files(share)
    if index < 0 or index >= len(files) or not valid_media(files[index]):
        return None
    return Path(files[index])


def encode_dir(share, index, path):
    fingerprint = hashlib.sha256(f"{share['id']}:{index}:{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}".encode()).hexdigest()[:24]
    return CACHE / fingerprint


def start_encode(share, index, path):
    target = encode_dir(share, index, path)
    playlist = target / "index.m3u8"
    with encode_lock:
        running = encodes.get(str(target))
        if running and running.poll() is None:
            return target
        if playlist.exists() and "#EXT-X-ENDLIST" in playlist.read_text(errors="ignore"):
            return target
        if target.exists():
            shutil.rmtree(target)
        if sum(process.poll() is None for process in encodes.values()) >= 3:
            raise RuntimeError("All playback converters are busy")
        target.mkdir(parents=True)
        # H.264 video can be copied into HLS quickly; other codecs are converted.
        try:
            probe = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=15)
            codec = probe.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            codec = ""
        video = ["-c:v", "copy"] if codec == "h264" else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-vf", "scale=w='min(1280,iw)':h=-2"]
        command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", *video, "-threads", "4", "-c:a", "aac", "-ac", "2", "-b:a", "160k", "-f", "hls", "-hls_time", "6", "-hls_list_size", "0", "-hls_playlist_type", "event", "-hls_segment_filename", str(target / "segment_%05d.ts"), str(playlist)]
        log = open(target / "ffmpeg.log", "wb")
        try:
            encodes[str(target)] = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
        finally:
            log.close()
    return target


def cleanup_cache():
    global last_cleanup
    if time.time() - last_cleanup < 1800:
        return
    last_cleanup = time.time()
    with encode_lock:
        for folder in CACHE.iterdir():
            if not folder.is_dir() or time.time() - folder.stat().st_mtime < 6 * 3600:
                continue
            process = encodes.get(str(folder))
            if process and process.poll() is None:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            encodes.pop(str(folder), None)


class Handler(BaseHTTPRequestHandler):
    server_version = "SharePortal/1"

    def log_message(self, fmt, *args):
        # URLs contain private bearer tokens; avoid access logs.
        pass

    def respond(self, status, body=b"", content_type="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status, obj):
        self.respond(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        cleanup_cache()
        path = urlsplit(self.path).path
        if self.server.is_admin:
            if self.headers.get("Host") not in ADMIN_HOST_HEADERS:
                return self.respond(403, "Invalid host")
            return self.admin_get(path)
        return self.public_get(path)

    def do_POST(self):
        if not self.server.is_admin:
            return self.respond(404, "Not found")
        # Reject DNS rebinding and cross-site form submissions to the LAN manager.
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin != f"http://{host}" or host not in ADMIN_HOST_HEADERS:
            return self.respond(403, "Invalid origin")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.respond(415, "JSON required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096 or length < 1:
                raise ValueError()
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return self.respond(400, "Invalid request")
        path = urlsplit(self.path).path
        if path == "/api/shares":
            item = scan_catalog().get(str(payload.get("catalog_id", "")))
            hours = payload.get("hours")
            if not item or hours not in {1, 24, 72, 168}:
                return self.respond(400, "Invalid selection")
            scope = payload.get("scope", "movie" if item["kind"] == "movie" else "series")
            scope_label = ""
            if item["kind"] == "movie":
                if scope != "movie":
                    return self.respond(400, "Invalid scope")
                selected = item["files"]
            elif scope == "series":
                selected = item["files"]
            elif scope == "season":
                season = payload.get("season")
                if type(season) is not int or not 0 <= season <= 999:
                    return self.respond(400, "Invalid season")
                selected = [p for p in item["files"] if (episode_parts(p)[0] or 0) == season]
                scope_label = f"Season {season}" if season else "Other episodes"
            elif scope == "episode":
                requested = payload.get("episode_id")
                selected = [p for p in item["files"] if episode_id(p) == requested]
                if len(selected) != 1:
                    return self.respond(400, "Invalid episode")
                scope_label = file_label(selected[0], "series", item["files"].index(selected[0]))
            else:
                return self.respond(400, "Invalid scope")
            files = [p for p in selected if valid_media(p)]
            if not files:
                return self.respond(400, "Media unavailable")
            token = secrets.token_urlsafe(32)
            now = int(time.time())
            try:
                with db() as connection:
                    connection.execute("INSERT INTO shares (token_hash, token, title, kind, files_json, created_at, expires_at, scope_label) VALUES (?,?,?,?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), token, item["title"], item["kind"], json.dumps(files), now, now + hours * 3600, scope_label))
            except sqlite3.OperationalError:
                return self.respond(503, "Link storage is busy. Try again shortly.", extra={"Retry-After": "3"})
            return self.json(201, {"url": f"{PUBLIC_BASE}/s/{token}"})
        match = re.fullmatch(r"/api/shares/(\d+)/revoke", path)
        if match:
            try:
                with db() as connection:
                    result = connection.execute("UPDATE shares SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (int(time.time()), int(match.group(1))))
            except sqlite3.OperationalError:
                return self.respond(503, "Link storage is busy. Try again shortly.", extra={"Retry-After": "3"})
            return self.json(200 if result.rowcount else 404, {"revoked": bool(result.rowcount)})
        return self.respond(404, "Not found")

    def admin_get(self, path):
        if path == "/":
            return self.respond(200, (Path(__file__).parent / "static" / "admin.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/catalog":
            params = parse_qs(urlsplit(self.path).query)
            query = params.get("q", [""])[0].strip().casefold()[:100]
            entries = [entry for entry in scan_catalog(force=params.get("refresh") == ["1"]).values() if query in entry["title"].casefold()]
            entries.sort(key=lambda entry: entry["title"].casefold())
            return self.json(200, [{"id": e["id"], "title": e["title"], "kind": e["kind"], "count": len(e["files"])} for e in entries[:60]])
        detail = re.fullmatch(r"/api/catalog/([0-9a-f]{24})", path)
        if detail:
            entry = scan_catalog().get(detail.group(1))
            return self.json(200, catalog_detail(entry)) if entry else self.respond(404, "Title unavailable")
        if path == "/api/shares":
            with db() as connection:
                rows = connection.execute("SELECT id,token,title,kind,scope_label,created_at,expires_at,revoked_at FROM shares ORDER BY id DESC LIMIT 100").fetchall()
            return self.json(200, [{**dict(row), "url": f"{PUBLIC_BASE}/s/{row['token']}"} for row in rows])
        return self.static(path)

    def public_get(self, path):
        if path in {"/", "/robots.txt"}:
            return self.respond(200, "User-agent: *\nAllow: /s/\nDisallow: /\n" if path == "/robots.txt" else "This link is private. Open the exact share URL you received.")
        if path.startswith("/assets/"):
            if path not in {"/assets/style.css", "/assets/viewer.js", "/assets/hls.min.js", "/assets/favicon.svg"}:
                return self.respond(404, "Not found")
            return self.static(path)
        match = re.fullmatch(r"/s/([^/]+)(?:/(.*))?", path)
        if not match:
            return self.respond(404, "Not found")
        token, suffix = match.groups()
        share = lookup(token)
        if not share:
            return self.respond(404, "This link has expired or was revoked.")
        if not suffix:
            return self.respond(200, viewer_html(share, token), "text/html; charset=utf-8")
        if suffix == "preview.jpg":
            try:
                card = render_card(share["title"], share.get("scope_label") or "", share["kind"], poster_for(share))
            except (OSError, ValueError):
                return self.respond(500, "Preview unavailable")
            return self.respond(200, card, "image/jpeg")
        if suffix == "info":
            files = share_files(share)
            return self.json(200, {"title": share["title"], "scope_label": share.get("scope_label") or "", "kind": share["kind"], "expires_at": share["expires_at"], "episodes": [{"index": i, "label": file_label(p, share["kind"], i), "format": "mp4" if Path(p).suffix.lower() == ".mp4" else "hls", "subtitles": [{"index": j, **subtitle_details(sub)} for j, sub in enumerate(subtitles_for(p))]} for i, p in enumerate(files)]})
        subtitle = re.fullmatch(r"subtitle/(\d+)/(\d+)\.vtt", suffix)
        if subtitle:
            index, sub_index = int(subtitle.group(1)), int(subtitle.group(2))
            media = asset_path(share, index)
            if not media:
                return self.respond(404, "Media unavailable")
            tracks = subtitles_for(media)
            if sub_index >= len(tracks):
                return self.respond(404, "Subtitle unavailable")
            content = tracks[sub_index].read_text(encoding="utf-8-sig", errors="replace")
            if tracks[sub_index].suffix.lower() == ".srt":
                content = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)
            return self.respond(200, content, "text/vtt; charset=utf-8")
        stream = re.fullmatch(r"stream/(\d+)", suffix)
        if stream:
            index = int(stream.group(1))
            media = asset_path(share, index)
            if not media:
                return self.respond(404, "Media unavailable")
            if media.suffix.lower() == ".mp4":
                return self.send_file(media, "video/mp4", range_allowed=True)
            return self.json(200, {"type": "hls", "url": f"/s/{token}/hls/{index}/index.m3u8"})
        hls = re.fullmatch(r"hls/(\d+)/(index\.m3u8|segment_[0-9]{5,8}\.ts)", suffix)
        if hls:
            index, name = int(hls.group(1)), hls.group(2)
            media = asset_path(share, index)
            if not media:
                return self.respond(404, "Media unavailable")
            if name == "index.m3u8":
                try:
                    target = start_encode(share, index, media)
                except RuntimeError:
                    return self.respond(503, "Playback is busy. Try again shortly.", extra={"Retry-After": "5"})
            else:
                target = encode_dir(share, index, media)
            file = target / name
            if name == "index.m3u8":
                deadline = time.time() + 15
                while not file.exists() and time.time() < deadline:
                    time.sleep(.25)
                if not file.exists():
                    return self.respond(503, "Playback is starting. Try again shortly.", extra={"Retry-After": "3"})
                return self.respond(200, file.read_bytes(), "application/vnd.apple.mpegurl")
            if not SEGMENT_RE.fullmatch(name) or not file.exists():
                return self.respond(404, "Segment unavailable")
            return self.send_file(file, "video/mp2t")
        return self.respond(404, "Not found")

    def static(self, path):
        name = path.removeprefix("/assets/")
        if path.startswith("/assets/") and name in {"style.css", "admin.js", "viewer.js", "hls.min.js", "favicon.svg"}:
            file = Path(__file__).parent / "static" / name
            if file.is_file():
                return self.respond(200, file.read_bytes(), mimetypes.guess_type(file)[0] or "application/octet-stream")
        return self.respond(404, "Not found")

    def send_file(self, path, content_type, range_allowed=False):
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        header = self.headers.get("Range", "") if range_allowed else ""
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match:
                return self.respond(416, "Invalid range", extra={"Content-Range": f"bytes */{size}"})
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            elif match.group(2):
                start = max(0, size - int(match.group(2)))
            if start >= size or end < start:
                return self.respond(416, "Invalid range", extra={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if range_allowed:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    init()
    public = ThreadingHTTPServer((PUBLIC_HOST, PUBLIC_PORT), Handler)
    public.is_admin = False
    admin = ThreadingHTTPServer((ADMIN_HOST, ADMIN_PORT), Handler)
    admin.is_admin = True
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    print(f"Viewer {PUBLIC_HOST}:{PUBLIC_PORT}; manager {ADMIN_HOST}:{ADMIN_PORT}", flush=True)
    public.serve_forever()


if __name__ == "__main__":
    main()
