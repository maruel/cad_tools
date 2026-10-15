"""Read bounded JSON and archive metadata."""

import json
import zipfile
from pathlib import Path


def read_json(path: Path) -> object:
    # Profiles and reports are small metadata, never toolpaths or mesh payloads.
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"JSON metadata exceeds 1 MiB: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def archive_metadata(archive: zipfile.ZipFile, name: str) -> bytes:
    if archive.getinfo(name).file_size > 1024 * 1024:
        raise ValueError(f"Archive metadata exceeds 1 MiB: {name}")
    return archive.read(name)
