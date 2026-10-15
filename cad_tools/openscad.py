"""Build pinned OpenSCAD releases and run their headless command-line exports."""

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from cad_tools.execution import require_files, run
from cad_tools.workspace import workspace_root

VERSION = "2026.10-TEST3"

# Headless upstream build dependencies for Debian 13 / Ubuntu 24.04.
BUILD_PACKAGES = (
    "bison",
    "build-essential",
    "clang",
    "cmake",
    "flex",
    "gettext",
    "git",
    "lib3mf-dev",
    "libboost-program-options-dev",
    "libboost-regex-dev",
    "libcairo2-dev",
    "libcgal-dev",
    "libdouble-conversion-dev",
    "libeigen3-dev",
    "libfontconfig-dev",
    "libfreetype-dev",
    "libglib2.0-dev",
    "libgmp-dev",
    "libharfbuzz-dev",
    "libmpfr-dev",
    "libssl-dev",
    "libtbb-dev",
    "libxml2-dev",
    "libzip-dev",
    "ninja-build",
    "pkg-config",
)


def install_root() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache.expanduser().resolve() / "cad-tools" / "openscad"


def release_directory() -> Path:
    return install_root() / f"openscad-{VERSION}"


def build_prerequisites() -> tuple[str, ...]:
    """A reusable installed release needs no compiler or development packages."""
    if platform.system() == "Darwin":
        openscad_path()
        return ()
    executable = release_directory() / "bin" / "openscad"
    if executable.is_file():
        if openscad_version(executable) != VERSION:
            raise ValueError(f"Expected OpenSCAD {VERSION} in {executable}")
        return ()
    return BUILD_PACKAGES


def install() -> Path:
    """Use the installed macOS app or build and cache the exact Linux release."""
    if platform.system() == "Darwin":
        return openscad_path()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("OpenSCAD installation requires Linux x86_64")
    destination = release_directory()
    root = destination.parent
    root.mkdir(parents=True, exist_ok=True)
    staging = workspace_root() / "install-openscad"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    if not destination.exists():
        for name in ("clang", "clang++", "cmake", "git", "msgfmt", "ninja"):
            if not shutil.which(name):
                raise FileNotFoundError(f"Missing {name}; install the openscad.py build-prerequisites packages")
        print(f"Building OpenSCAD {VERSION} (CGAL and Manifold)...", flush=True)
        source, build, installed = (staging / name for name in ("source", "build", "installed"))
        log = staging / "install.log"
        environment = dict(os.environ)
        run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                f"openscad-{VERSION}",
                "--recurse-submodules",
                "--shallow-submodules",
                "https://github.com/openscad/openscad.git",
                str(source),
            ],
            log,
            staging,
            environment,
        )
        run(
            [
                "cmake",
                "-S",
                str(source),
                "-B",
                str(build),
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_COMPILER=clang",
                "-DCMAKE_CXX_COMPILER=clang++",
                "-DNULLGL=ON",
                "-DENABLE_TESTS=OFF",
                "-DEXPERIMENTAL=ON",
                "-DENABLE_CGAL=ON",
                "-DENABLE_MANIFOLD=ON",
                "-DBoost_USE_STATIC_LIBS=ON",
                f"-DOPENSCAD_VERSION={VERSION}",
                f"-DCMAKE_INSTALL_PREFIX={installed}",
            ],
            log,
            staging,
            environment,
        )
        run(["cmake", "--build", str(build)], log, staging, environment)
        run(["cmake", "--install", str(build)], log, staging, environment)
        executable = installed / "bin" / "openscad"
        actual = openscad_version(executable)
        if actual != VERSION:
            raise ValueError(f"Expected OpenSCAD {VERSION}, built {actual}")
        shutil.move(installed, destination)
    else:
        actual = openscad_version(destination / "bin" / "openscad")
        if actual != VERSION:
            raise ValueError(f"Expected OpenSCAD {VERSION}, found {actual} in {destination}")
    return destination / "bin" / "openscad"


def openscad_path() -> Path:
    executable = Path(
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
        if platform.system() == "Darwin"
        else release_directory() / "bin" / "openscad"
    )
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError("OpenSCAD executable not found; run make prepare or openscad.py install")
    actual = openscad_version(executable)
    if actual != VERSION:
        raise ValueError(f"Expected OpenSCAD {VERSION}, found {actual} at {executable}")
    return executable


def openscad_environment() -> dict[str, str]:
    """Use the caller's library paths for exports and static analysis."""
    return {**os.environ, "QT_QPA_PLATFORM": "offscreen"}


def run_openscad(executable: Path, arguments: list[str]) -> str:
    environment = openscad_environment()
    log = workspace_root() / "openscad/command.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w+b") as diagnostics:
        result = subprocess.run(
            [str(executable), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=diagnostics,
            stderr=subprocess.STDOUT,
            env=environment,
            timeout=120,
            check=False,
        )
        diagnostics.seek(0)
        output = diagnostics.read(1024 * 1024 + 1)
    if len(output) > 1024 * 1024:
        raise RuntimeError("OpenSCAD diagnostics exceeded 1 MiB")
    text = output.decode("utf-8", errors="replace")
    # Some export modes report assertion failures without a nonzero exit code.
    if result.returncode or "ERROR:" in text or "WARNING:" in text:
        raise RuntimeError(f"OpenSCAD failed ({result.returncode}): {text}")
    return text


def openscad_version(executable: Path) -> str:
    output = run_openscad(executable, ["--version"])
    match = re.search(r"OpenSCAD version (\S+)", output)
    if not match:
        raise ValueError(f"Cannot identify OpenSCAD version: {output}")
    return match.group(1)


def export_openscad(executable: Path, source: Path, output: Path, definitions: list[str]) -> str:
    """Publish a successful export while preserving an existing output on failure."""
    arguments = ["--hardwarnings"]
    if output.suffix.lower() == ".stl":
        arguments.extend(["--export-format", "binstl"])
    for definition in definitions:
        arguments.extend(["-D", definition])
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = workspace_root() / "openscad/export"
    staging.mkdir(parents=True, exist_ok=True)
    generated = staging / destination.name
    generated.unlink(missing_ok=True)
    diagnostics = run_openscad(
        executable,
        [*arguments, "-o", str(generated), str(source.resolve(strict=True))],
    )
    require_files(generated.parent, (generated.name,))
    generated.replace(destination)
    return diagnostics
