"""Conservative deposited-path masks for Studio objects and prime towers."""

import math
from itertools import combinations
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


class Footprints:
    """Rasterize all layers at 0.5 mm; include bead width and pixel-rounding margin."""

    resolution_mm = 0.5

    def __init__(self) -> None:
        self.masks: dict[str, Image.Image] = {}
        self.drawings: dict[str, ImageDraw.ImageDraw] = {}
        self.origin = (0.0, 0.0)
        self.size = (0, 0)

    def initialize(self, polygon: str) -> None:
        points = [tuple(map(float, point.split("x"))) for point in polygon.split(",")]
        if not points or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points):
            raise ValueError("Invalid printable-area polygon")
        self.origin = (min(p[0] for p in points) - 2, min(p[1] for p in points) - 2)
        self.size = tuple(
            math.ceil((max(p[i] for p in points) - self.origin[i] + 2) / self.resolution_mm) for i in (0, 1)
        )
        if min(self.size) <= 0 or math.prod(self.size) > 16_000_000:
            raise ValueError("Printable area exceeds bounded footprint-rendering memory")

    def deposit(self, label: str, points: list[tuple[float, float]], width_mm: float) -> None:
        if not self.size[0]:
            raise ValueError("Missing printable-area header before deposited paths")
        if not math.isfinite(width_mm) or width_mm <= 0:
            raise ValueError("Missing or invalid deposited line width")
        if label not in self.masks:
            self.masks[label] = Image.new("L", self.size)
            self.drawings[label] = ImageDraw.Draw(self.masks[label])
        pixels = [tuple(round((p[i] - self.origin[i]) / self.resolution_mm) for i in (0, 1)) for p in points]
        # Two extra pixels bound coordinate rounding and rasterized line endpoints.
        self.drawings[label].line(pixels, fill=255, width=math.ceil(width_mm / self.resolution_mm) + 2)

    def validate(self, clearance_mm: float) -> None:
        if not math.isfinite(clearance_mm) or clearance_mm < 0:
            raise ValueError("Footprint clearance must be finite and nonnegative")
        radius = math.ceil(clearance_mm / self.resolution_mm)
        expanded = {
            label: mask.filter(ImageFilter.MaxFilter(radius * 2 + 1)) if radius else mask
            for label, mask in self.masks.items()
        }
        for first, second in combinations(self.masks, 2):
            if ImageChops.multiply(expanded[first], self.masks[second]).getbbox():
                raise ValueError(f"Deposited footprints violate {clearance_mm:g} mm clearance: {first}, {second}")

    def render(self, destination: Path) -> None:
        colors = ("#2F78C4", "#BB6C22", "#43966C", "#9158A1", "#D24757", "#6E7B8B")
        image = Image.new("RGB", self.size, "white")
        for index, (label, mask) in enumerate(self.masks.items()):
            image.paste("#333333" if label == "Prime tower" else colors[index % len(colors)], mask=mask)
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        sheet = Image.new("RGB", (image.width, image.height + 40), "white")
        sheet.paste(image)
        ImageDraw.Draw(sheet).text(
            (8, image.height + 8), "Conservative XY paths: model, support, brim and tower / all layers", fill="black"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(destination, lossless=True)


def arc_points(
    start: tuple[float, float], end: tuple[float, float], center_offset: tuple[float, float], clockwise: bool
) -> list[tuple[float, float]]:
    """Sample a verified single-turn XY arc below footprint raster resolution."""
    center = (start[0] + center_offset[0], start[1] + center_offset[1])
    radius = math.dist(start, center)
    first = math.atan2(start[1] - center[1], start[0] - center[0])
    last = math.atan2(end[1] - center[1], end[0] - center[0])
    direction = -1 if clockwise else 1
    sweep = math.tau if start == end else (direction * (last - first)) % math.tau
    steps = max(2, math.ceil(sweep * radius / 0.25))
    return [
        (
            center[0] + radius * math.cos(first + direction * sweep * i / steps),
            center[1] + radius * math.sin(first + direction * sweep * i / steps),
        )
        for i in range(steps + 1)
    ]
