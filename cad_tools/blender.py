"""Install official Blender releases and invoke headless project recipes."""

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from cad_tools.execution import headless_command
from cad_tools.workspace import workspace_root

VERSION = "5.2.2"


def install_root() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache.expanduser().resolve() / "cad-tools" / "blender"


def blender_path() -> Path:
    executable = (
        Path("/Applications/Blender.app/Contents/MacOS/Blender")
        if platform.system() == "Darwin"
        else install_root() / f"blender-{VERSION}-linux-x64" / "blender"
    )
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError(
            f"Blender executable not found at {executable}; run your project's preparation command "
            "or blender.py install"
        )
    actual = blender_version(executable)
    if actual != VERSION:
        raise ValueError(f"Expected Blender {VERSION}, found {actual} at {executable}")
    return executable


def blender_version(executable: Path) -> str:
    log = workspace_root() / "blender/version.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w+", encoding="utf-8") as output:
        subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=60,
        )
        output.seek(0)
        banner = output.read(1024 * 1024 + 1)
    versions = re.findall(r"^Blender (\d+\.\d+\.\d+)\b", banner, re.MULTILINE)
    if len(banner) > 1024 * 1024 or len(versions) != 1:
        raise ValueError(f"Could not identify Blender version from {executable}")
    return versions[0]


def install() -> Path:
    """Use the installed macOS app or cache the pinned Linux x64 release."""
    if platform.system() == "Darwin":
        return blender_path()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("Blender installation requires Linux x86_64")
    name = f"blender-{VERSION}-linux-x64"
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    staging = workspace_root() / "install-blender"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    if not destination.exists():
        series = VERSION.rsplit(".", 1)[0]
        base_url = f"https://download.blender.org/release/Blender{series}"
        archive_name = f"{name}.tar.xz"
        checksum_request = urllib.request.Request(
            f"{base_url}/blender-{VERSION}.sha256", headers={"User-Agent": "cad-tools"}
        )
        with urllib.request.urlopen(checksum_request, timeout=60) as response:
            checksums = response.read(64 * 1024 + 1)
        if len(checksums) > 64 * 1024:
            raise ValueError("Blender checksum file exceeds 64 KiB")
        matches = re.findall(
            rf"^([a-f0-9]{{64}})\s+{re.escape(archive_name)}$", checksums.decode("ascii"), re.MULTILINE
        )
        if len(matches) != 1:
            raise ValueError(f"Expected one published checksum for {archive_name}")
        digest = hashlib.sha256()
        archive_path = staging / archive_name
        print(f"Downloading {base_url}/{archive_name}", flush=True)
        archive_request = urllib.request.Request(f"{base_url}/{archive_name}", headers={"User-Agent": "cad-tools"})
        with (
            urllib.request.urlopen(archive_request, timeout=60) as response,
            archive_path.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != matches[0]:
            raise ValueError("Blender archive checksum does not match the official release")
        with tarfile.open(archive_path) as archive:
            archive.extractall(staging, filter="data")
        extracted = staging / name
        actual = blender_version(extracted / "blender")
        if actual != VERSION:
            raise ValueError(f"Expected Blender {VERSION}, downloaded {actual}")
        shutil.move(extracted, destination)
    else:
        actual = blender_version(destination / "blender")
        if actual != VERSION:
            raise ValueError(f"Expected Blender {VERSION}, found {actual} in {destination}")
    return destination / "blender"


def blender_recipe_command(blender: Path, recipe: Path, *arguments: str) -> list[str]:
    return headless_command(
        blender,
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(recipe),
        "--",
        *arguments,
    )
