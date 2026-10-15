"""Project-local temporary files and caches for CAD commands and recipes."""

from pathlib import Path


def workspace_root() -> Path:
    return Path.cwd() / "build"


def prepare_workspace() -> None:
    """Create the project build directory."""
    workspace_root().mkdir(parents=True, exist_ok=True)
