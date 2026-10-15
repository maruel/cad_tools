"""Render diagnostic deposited centerlines from an already validated Studio slice."""

import math
import re
from pathlib import Path

from PIL import Image, ImageDraw


def render_layers(
    path: Path,
    destination: Path,
    heights: list[float],
    bounds: tuple[float, float, float, float],
    inspection: tuple[float, float, float, float],
    z_band: list[float],
) -> tuple[float, dict[str, float]]:
    """Return longest bridge-move chord and filament feed by feature in the inspection band."""
    scale = min(450 / (bounds[1] - bounds[0]), 450 / (bounds[3] - bounds[2]))
    tiles = [Image.new("RGB", (500, 500), "white") for _ in heights]
    drawings = [ImageDraw.Draw(tile) for tile in tiles]
    colors = {"Sparse infill": "#2478cb", "Internal solid infill": "#e6a342", "Bridge": "#c52727"}
    x = y = z = 0.0
    absolute_xy = True
    feature = ""
    longest_bridge = 0.0
    inspection_lengths: dict[str, float] = {}
    with path.open() as stream:
        for line in stream:
            if line.startswith("; Z_HEIGHT: "):
                z = float(line.split(":", 1)[1])
            elif line.startswith("; FEATURE: "):
                feature = line.split(":", 1)[1].strip()
            code = line.split(";", 1)[0].strip().split(maxsplit=1)
            if code and code[0] in ("G90", "G91"):
                absolute_xy = code[0] == "G90"
            if not code or code[0] not in ("G0", "G1", "G2", "G3", "G92"):
                continue
            axes = {name: float(value) for name, value in re.findall(r"\b([EIJXY])([-+]?(?:\d*\.)?\d+)", line)}
            start = (x, y)
            if absolute_xy or code[0] == "G92":
                x, y = axes.get("X", x), axes.get("Y", y)
            else:
                x, y = x + axes.get("X", 0), y + axes.get("Y", 0)
            if (
                code[0] == "G92"
                or axes.get("E", 0) <= 0
                or feature in ("", "Custom", "Flush", "Prime tower")
                or not (code[0] in ("G2", "G3") or "X" in axes or "Y" in axes)
            ):
                continue
            if feature == "Bridge" and bounds[0] <= x <= bounds[1] and bounds[2] <= y <= bounds[3]:
                longest_bridge = max(longest_bridge, math.dist(start, (x, y)))
            if (
                z_band[0] <= z <= z_band[1]
                and inspection[0] <= x <= inspection[1]
                and inspection[2] <= y <= inspection[3]
            ):
                inspection_lengths[feature] = inspection_lengths.get(feature, 0) + axes["E"]
            if not any(abs(z - height) < 0.01 for height in heights):
                continue
            points = [start, (x, y)]
            if code[0] in ("G2", "G3"):
                cx, cy = start[0] + axes.get("I", 0), start[1] + axes.get("J", 0)
                radius = math.dist(start, (cx, cy))
                angle = math.atan2(start[1] - cy, start[0] - cx)
                finish = math.atan2(y - cy, x - cx)
                direction = -1 if code[0] == "G2" else 1
                sweep = math.tau if start == (x, y) else (direction * (finish - angle)) % math.tau
                steps = max(2, math.ceil(sweep * radius / 0.25))
                points = [
                    (
                        cx + radius * math.cos(angle + direction * sweep * i / steps),
                        cy + radius * math.sin(angle + direction * sweep * i / steps),
                    )
                    for i in range(steps + 1)
                ]
            for height, drawing in zip(heights, drawings, strict=True):
                if abs(z - height) < 0.01:
                    drawing.line(
                        [(25 + (px - bounds[0]) * scale, 470 - (py - bounds[2]) * scale) for px, py in points],
                        fill=colors.get(feature, "#333333"),
                        width=2,
                    )
    rows = math.ceil(len(heights) / 2)
    sheet = Image.new("RGB", (1000, rows * 500 + 40), "#edf0f3")
    for index, (tile, height) in enumerate(zip(tiles, heights, strict=True)):
        ImageDraw.Draw(tile).text((15, 15), f"Z = {height:g} mm", fill="black")
        sheet.paste(tile, (index % 2 * 500, index // 2 * 500))
    ImageDraw.Draw(sheet).text(
        (15, rows * 500 + 15), "Blue: sparse / orange: solid / red: bridges / dark: walls and supports", fill="black"
    )
    sheet.save(destination, lossless=True)
    return longest_bridge, inspection_lengths
