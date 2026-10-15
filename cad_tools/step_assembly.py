"""Write compact AP214 faceted STEP assemblies and validate them with Open CASCADE.

FACETED_BREP represents the source's closed polygon shells directly, without
fitting curves or creating a separate curve network for every edge.
The saved STEP is independently imported to check names, colors, solids, placed
bounds and volume. Open CASCADE's FacetedBrep writer crashes on simple boxes in
the pinned bindings, so serialization uses the polygon representation directly.
"""

import math
import struct
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB, Quantity_TOC_sRGB
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

from cad_tools.assembly import AssemblyPart
from cad_tools.step_archive import pack_step, unpack_step
from cad_tools.stl import TRIANGLE, MeshInfo, validate_stl
from cad_tools.workspace import workspace_root


def rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))


def quoted(value: str) -> str:
    if any(ord(char) > 126 or char == "\\" for char in value):
        value = "\\X2\\" + value.encode("utf-16-be").hex().upper() + "\\X0\\"
    return "'" + value.replace("'", "''") + "'"


def refs(ids: Sequence[int]) -> str:
    return "(" + ",".join(f"#{index}" for index in ids) + ")"


class FacetedStep:
    """Stream standard polygon BREP entities, retaining assembly product structure."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.index = 0
        self.application = self.entity("APPLICATION_CONTEXT('automotive_design')")
        self.entity(
            f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#{self.application})"
        )
        length = self.entity("(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))")
        angle = self.entity("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
        solid_angle = self.entity("(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())")
        tolerance = self.entity(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{length},'accuracy','')")
        self.context = self.entity(
            f"(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{tolerance}))"
            f"GLOBAL_UNIT_ASSIGNED_CONTEXT({refs([length, angle, solid_angle])})REPRESENTATION_CONTEXT('',''))"
        )
        self.origin = self.axis((0, 0, 0))

    def entity(self, value: str) -> int:
        self.index += 1
        self.stream.write(f"#{self.index}={value};\n")
        return self.index

    def point(self, coordinates: tuple[float, ...]) -> int:
        values = ",".join(format(value, ".17E") for value in coordinates)
        return self.entity(f"CARTESIAN_POINT('',({values}))")

    def direction(self, vector: tuple[float, ...]) -> int:
        length = math.hypot(*vector)
        values = ",".join(format(value / length, ".17E") for value in vector)
        return self.entity(f"DIRECTION('',({values}))")

    def axis(self, position: tuple[float, float, float]) -> int:
        point = self.point(position)
        z = self.entity("DIRECTION('',(0.,0.,1.))")
        x = self.entity("DIRECTION('',(1.,0.,0.))")
        return self.entity(f"AXIS2_PLACEMENT_3D('',#{point},#{z},#{x})")

    def product(self, name: str, representation: int) -> int:
        context = self.entity(f"PRODUCT_CONTEXT('',#{self.application},'mechanical')")
        product = self.entity(f"PRODUCT({quoted(name)},{quoted(name)},'',(#{context}))")
        formation = self.entity(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
        definition_context = self.entity(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{self.application},'design')")
        definition = self.entity(f"PRODUCT_DEFINITION('design','',#{formation},#{definition_context})")
        shape = self.entity(f"PRODUCT_DEFINITION_SHAPE('','',#{definition})")
        self.entity(f"SHAPE_DEFINITION_REPRESENTATION(#{shape},#{representation})")
        self.entity(f"PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{product}))")
        return definition

    def mesh(self, path: Path) -> list[int]:
        vertices: dict[tuple[float, ...], int] = {}
        parents = []
        triangles = []

        def root(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        with path.open("rb") as stream:
            count = struct.unpack_from("<I", stream.read(84), 80)[0]
            for _ in range(count):
                triangle = TRIANGLE.unpack(stream.read(TRIANGLE.size))
                ids = []
                for offset in (3, 6, 9):
                    point = triangle[offset : offset + 3]
                    if point not in vertices:
                        vertices[point] = len(parents)
                        parents.append(len(parents))
                    ids.append(vertices[point])
                for index in ids[1:]:
                    parents[root(index)] = root(ids[0])
                triangles.append(ids)
        coordinates = list(vertices)
        points = [self.point(point) for point in coordinates]
        shells = defaultdict(list)
        for triangle in triangles:
            a, b, c = (coordinates[index] for index in triangle)
            u = tuple(b[axis] - a[axis] for axis in range(3))
            v = tuple(c[axis] - a[axis] for axis in range(3))
            normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            # Collinear mesh triangles have no surface or volume to export.
            if math.hypot(*normal) == 0:
                continue
            loop = self.entity(f"POLY_LOOP('',{refs([points[index] for index in triangle])})")
            bound = self.entity(f"FACE_OUTER_BOUND('',#{loop},.T.)")
            directions = [self.direction(vector) for vector in (normal, u)]
            axis = self.entity(f"AXIS2_PLACEMENT_3D('',#{points[triangle[0]]},#{directions[0]},#{directions[1]})")
            plane = self.entity(f"PLANE('',#{axis})")
            face = self.entity(f"FACE_SURFACE('',(#{bound}),#{plane},.T.)")
            shells[root(triangle[0])].append(face)
        solids = []
        for faces in shells.values():
            shell = self.entity(f"CLOSED_SHELL('',{refs(faces)})")
            solids.append(self.entity(f"FACETED_BREP('',#{shell})"))
        return solids

    def color(self, solids: list[int], color: str) -> None:
        srgb = Quantity_Color(*rgb(color), Quantity_TOC_RGB).Values(Quantity_TOC_sRGB)
        colour = self.entity(f"COLOUR_RGB('',{','.join(format(value, '.17E') for value in srgb)})")
        fill_color = self.entity(f"FILL_AREA_STYLE_COLOUR('',#{colour})")
        fill = self.entity(f"FILL_AREA_STYLE('',(#{fill_color}))")
        surface = self.entity(f"SURFACE_STYLE_FILL_AREA(#{fill})")
        side = self.entity(f"SURFACE_SIDE_STYLE('',(#{surface}))")
        usage = self.entity(f"SURFACE_STYLE_USAGE(.BOTH.,#{side})")
        style = self.entity(f"PRESENTATION_STYLE_ASSIGNMENT((#{usage}))")
        items = [self.entity(f"STYLED_ITEM('',(#{style}),#{solid})") for solid in solids]
        self.entity(f"MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',{refs(items)},#{self.context})")

    def view(self, name: str, parts: Sequence[AssemblyPart]) -> None:
        placements = [self.axis(part.translation) for part in parts]
        assembly = self.entity(f"SHAPE_REPRESENTATION('',{refs([self.origin, *placements])},#{self.context})")
        parent = self.product(name, assembly)
        for index, (part, placement) in enumerate(zip(parts, placements, strict=True)):
            solids = self.mesh(part.path)
            representation = self.entity(
                f"FACETED_BREP_SHAPE_REPRESENTATION('',{refs([self.origin, *solids])},#{self.context})"
            )
            definition = self.product(part.name, representation)
            transform = self.entity(f"ITEM_DEFINED_TRANSFORMATION('','',#{self.origin},#{placement})")
            relationship = self.entity(
                f"(REPRESENTATION_RELATIONSHIP('','',#{representation},#{assembly})"
                f"REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#{transform})SHAPE_REPRESENTATION_RELATIONSHIP())"
            )
            occurrence = self.entity(
                f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('{index}',{quoted(part.name)},'',#{parent},#{definition},$)"
            )
            shape = self.entity(f"PRODUCT_DEFINITION_SHAPE('','',#{occurrence})")
            self.entity(f"CONTEXT_DEPENDENT_SHAPE_REPRESENTATION(#{relationship},#{shape})")
            self.color(solids, part.color)


def check_geometry(shape: TopoDS_Shape, info: MeshInfo, part: AssemblyPart) -> None:
    solids = TopExp_Explorer(shape, TopAbs_SOLID)
    if not solids.More() or not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError(f"Invalid STEP solids: {part.name}")
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    actual = (xmin, xmax, ymin, ymax, zmin, zmax)
    expected = tuple(value + part.translation[index // 2] for index, value in enumerate(info.bounds_mm))
    if any(abs(a - b) > 1e-5 for a, b in zip(actual, expected, strict=True)):
        raise ValueError(f"STEP bounds changed: {part.name}: {actual} != {expected}")
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    if not math.isclose(properties.Mass(), info.volume_mm3, rel_tol=1e-7, abs_tol=1e-5):
        raise ValueError(f"STEP volume changed: {part.name}: {properties.Mass()} != {info.volume_mm3}")


def validate_view(path: Path, parts: Sequence[AssemblyPart], meshes: dict[Path, MeshInfo]) -> None:
    document = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    reader = STEPCAFControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone or not reader.Transfer(document):
        raise ValueError(f"Cannot reopen STEP assembly: {path}")
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    if roots.Length() != 1:
        raise ValueError(f"STEP must contain one root assembly: {path}")
    labels = TDF_LabelSequence()
    XCAFDoc_ShapeTool.GetComponents_s(roots.Value(1), labels)
    expected = {part.name: part for part in parts}
    if labels.Length() != len(expected):
        raise ValueError(f"STEP part count changed: {path}")
    for index in range(1, labels.Length() + 1):
        instance = labels.Value(index)
        definition = TDF_Label()
        if not XCAFDoc_ShapeTool.GetReferredShape_s(instance, definition):
            raise ValueError(f"STEP component has no shape: {path}")
        name = TDataStd_Name()
        if not definition.FindAttribute(TDataStd_Name.GetID_s(), name):
            raise ValueError(f"STEP component lost its name: {path}")
        part = expected.pop(name.Get().ToExtString())
        solids = TopExp_Explorer(tool.GetShape_s(definition), TopAbs_SOLID)
        while solids.More():
            color = Quantity_Color()
            if not colors.GetColor(solids.Current(), XCAFDoc_ColorSurf, color) or any(
                abs(a - b) > 1e-5 for a, b in zip(color.Values(Quantity_TOC_RGB), rgb(part.color), strict=True)
            ):
                raise ValueError(f"STEP solid lost its color: {part.name}")
            solids.Next()
        check_geometry(tool.GetShape_s(instance), meshes[part.path], part)


def write_views(directory: Path, views: dict[str, Sequence[AssemblyPart]]) -> None:
    """Publish STPZ only after reopening its archive and importing the saved STEP."""
    if not views or any(Path(name).name != name or Path(name).suffix != ".stpz" for name in views):
        raise ValueError("STEP view names must be local .stpz filenames")
    for parts in views.values():
        if not parts or len({part.name for part in parts}) != len(parts):
            raise ValueError("Each STEP view requires uniquely named parts")
        if any(len(part.color) != 7 for part in parts):
            raise ValueError("STEP solids require opaque RGB colors")
    paths = sorted({part.path for parts in views.values() for part in parts})
    meshes = {path: validate_stl(path, require_one_shell=False) for path in paths}
    directory.mkdir(parents=True, exist_ok=True)
    staging = workspace_root() / "step-assembly"
    staging.mkdir(parents=True, exist_ok=True)
    for filename, parts in views.items():
        with tempfile.TemporaryDirectory(dir=staging) as scratch:
            archive = Path(scratch) / filename
            temporary = archive.with_suffix(".stp")
            reopened = Path(scratch) / f"{archive.stem}.reopened.stp"
            with temporary.open("w", encoding="ascii") as stream:
                stream.write(
                    "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Faceted assembly'),'2;1');\n"
                    "FILE_NAME('','1970-01-01T00:00:00',(''),(''),'cad_tools','cad_tools','');\n"
                    "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"
                )
                FacetedStep(stream).view(Path(filename).stem, parts)
                stream.write("ENDSEC;\nEND-ISO-10303-21;\n")
            pack_step(temporary, archive)
            unpack_step(archive, reopened)
            print(f"Validating saved STPZ: {filename} ({archive.stat().st_size / 1024**2:.1f} MiB)", flush=True)
            validate_view(reopened, parts, meshes)
            archive.replace(directory / filename)
