"""Pinned source formatting and static analysis, independent of the CAD executable."""

import filecmp
import os
import platform
import re
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from cad_tools.openscad import openscad_environment
from cad_tools.workspace import workspace_root

SCADFORMAT_VERSION = "v0.10"
# Transform chains are idiomatic SCAD; globals include CLI-overridable parameters,
# and documentation uses ordinary comments rather than SCA2D's docstring dialect.
SCA2D_IGNORE = ("I0004", "I3001", "I4001", "I4002", "I4003")


def _tool_command(*arguments: str) -> list[str]:
    return [
        "uv",
        "run",
        "--locked",
        "--quiet",
        *arguments,
    ]


def _formatter_directory() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache.expanduser().resolve() / "cad-tools" / "scadformat" / SCADFORMAT_VERSION


def formatter_path() -> Path:
    executable = _formatter_directory() / "scadformat"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError("SCADFormat executable not found; run openscad.py install")
    return executable


def formatter_version(executable: Path) -> str:
    # Upstream has no --version option; its startup banner reports the embedded tag.
    log = workspace_root() / "scadformat/version.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w+b") as diagnostics:
        subprocess.run(
            [str(executable)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=diagnostics,
            check=True,
            timeout=30,
        )
        diagnostics.seek(0)
        banner = diagnostics.read(65537)
    match = re.search(rb"\bSCADFormat (v\d+\.\d+(?:\.\d+)?)\b", banner)
    if len(banner) > 65536 or not match:
        raise ValueError(f"Cannot identify SCADFormat version from {executable}")
    return match[1].decode("ascii")


def install_source_tools() -> None:
    """Cache the release binary and check the locked SCA2D environment."""
    destination = _formatter_directory()
    executable = destination / "scadformat"
    if not executable.exists():
        assets = {("Darwin", "arm64"): "macos.zip", ("Linux", "x86_64"): "linux.zip"}
        asset = assets.get((platform.system(), platform.machine()))
        if asset is None:
            raise ValueError("SCADFormat binaries support Linux x86_64 and macOS arm64")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = workspace_root() / "install-scadformat"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        archive = staging / asset
        url = f"https://github.com/hugheaves/scadformat/releases/download/{SCADFORMAT_VERSION}/{asset}"
        print(f"Downloading SCADFormat {SCADFORMAT_VERSION}", flush=True)
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with zipfile.ZipFile(archive) as release, release.open("scadformat") as source:
            with (staging / "scadformat").open("wb") as output:
                shutil.copyfileobj(source, output)
        (staging / "scadformat").chmod(0o755)
        actual = formatter_version(staging / "scadformat")
        if actual != SCADFORMAT_VERSION:
            raise ValueError(f"Expected SCADFormat {SCADFORMAT_VERSION}, downloaded {actual}")
        archive.unlink()
        shutil.move(staging, destination)
    actual = formatter_version(executable)
    if actual != SCADFORMAT_VERSION:
        raise ValueError(f"Expected SCADFormat {SCADFORMAT_VERSION}, found {actual}")
    sca2d_version = subprocess.check_output(
        _tool_command("python", "-c", "from importlib.metadata import version; print(version('sca2d'))"),
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=60,
    ).strip()
    print(f"SCADFormat {actual}")
    print(f"SCA2D {sca2d_version}")


def source_files(sources: list[Path]) -> list[Path]:
    """Expand explicit source roots, rejecting typos and empty selections."""
    files = set()
    for source in sources:
        source = source.expanduser().resolve(strict=True)
        if source.is_dir():
            files.update(path for path in source.rglob("*.scad") if path.is_file())
        elif source.is_file() and source.suffix == ".scad":
            files.add(source)
        else:
            raise ValueError(f"Expected a .scad file or source directory: {source}")
    if not files:
        raise ValueError("No OpenSCAD source files selected")
    return sorted(files)


def format_sources(sources: list[Path], *, check: bool) -> int:
    """Use stdin/stdout to avoid upstream backups; replace each successful format."""
    files = source_files(sources)
    executable = formatter_path()
    changed = 0
    staging = workspace_root() / "scadformat"
    staging.mkdir(parents=True, exist_ok=True)
    generated = staging / "formatted.scad"
    diagnostics = staging / "formatter.log"
    for source in files:
        with source.open("rb") as original, generated.open("wb") as formatted, diagnostics.open("wb") as log:
            result = subprocess.run(
                [str(executable), "--log-level", "error"],
                stdin=original,
                stdout=formatted,
                stderr=log,
                check=False,
                timeout=60,
            )
        with diagnostics.open("rb") as log:
            errors = log.read(65537)
        if result.returncode or errors:
            # Upstream lexer errors can print to stderr yet still exit zero.
            raise RuntimeError(
                f"SCADFormat failed for {source} ({result.returncode}):\n"
                + errors[:65536].decode("utf-8", errors="replace")
            )
        if filecmp.cmp(source, generated, shallow=False):
            continue
        changed += 1
        if check:
            print(f"Would format {source}")
        else:
            generated.chmod(source.stat().st_mode)
            generated.replace(source)
            print(f"Formatted {source}")
    if not changed:
        print(f"{len(files)} OpenSCAD file(s) already formatted")
    return int(check and changed > 0)


def lint_sources(sources: list[Path], ignored: str) -> int:
    """Show diagnostics and preserve SCA2D's fatal/error failure status."""
    files = source_files(sources)
    status = 0
    for source in files:
        result = subprocess.run(
            _tool_command("sca2d", "--ignore", ignored, str(source)),
            stdin=subprocess.DEVNULL,
            env=openscad_environment(),
            check=False,
        )
        status = max(status, int(result.returncode != 0))
    return status
