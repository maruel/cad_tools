"""Headless command preparation and cancellable execution."""

import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from cad_tools.workspace import workspace_root


def headless_command(executable: Path, *arguments: str) -> list[str]:
    """Prepare a CLI invocation, supplying a display on headless Linux."""
    command = [str(executable), *arguments]
    if sys.platform == "linux" and not os.environ.get("DISPLAY"):
        xvfb = shutil.which("xvfb-run")
        if not xvfb:
            raise FileNotFoundError("Headless Linux tools require xvfb-run; install xvfb and xauth")
        command = [xvfb, "-a", *command]
    return command


def require_files(directory: Path, names: tuple[str, ...]) -> None:
    for name in names:
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing or empty generated artifact: {path}")


def run(command: list[str], log: Path, cwd: Path, environment: dict[str, str]) -> None:
    """Stream diagnostics to disk and terminate the entire tool on interruption."""
    with log.open("a", encoding="utf-8") as output:
        output.write(f"Command: {json.dumps(command)}\n")
        output.flush()
        with subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=environment,
        ) as process:
            try:
                result = process.wait()
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                # A launcher can exit on SIGTERM while a worker ignores it. Kill
                # remaining members of the session before deleting their staging.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise
    if result:
        with log.open("rb") as diagnostics:
            diagnostics.seek(max(0, log.stat().st_size - 16384))
            tail = diagnostics.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Tool exited with code {result}: {command[0]}\n{tail}")


def interrupted(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def run_console(command: list[str], cwd: Path) -> None:
    """Run a tool with cancellation handling and stream its completed diagnostics."""
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        log = workspace_root() / "command.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("", encoding="utf-8")
        run(command, log, cwd, dict(os.environ))
        with log.open(encoding="utf-8") as output:
            shutil.copyfileobj(output, sys.stdout)
    finally:
        signal.signal(signal.SIGTERM, previous)
