#!/usr/bin/env python3
"""Build a minimal poster lookup from Jellyfin's local database."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

DATABASE = Path(os.environ.get("JELLYFIN_DB", "/var/lib/jellyfin/data/jellyfin.db"))
ARTWORK = Path(os.environ.get("JELLYFIN_METADATA_ROOT", "/var/lib/jellyfin/metadata/library"))
ARTWORK_IN_CONTAINER = Path(os.environ.get("JELLYFIN_METADATA_CONTAINER_ROOT", "/var/lib/jellyfin/metadata/library"))
OUTPUT = Path(os.environ.get("POSTER_INDEX_OUTPUT", Path(__file__).parent / "data" / "poster-index.json"))
MEDIA_PATH_MAP = json.loads(os.environ.get("MEDIA_PATH_MAP", "{}"))


def container_media_path(media):
    path = Path(media)
    for source, target in sorted(MEDIA_PATH_MAP.items(), key=lambda pair: len(pair[0]), reverse=True):
        try:
            return str(Path(target) / path.relative_to(source))
        except ValueError:
            pass
    return media


def main():
    index = {}
    artwork_root = ARTWORK.resolve(strict=True)
    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as connection:
        rows = connection.execute("""SELECT b.Path, i.Path FROM BaseItems b
            JOIN BaseItemImageInfos i ON i.ItemId=b.Id
            WHERE i.ImageType=0 AND b.Path IS NOT NULL""")
        for media, poster in rows:
            if not poster:
                continue
            artwork = Path(poster)
            try:
                relative = artwork.resolve(strict=True).relative_to(artwork_root)
                if artwork.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    index[container_media_path(media)] = str(ARTWORK_IN_CONTAINER / relative)
            except (OSError, ValueError):
                continue
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="poster-index-", dir=OUTPUT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(index, file, separators=(",", ":"))
        os.chmod(temporary, 0o600)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Indexed {len(index)} Jellyfin posters")


if __name__ == "__main__":
    main()
