"""Compare decoded previews while tolerating negligible rendering differences."""

from pathlib import Path

from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError


def same_preview(previous: Path, generated: Path) -> bool:
    """Preserve a preview only when both global and local pixel differences are small.

    On 8-bit RGBA, require each channel's RMS difference <= 0.25 and every pixel's
    channel difference <= 2. The local limit keeps
    a small changed feature from disappearing into the full-image average.
    """
    if not previous.is_file() or previous.is_symlink():
        return False
    with Image.open(generated) as source:
        current = source.convert("RGBA")
    try:
        with Image.open(previous) as source:
            existing = source.convert("RGBA")
    except (OSError, UnidentifiedImageError):
        # An unreadable old preview needs replacement; invalid new renders fail.
        return False
    if existing.size != current.size:
        return False
    difference = ImageChops.difference(existing, current)
    return max(ImageStat.Stat(difference).rms) <= 0.25 and max(high for _, high in difference.getextrema()) <= 2
