"""Bambu Studio installation, discovery, version and headless CLI invocation."""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cad_tools.execution import headless_command
from cad_tools.workspace import workspace_root


def install_root() -> Path:
    """Use a user cache independent of whichever project invokes the tools."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache.expanduser().resolve() / "cad-tools" / "bambu-studio"


def studio_version(studio: Path) -> str:
    """Read the invoked executable's version from its CLI help banner.

    Bambu Studio has no advertised --version flag. Do not infer the version from
    the installation directory: custom executables may have unrelated names.
    Even --help writes result.json, so isolate its working directory.
    """
    directory = workspace_root() / "bambu-version"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "help.log").open("w+", encoding="utf-8") as output:
        subprocess.run(
            headless_command(studio.absolute(), "--help"),
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=60,
        )
        output.seek(0)
        # CLI help is small; refuse unbounded diagnostic output.
        help_text = output.read(2 * 1024 * 1024 + 1)
    if len(help_text) > 2 * 1024 * 1024:
        raise ValueError("Bambu Studio help exceeds 2 MiB")
    versions = re.findall(r"^BambuStudio-(\d+\.\d+\.\d+\.\d+):\s*$", help_text, re.MULTILINE)
    if len(versions) != 1:
        raise ValueError(f"Could not identify Bambu Studio version from {studio} --help")
    return versions[0]


def studio_path() -> Path:
    cached = install_root() / "current"
    candidates = [
        cached / "BambuStudio.app/Contents/MacOS/BambuStudio" if platform.system() == "Darwin" else cached / "AppRun"
    ]
    for name in ("bambu-studio", "BambuStudio", "Bambu_Studio.AppImage"):
        if executable := shutil.which(name):
            candidates.append(Path(executable))
    candidates.append(Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"))
    # Preserve launcher names; symlink targets may dispatch by argv[0].
    studio = next((path.absolute() for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    if studio is None:
        raise FileNotFoundError("Bambu Studio executable not found; run bambu_studio.py install")
    return studio


def studio_profiles(studio: Path) -> Path:
    candidates = (
        root / suffix
        # Resource lookup follows the target; invocation keeps the launcher's name.
        for root in studio.resolve().parents
        for suffix in (
            "Resources/profiles/BBL",
            "resources/profiles/BBL",
            "share/BambuStudio/profiles/BBL",
            "share/bambu-studio/resources/profiles/BBL",
        )
    )
    profiles = next((path for path in candidates if path.is_dir()), None)
    if profiles is None:
        raise FileNotFoundError("BBL profiles not found; run bambu_studio.py install")
    return profiles


def studio_paths() -> tuple[Path, Path]:
    studio = studio_path()
    return studio, studio_profiles(studio)


def studio_command(studio: Path, *arguments: str) -> list[str]:
    return headless_command(studio, *arguments)


RELEASES_API = "https://api.github.com/repos/bambulab/BambuStudio/releases"


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    digest: str | None


def release_asset() -> Asset:
    """Select the latest stable release for the current platform."""
    release_url = f"{RELEASES_API}/latest"
    request = urllib.request.Request(
        release_url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "cad-tools"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        # GitHub release metadata is small; reject unexpectedly large responses.
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("GitHub release metadata exceeds 2 MiB")
    release = json.loads(body)
    tag = release["tag_name"]
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+\.\d+", tag):
        raise ValueError("Invalid Bambu release tag in GitHub metadata")
    pattern = (
        rf"Bambu_Studio_mac-{re.escape(tag)}-\d+\.dmg"
        if platform.system() == "Darwin"
        else rf"BambuStudio_ubuntu24\.04-{re.escape(tag)}-\d+\.AppImage"
    )
    assets = [
        Asset(a["name"], a["browser_download_url"], a["size"], a.get("digest"))
        for a in release["assets"]
        if re.fullmatch(pattern, a["name"])
    ]
    if len(assets) != 1:
        raise ValueError(f"Expected one matching installer in {tag}, found {len(assets)}; check {release_url}")
    asset = assets[0]
    if Path(asset.name).name != asset.name or asset.size <= 0:
        raise ValueError("Invalid release asset name or size")
    expected_url = f"https://github.com/bambulab/BambuStudio/releases/download/{tag}/{asset.name}"
    if asset.url != expected_url:
        raise ValueError("Unexpected release asset download URL")
    return asset


def download(asset: Asset, destination: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    with (
        urllib.request.urlopen(asset.url, timeout=60) as response,
        destination.open("wb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > asset.size:
                raise ValueError("Installer exceeds its published size")
            digest.update(chunk)
            output.write(chunk)
    if size != asset.size:
        raise ValueError(f"Incomplete installer download: {size} of {asset.size} bytes")
    if asset.digest and asset.digest != f"sha256:{digest.hexdigest()}":
        raise ValueError("Installer checksum does not match GitHub release metadata")


def validate_install(directory: Path) -> None:
    if platform.system() == "Darwin":
        application = directory / "BambuStudio.app/Contents"
        required = (application / "MacOS/BambuStudio",)
        profiles = application / "Resources/profiles/BBL"
    else:
        required = (directory / "AppRun", directory / "bin/bambu-studio")
        profiles = directory / "resources/profiles/BBL"
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f"Incomplete Bambu Studio installation: {path}")
    if not profiles.is_dir():
        raise FileNotFoundError(f"Bambu Studio profiles missing from {directory}")


def install() -> Path:
    """Use an existing Studio installation, otherwise download the latest release."""
    try:
        return studio_path()
    except FileNotFoundError:
        system = platform.system()
    if system != "Darwin" and (system != "Linux" or platform.machine() not in ("amd64", "x86_64")):
        raise ValueError("Bambu Studio installation requires macOS or x86_64 Linux")
    asset = release_asset()
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / Path(asset.name).stem
    staging = workspace_root() / "install-bambu-studio"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    if not destination.exists():
        archive = staging / asset.name
        print(f"Downloading {asset.url}", flush=True)
        download(asset, archive)
        with (staging / "extract.log").open("w+") as log:
            if system == "Darwin":
                mount = staging / "mounted"
                mount.mkdir()
                subprocess.run(
                    ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(archive)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
                try:
                    validate_install(mount)
                    extracted = staging / "installed"
                    extracted.mkdir()
                    shutil.copytree(mount / "BambuStudio.app", extracted / "BambuStudio.app", symlinks=True)
                finally:
                    subprocess.run(["hdiutil", "detach", str(mount)], stdout=log, stderr=subprocess.STDOUT, check=True)
            else:
                archive.chmod(0o755)
                result = subprocess.run(
                    [str(archive), "--appimage-extract"],
                    cwd=staging,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                if result.returncode:
                    log.seek(0)
                    raise RuntimeError(f"AppImage extraction failed: {log.read(4096)}")
                extracted = staging / "squashfs-root"
            validate_install(extracted)
            shutil.move(extracted, destination)
    validate_install(destination)
    link = root / "current"
    link.unlink(missing_ok=True)
    link.symlink_to(destination.name)
    return studio_path()
