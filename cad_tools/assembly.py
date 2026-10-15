"""Write named, colored inspection assemblies from assembly-coordinate binary STL."""

import math
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from cad_tools.stl import TRIANGLE
from cad_tools.workspace import workspace_root

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
RELATIONSHIPS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)


@dataclass(frozen=True)
class AssemblyPart:
    name: str
    path: Path
    color: str
    translation: tuple[float, float, float] = (0, 0, 0)

    def __post_init__(self) -> None:
        if not self.name.strip() or any(ord(char) < 32 for char in self.name):
            raise ValueError("Assembly parts require nonempty XML-safe names")
        if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", self.color) is None:
            raise ValueError(f"Invalid assembly display color: {self.color}")
        if len(self.translation) != 3 or not all(math.isfinite(value) for value in self.translation):
            raise ValueError("Assembly translations require three finite coordinates")


def _read_vertices(path: Path) -> dict[tuple[float, float, float], int]:
    """Index one bounded STL at a time; callers own shell/assembly validation."""
    vertices = {}
    with path.open("rb") as stream:
        header = stream.read(84)
        if len(header) != 84:
            raise ValueError(f"Truncated binary STL: {path}")
        count = struct.unpack_from("<I", header, 80)[0]
        if not count or path.stat().st_size != 84 + TRIANGLE.size * count:
            raise ValueError(f"Invalid binary STL triangle data: {path}")
        for _ in range(count):
            record = TRIANGLE.unpack(stream.read(TRIANGLE.size))
            for start in (3, 6, 9):
                point = record[start : start + 3]
                if not all(math.isfinite(value) for value in point):
                    raise ValueError(f"Nonfinite assembly coordinates: {path}")
                vertices.setdefault(point, len(vertices))
    return vertices


def write_assembly(path: Path, parts: Sequence[AssemblyPart]) -> None:
    """Keep STL geometry exact; only inspection placements affect build transforms."""
    if not parts or len({part.name for part in parts}) != len(parts):
        raise ValueError("An assembly requires uniquely named parts")
    if any(part.path.resolve() == path.resolve() for part in parts):
        raise ValueError("Assembly output overlaps its source mesh")
    temporary = workspace_root() / "assembly" / f"{path.name}.pending"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(ZipInfo("[Content_Types].xml"), CONTENT_TYPES, compress_type=ZIP_DEFLATED)
            archive.writestr(ZipInfo("_rels/.rels"), RELATIONSHIPS, compress_type=ZIP_DEFLATED)
            with archive.open("3D/3dmodel.model", "w") as stream:
                stream.write(
                    f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{CORE}">'
                    '<resources><basematerials id="1">'.encode()
                )
                for part in parts:
                    stream.write(f"<base name={quoteattr(part.name)} displaycolor={quoteattr(part.color)}/>".encode())
                stream.write(b"</basematerials>")
                for index, part in enumerate(parts):
                    vertices = _read_vertices(part.path)
                    stream.write(
                        f'<object id="{index + 2}" type="model" name={quoteattr(part.name)} '
                        f'pid="1" pindex="{index}"><mesh><vertices>'.encode()
                    )
                    for x, y, z in vertices:
                        stream.write(f'<vertex x="{x:.17g}" y="{y:.17g}" z="{z:.17g}"/>'.encode())
                    stream.write(b"</vertices><triangles>")
                    with part.path.open("rb") as source:
                        source.seek(84)
                        while record_bytes := source.read(TRIANGLE.size):
                            record = TRIANGLE.unpack(record_bytes)
                            a, b, c = (vertices[record[start : start + 3]] for start in (3, 6, 9))
                            stream.write(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'.encode())
                    stream.write(b"</triangles></mesh></object>")
                stream.write(b"</resources><build>")
                for index, part in enumerate(parts):
                    offset = " ".join(f"{value:.17g}" for value in part.translation)
                    stream.write(f'<item objectid="{index + 2}" transform="1 0 0 0 1 0 0 0 1 {offset}"/>'.encode())
                stream.write(b"</build></model>")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
