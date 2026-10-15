"""Publish complete generated trees, preserving essentially unchanged previews."""

import shutil
from pathlib import Path

from PIL import Image

from cad_tools.execution import require_files
from cad_tools.previews import same_preview


def _tree(root: Path) -> list[Path]:
    """Reject links, including ancestors, before reading or changing a tree."""
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError(f"Publication paths cannot contain symlinks: {path}")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"Publication tree is not a directory: {root}")
    entries = sorted(root.resolve().rglob("*"))
    for path in entries:
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Publication tree contains a link or special file: {path}")
    return entries


def publish(staging: Path, destination: Path) -> None:
    """Own the complete destination tree and consume staging on success.

    Call after generation and checks finish. Validate all files and paths before
    deleting obsolete results. Each replacement is atomic, while the collection
    can be partially published if interrupted. There is no rollback or concurrent
    writer support. Regenerate complete staging after interruption. Staging must
    be nonempty and on the destination filesystem.
    """
    staged = _tree(staging)
    existing = _tree(destination)
    staging, destination = staging.resolve(), destination.resolve()
    if staging == destination or staging in destination.parents or destination in staging.parents:
        raise ValueError("Publication trees must be separate and cannot contain each other")
    files = [path for path in staged if path.is_file()]
    if not files:
        raise ValueError(f"Publication staging has no generated files: {staging}")
    names = tuple(str(path.relative_to(staging)) for path in files)
    require_files(staging, names)
    parent = destination
    while not parent.exists():
        parent = parent.parent
    if not parent.is_dir():
        raise ValueError(f"Publication ancestor is not a directory: {parent}")
    if staging.stat().st_dev != parent.stat().st_dev:
        raise ValueError("Publication trees must be on the same filesystem")
    generated = set(names)
    preserved = set()
    for name in names:
        source = staging / name
        if source.suffix == ".webp":
            with Image.open(source) as image:
                image.verify()
            if same_preview(destination / name, source):
                preserved.add(name)
    # Delete only within the inspected destination. Remove directories bottom-up
    # so file/directory shape changes and empty output directories are supported.
    for path in reversed(existing):
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
        elif str(path.relative_to(destination)) not in generated:
            path.unlink()
    for name in names:
        if name in preserved:
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        (staging / name).replace(target)
    shutil.rmtree(staging)
