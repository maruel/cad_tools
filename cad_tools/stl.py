"""Validate closed binary STL meshes and combine already validated preview parts."""

import math
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from cad_tools.workspace import workspace_root

TRIANGLE = struct.Struct("<12fH")


@dataclass(frozen=True)
class MeshInfo:
    triangles: int
    bounds_mm: tuple[float, float, float, float, float, float]
    volume_mm3: float


def validate_stl(path: Path, *, require_one_shell: bool = True) -> MeshInfo:
    """Require closed, consistently oriented shells of finite triangles and positive volume."""
    vertices: dict[tuple[float, ...], int] = {}
    edges: dict[tuple[int, int], list[int]] = {}
    neighbors: dict[int, set[int]] = {}
    volumes = []
    with path.open("rb") as stream:
        header = stream.read(84)
        if len(header) != 84:
            raise ValueError(f"Truncated binary STL: {path}")
        triangles = struct.unpack_from("<I", header, 80)[0]
        if not triangles or path.stat().st_size != 84 + TRIANGLE.size * triangles:
            raise ValueError(f"Invalid binary STL triangle data: {path}")
        for _ in range(triangles):
            record = TRIANGLE.unpack(stream.read(TRIANGLE.size))
            if not all(math.isfinite(value) for value in record[:12]):
                raise ValueError(f"Nonfinite STL coordinates: {path}")
            points = [tuple(record[start : start + 3]) for start in (3, 6, 9)]
            ids = [vertices.setdefault(point, len(vertices)) for point in points]
            if len(set(ids)) != 3:
                raise ValueError(f"Degenerate STL triangle: {path}")
            a, b, c = points
            cross = (
                b[1] * c[2] - b[2] * c[1],
                b[2] * c[0] - b[0] * c[2],
                b[0] * c[1] - b[1] * c[0],
            )
            volumes.append(sum(a[i] * cross[i] for i in range(3)) / 6)
            for first, second in zip(ids, ids[1:] + ids[:1], strict=True):
                edge = (min(first, second), max(first, second))
                counts = edges.setdefault(edge, [0, 0])
                counts[0] += 1
                counts[1] += 1 if first < second else -1
                neighbors.setdefault(first, set()).add(second)
                neighbors.setdefault(second, set()).add(first)
    if any(count != 2 or direction != 0 for count, direction in edges.values()):
        raise ValueError(f"STL has open, nonmanifold or inconsistently oriented edges: {path}")
    visited = set()
    pending = [0]
    while pending:
        current = pending.pop()
        if current not in visited:
            visited.add(current)
            pending.extend(neighbors[current] - visited)
    volume = math.fsum(volumes)
    if (require_one_shell and len(visited) != len(vertices)) or volume <= 0:
        raise ValueError(f"STL must have one connected shell with positive volume: {path}")
    bounds = tuple(
        value
        for axis in range(3)
        for value in (
            min(point[axis] for point in vertices),
            max(point[axis] for point in vertices),
        )
    )
    return MeshInfo(triangles, bounds, volume)


def combine_stl(paths: tuple[Path, ...], output: Path) -> None:
    """Concatenate binary triangle records for assembly previews, preserving coordinates."""
    counts = []
    for path in paths:
        with path.open("rb") as stream:
            counts.append(struct.unpack_from("<I", stream.read(84), 80)[0])
    temporary = workspace_root() / "stl" / f"{output.name}.pending"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("wb") as target:
            target.write(b"CAD assembly preview".ljust(80, b"\0"))
            target.write(struct.pack("<I", sum(counts)))
            for path in paths:
                with path.open("rb") as source:
                    source.seek(84)
                    shutil.copyfileobj(source, target)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
