"""Deterministic single-member STPZ archives for STEP visualization delivery.

Each .stpz contains exactly one same-stem .stp regular file, compressed with
DEFLATE. Packing preserves the source bytes. Unpacking checks archive structure
and CRC before replacing its destination; callers validate STEP semantics on the
unpacked saved archive before publishing it. Recipe scratch paths belong in build/.
Archives support up to 64 MiB compressed and 512 MiB uncompressed STEP data.
"""

import io
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_SIZE = 64 * 1024 * 1024
MAX_STEP_SIZE = 512 * 1024 * 1024


def _copy_step(source: BinaryIO, destination: BinaryIO) -> None:
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        size += len(chunk)
        if size > MAX_STEP_SIZE:
            raise ValueError(f"Uncompressed STEP exceeds {MAX_STEP_SIZE // 1024**2} MiB")
        destination.write(chunk)


class _BoundedZipReader(io.BufferedReader):
    """Bound ZIP metadata reads as well as streamed member reads.

    ZipFile otherwise materializes an arbitrarily large central directory before
    callers can reject extra members. One member's directory fits within 1 MiB.
    """

    def read(self, size: int = -1) -> bytes:
        if size > CHUNK_SIZE:
            raise ValueError("STPZ archive metadata exceeds the single-member bound")
        data = super().read(CHUNK_SIZE + 1 if size < 0 else size)
        if len(data) > CHUNK_SIZE:
            raise ValueError("STPZ archive metadata exceeds the single-member bound")
        return data


def _member_name(archive: Path) -> str:
    if archive.suffix != ".stpz" or any(char in archive.name for char in ("\\", ":")):
        raise ValueError("Compressed STEP archives require a local .stpz filename")
    return archive.with_suffix(".stp").name


def pack_step(source: Path, archive: Path) -> None:
    """Atomically write deterministic ZIP bytes; callers own STEP validation."""
    name = _member_name(archive)
    if source.stat().st_size > MAX_STEP_SIZE:
        raise ValueError(f"Uncompressed STEP exceeds {MAX_STEP_SIZE // 1024**2} MiB")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=archive.parent, prefix=f".{archive.name}.", delete=False) as pending:
        temporary = Path(pending.name)
    try:
        info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.file_size = source.stat().st_size
        info.compress_type = ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as packed:
            with source.open("rb") as stream, packed.open(info, "w") as member:
                _copy_step(stream, member)
        if temporary.stat().st_size > MAX_ARCHIVE_SIZE:
            raise ValueError(f"STPZ archive exceeds {MAX_ARCHIVE_SIZE // 1024**2} MiB")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)


def unpack_step(archive: Path, destination: Path) -> None:
    """Validate one same-stem .stp member and CRC before replacing destination."""
    name = _member_name(archive)
    if archive.stat().st_size > MAX_ARCHIVE_SIZE:
        raise ValueError(f"STPZ archive exceeds {MAX_ARCHIVE_SIZE // 1024**2} MiB")
    if archive.resolve() == destination.resolve():
        raise ValueError("STPZ extraction cannot replace its archive")
    with _BoundedZipReader(archive.open("rb", buffering=0)) as stream, ZipFile(stream) as packed:
        members = packed.infolist()
        if len(members) != 1:
            raise ValueError("STPZ requires exactly one STEP member")
        info = members[0]
        if info.file_size > MAX_STEP_SIZE:
            raise ValueError(f"Uncompressed STEP exceeds {MAX_STEP_SIZE // 1024**2} MiB")
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (
            info.filename != name
            or info.orig_filename != name
            or info.is_dir()
            or mode not in (0, stat.S_IFREG)
            or info.flag_bits & (1 | 64)
            or info.compress_type != ZIP_DEFLATED
        ):
            raise ValueError("STPZ requires one unencrypted, DEFLATE-compressed same-stem .stp regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", delete=False
        ) as pending:
            temporary = Path(pending.name)
            try:
                with packed.open(info) as member:
                    _copy_step(member, pending)
                pending.close()
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
