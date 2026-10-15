"""Plain camera, caption and shading choices for reusable Blender preview recipes."""

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RenderStyle:
    color_type: str = "OBJECT"
    light: str = "STUDIO"
    cavity: bool = True
    exposure: float = 0.6
    view_transform: str | None = None
    banner: bool = True
    title_size: float = 0.02
    clip_end: float = 3000


@dataclass(frozen=True)
class PreviewView:
    eye: tuple[float, float, float]
    target: tuple[float, float, float]
    scale: float
    caption: str = ""
    style: str = "shaded"
    up_axis: tuple[float, float, float] = (0, 1, 0)
    resolution: tuple[int, int] = (1000, 1000)
    plate: int | None = None


@dataclass(frozen=True)
class PreviewConfig:
    styles: dict[str, RenderStyle]
    views: dict[str, PreviewView]


def read_previews(path: Path) -> PreviewConfig:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized preview configuration")
    with path.open("rb") as stream:
        values = tomllib.load(stream)
    result = PreviewConfig(
        styles={name: RenderStyle(**row) for name, row in values["styles"].items()},
        views={name: PreviewView(**row) for name, row in values["views"].items()},
    )
    for name, view in result.views.items():
        if (
            Path(name).name != name
            or not name
            or view.style not in result.styles
            or any(len(v) != 3 or not all(math.isfinite(n) for n in v) for v in (view.eye, view.target, view.up_axis))
            or not math.isfinite(view.scale)
            or view.scale <= 0
            or len(view.resolution) != 2
            or any(not isinstance(n, int) or n <= 0 for n in view.resolution)
        ):
            raise ValueError(f"Invalid preview: {name}")
    return result
