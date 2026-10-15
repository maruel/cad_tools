"""Resolve authored print jobs, package Studio projects and verify actual toolpaths."""

from __future__ import annotations

import json
import math
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cad_tools.bambu_profiles import Purge, configure_flush_volumes, resolve_profile, validate_flush_volumes
from cad_tools.bambu_project import (
    PresetOverrides,
    ProjectPatch,
    filament_setting_indices,
    inspect_component,
    metadata,
    package_project,
)
from cad_tools.bambu_slicing import SLICED_PROJECT, read_slice
from cad_tools.bambu_studio import studio_command, studio_version
from cad_tools.execution import run
from cad_tools.gcode import Material
from cad_tools.metadata import archive_metadata
from cad_tools.reports import dumps_report
from cad_tools.stl import MeshInfo, validate_stl
from cad_tools.stock import Stock

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
LAYER_RANGES_FILE = "Metadata/layer_config_ranges.xml"


@dataclass(frozen=True)
class FilamentRef:
    type: str
    color: str

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v.strip() for v in (self.type, self.color)):
            raise ValueError("Filaments require a type and color")

    def __str__(self) -> str:
        return f"{self.type} / {self.color}"


type PrintSettings = dict[str, str | FilamentRef]
SUPPORT_MATERIAL_KEYS = {"support_filament", "support_interface_filament"}


def read_settings(settings: dict[str, object]) -> PrintSettings:
    result = {}
    for key, value in settings.items():
        if key in SUPPORT_MATERIAL_KEYS:
            if isinstance(value, dict):
                result[key] = FilamentRef(**value)
            elif value == "auto":
                result[key] = value
            else:
                raise ValueError(f"{key} requires type/color or auto")
        elif isinstance(value, str):
            result[key] = value
        else:
            raise ValueError(f"Native process setting {key} must be a string")
    return result


def native_settings(settings: PrintSettings, filaments: list[FilamentRef]) -> dict[str, str]:
    """Convert identities to numeric IDs only for Studio's native formats."""
    return {
        key: str(filaments.index(value) + 1)
        if isinstance(value, FilamentRef)
        else "0"
        if key in SUPPORT_MATERIAL_KEYS and value == "auto"
        else value
        for key, value in settings.items()
    }


@dataclass(frozen=True)
class PrintObject:
    path: str
    filament: FilamentRef
    x: float
    y: float
    assembly: int = 0
    print_params: PrintSettings = field(default_factory=dict)

    def bambu(self, directory: Path, print_params: PrintSettings, filaments: list[FilamentRef]) -> dict[str, object]:
        result = {
            "path": str(directory / self.path),
            "count": 1,
            "filaments": [filaments.index(self.filament) + 1],
            "pos_x": [self.x],
            "pos_y": [self.y],
            "assemble_index": [self.assembly],
        }
        if not self.assembly:
            result["print_params"] = native_settings(print_params | self.print_params, filaments)
        return result


@dataclass(frozen=True)
class LayerRange:
    min_z_mm: float
    max_z_mm: float
    layer_height_mm: float


@dataclass(frozen=True)
class PrintPlate:
    name: str
    objects: list[PrintObject]
    print_params: PrintSettings = field(default_factory=dict)
    layer_ranges: list[LayerRange] = field(default_factory=list)
    prime_tower_xy: tuple[float, float] | None = None
    separate_interface_hotends: bool = False
    minimum_clearance_mm: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_clearance_mm) or self.minimum_clearance_mm < 0:
            raise ValueError("Plate clearance must be finite and nonnegative")


@dataclass(frozen=True)
class Filament:
    type: str
    color: str
    brand: str
    profile: str
    extruder: int
    overrides: dict[str, object] = field(default_factory=dict)

    @property
    def reference(self) -> FilamentRef:
        return FilamentRef(self.type, self.color)


@dataclass(frozen=True)
class ResolvedFilament:
    type: str
    color: str
    color_hex: str
    extruder: int

    @property
    def reference(self) -> FilamentRef:
        return FilamentRef(self.type, self.color)


@dataclass(frozen=True)
class PrintLayout:
    filaments: list[ResolvedFilament]
    plates: list[PrintPlate]

    @property
    def references(self) -> list[FilamentRef]:
        return [f.reference for f in self.filaments]

    @property
    def filament_colors(self) -> list[str]:
        return [f.color_hex for f in self.filaments]

    @property
    def filament_map(self) -> list[int]:
        return [f.extruder for f in self.filaments]

    def color(self, filament: FilamentRef) -> str:
        return self.filaments[self.references.index(filament)].color_hex

    def color_for_part(self, path: str) -> str:
        references = {o.filament for p in self.plates for o in p.objects if o.path == path}
        if len(references) != 1:
            raise ValueError(f"Expected one material for {path}: {references}")
        return self.color(next(iter(references)))

    def write(self, path: Path) -> None:
        path.write_text(dumps_report(asdict(self)))


@dataclass(frozen=True)
class FilamentUsage:
    type: str
    color: str
    total_used_g: float


@dataclass(frozen=True)
class PlateReport:
    name: str
    filaments: list[FilamentUsage]
    support_mass_g: float
    support_interface_mass_g: float
    supported_layers: int
    prime_tower_mass_g: float
    layer_heights_mm: list[float]
    extrusion_bounds_xy_mm: dict[str, list[float]]
    minimum_clearance_checked_mm: float


@dataclass(frozen=True)
class SliceReport:
    plates: list[PlateReport]
    filament_usage: list[FilamentUsage]


@dataclass(frozen=True)
class PurgeTransition:
    source: FilamentRef
    destination: FilamentRef
    extruder: int
    volume_mm3: float


@dataclass(frozen=True)
class JobPurge:
    default_transition_mm3: float
    load_unload_mm3: float
    multipliers: list[float]
    fast_multipliers: list[float]
    transitions: list[PurgeTransition] = field(default_factory=list)

    def native(self, filaments: list[FilamentRef]) -> Purge:
        tables = [
            [
                [0 if source == destination else self.default_transition_mm3 for destination in filaments]
                for source in filaments
            ]
            for _ in self.multipliers
        ]
        seen = set()
        for transition in self.transitions:
            key = (transition.source, transition.destination, transition.extruder)
            if key in seen or not isinstance(transition.extruder, int) or not 1 <= transition.extruder <= len(tables):
                raise ValueError(f"Duplicate or invalid purge transition: {transition}")
            seen.add(key)
            source = filaments.index(transition.source)
            destination = filaments.index(transition.destination)
            tables[transition.extruder - 1][source][destination] = transition.volume_mm3
        return Purge(tables, [self.load_unload_mm3] * (2 * len(filaments)), self.multipliers, self.fast_multipliers)


@dataclass(frozen=True)
class JobOverrides:
    process: PrintSettings = field(default_factory=dict)
    printer: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PrintJob:
    filename: str
    machine_profile: str
    process_profile: str
    filaments: list[Filament]
    plates: list[PrintPlate]
    purge: JobPurge
    overrides: JobOverrides = field(default_factory=JobOverrides)
    project: dict[str, object] = field(default_factory=dict)

    @property
    def references(self) -> list[FilamentRef]:
        return [f.reference for f in self.filaments]

    @property
    def native_overrides(self) -> PresetOverrides:
        return PresetOverrides(
            native_settings(self.overrides.process, self.references),
            [f.overrides for f in self.filaments],
            self.overrides.printer,
        )

    def filament_for_part(self, path: str) -> Filament:
        references = {obj.filament for plate in self.plates for obj in plate.objects if obj.path == path}
        if len(references) != 1:
            raise ValueError(f"Expected one material for {path}: {references}")
        return next(f for f in self.filaments if f.reference == next(iter(references)))

    def patch(self, layout: PrintLayout) -> ProjectPatch:
        x, y = tower_coordinates(layout.plates)
        return ProjectPatch(
            layout.filament_colors,
            layout.filament_map,
            self.purge.native(self.references),
            self.native_overrides,
            "Manual",
            self.project | {"wipe_tower_x": x, "wipe_tower_y": y},
        )


@dataclass(frozen=True)
class JobPart:
    path: str
    filament: FilamentRef


@dataclass(frozen=True)
class JobObject:
    parts: list[JobPart]
    placements_xy: list[list[float]]
    print_params: PrintSettings = field(default_factory=dict)


def relative_path(value: str) -> str:
    if not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"Expected a relative path without parent traversal: {value}")
    return value


def read_job(path: Path) -> PrintJob:
    """Load type/color material identities; numeric filament IDs belong to Studio.

    Profile paths are relative to Studio's profile root; mesh paths are relative
    to the supplied mesh root. Coordinates are mesh-origin translations.
    """
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized print job")
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    filaments = [Filament(**choice) for choice in value.pop("filaments")]
    references = [f.reference for f in filaments]
    if (
        not filaments
        or len(set(references)) != len(filaments)
        or any(not isinstance(f.extruder, int) or f.extruder < 1 for f in filaments)
    ):
        raise ValueError("Print jobs require unique type/color filaments and positive physical extruder numbers")
    for filament in filaments:
        relative_path(filament.profile)

    def params(settings: dict[str, object]) -> PrintSettings:
        parsed = read_settings(settings)
        for ref in parsed.values():
            if isinstance(ref, FilamentRef) and ref not in references:
                raise ValueError(f"Unknown support material: {ref}")
        return parsed

    plates = []
    for row in value.pop("plates"):
        objects = []
        assembly = 0
        for raw in row.pop("objects"):
            group = JobObject(
                **{
                    **raw,
                    "parts": [
                        JobPart(**{**part, "filament": FilamentRef(**part["filament"])}) for part in raw["parts"]
                    ],
                }
            )
            if not group.parts or not group.placements_xy:
                raise ValueError("Print objects require parts and placements")
            for part in group.parts:
                if part.filament not in references:
                    raise ValueError(f"Unknown part material: {part.filament}")
            for xy in group.placements_xy:
                if len(xy) != 2 or any(not math.isfinite(v) for v in xy):
                    raise ValueError(f"Invalid XY placement: {xy}")
                if len(group.parts) > 1:
                    assembly += 1
                objects.extend(
                    PrintObject(
                        relative_path(part.path),
                        part.filament,
                        *xy,
                        assembly if len(group.parts) > 1 else 0,
                        params(group.print_params),
                    )
                    for part in group.parts
                )
        ranges = [LayerRange(**band) for band in row.pop("layer_ranges", [])]
        previous = 0
        for band in ranges:
            if (
                not all(math.isfinite(v) for v in asdict(band).values())
                or band.min_z_mm != previous
                or band.max_z_mm <= band.min_z_mm
                or band.layer_height_mm <= 0
            ):
                raise ValueError("Layer ranges must be positive, contiguous and start at zero")
            previous = band.max_z_mm
        plate = PrintPlate(
            **{**row, "objects": objects, "layer_ranges": ranges, "print_params": params(row.get("print_params", {}))}
        )
        if not plate.objects:
            raise ValueError("Empty print plate")
        if plate.prime_tower_xy is not None and (
            len(plate.prime_tower_xy) != 2 or any(not math.isfinite(v) for v in plate.prime_tower_xy)
        ):
            raise ValueError("Invalid prime tower coordinates")
        plates.append(plate)
    raw_overrides = value.pop("overrides", {})
    overrides = JobOverrides(**{**raw_overrides, "process": params(raw_overrides.get("process", {}))})
    raw_purge = value.pop("purge")
    purge = JobPurge(
        **{
            **raw_purge,
            "transitions": [
                PurgeTransition(
                    **{**t, "source": FilamentRef(**t["source"]), "destination": FilamentRef(**t["destination"])}
                )
                for t in raw_purge.get("transitions", [])
            ],
        }
    )
    # Check dimensions and transitions before loading actual hardware profiles.
    configure_flush_volumes(
        {"nozzle_diameter": [None] * len(purge.multipliers), "filament_colour": references},
        purge.native(references),
    )
    job = PrintJob(**value, filaments=filaments, plates=plates, overrides=overrides, purge=purge)
    if not plates or Path(job.filename).name != job.filename or Path(job.filename).suffix != ".3mf":
        raise ValueError("Print jobs require plates and a .3mf output filename")
    relative_path(job.machine_profile)
    relative_path(job.process_profile)
    return job


def resolve_job(job: PrintJob, stock: list[Stock]) -> tuple[PrintLayout, list[Stock]]:
    """Select available owned stock; colors and consumption use the same spools."""
    stocks = []
    for filament in job.filaments:
        matches = [
            s for s in stock if (s.brand, s.material, s.color) == (filament.brand, filament.type, filament.color)
        ]
        if not matches:
            raise ValueError(f"Required filament missing: {filament.brand} / {filament.reference}")
        stocks.append(max(matches, key=lambda s: s.remaining_g))
    return PrintLayout(
        [
            ResolvedFilament(f.type, f.color, s.color_hex, f.extruder)
            for f, s in zip(job.filaments, stocks, strict=True)
        ],
        job.plates,
    ), stocks


def read_layout(path: Path) -> PrintLayout:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized print layout")
    value = json.loads(path.read_text())
    return PrintLayout(
        [ResolvedFilament(**row) for row in value["filaments"]],
        [
            PrintPlate(
                **{
                    **row,
                    "print_params": read_settings(row["print_params"]),
                    "objects": [
                        PrintObject(
                            **{
                                **obj,
                                "filament": FilamentRef(**obj["filament"]),
                                "print_params": read_settings(obj["print_params"]),
                            }
                        )
                        for obj in row["objects"]
                    ],
                    "layer_ranges": [LayerRange(**band) for band in row["layer_ranges"]],
                }
            )
            for row in value["plates"]
        ],
    )


def model_layers(path: Path) -> list[tuple[float, float]]:
    """Read model layer-change metadata, excluding thicker tower/bridge comments."""
    result = []
    pending_z = None
    with path.open() as stream:
        for line in stream:
            if line.startswith("; Z_HEIGHT: "):
                pending_z = float(line.split(":", 1)[1])
            elif pending_z is not None and line.startswith("; LAYER_HEIGHT: "):
                result.append((pending_z, float(line.split(":", 1)[1])))
                pending_z = None
    return result


def printable_bounds(machine: dict[str, object], extruders: set[int]) -> tuple[float, ...]:
    """Common rectangle for the used extruders. Reject nonrectangular areas."""
    areas = machine.get("extruder_printable_area")
    polygons = (
        [areas[e - 1].split(",") for e in sorted(extruders)] if areas and extruders else [machine["printable_area"]]
    )
    bounds = []
    for polygon in polygons:
        points = {tuple(map(float, point.split("x"))) for point in polygon}
        xs, ys = {p[0] for p in points}, {p[1] for p in points}
        if len(xs) != 2 or len(ys) != 2 or len(points) != 4:
            raise ValueError("Print-job bounds checking requires rectangular printable areas")
        bounds.append((min(xs), max(xs), min(ys), max(ys)))
    return max(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), min(b[3] for b in bounds)


def tower_coordinates(plates: list[PrintPlate]) -> tuple[list[str], list[str]]:
    return tuple(
        [str(plate.prime_tower_xy[axis]) if plate.prime_tower_xy else "0" for plate in plates] for axis in (0, 1)
    )


def plate_labels_config(path: Path, plates: list[PrintPlate]) -> bytes:
    """Write authored labels; Studio may omit them for single-part plates."""
    with zipfile.ZipFile(path) as archive:
        config = ET.fromstring(archive_metadata(archive, "Metadata/model_settings.config"))
    for planned, saved in zip(plates, config.findall("plate"), strict=True):
        label = saved.find("metadata[@key='plater_name']")
        if label is None:
            label = ET.SubElement(saved, "metadata", key="plater_name")
        label.set("value", planned.name)
    return ET.tostring(config, encoding="utf-8", xml_declaration=True)


def layer_ranges_config(path: Path, plates: list[PrintPlate]) -> ET.Element:
    """Map plate ranges to Studio's 1-based model indices, not 3MF resource IDs.

    Native height-range format: BambuStudio bbs_3mf.cpp, _add_layer_config_ranges_file_to_archive.
    """
    with zipfile.ZipFile(path) as archive:
        model = ET.fromstring(archive_metadata(archive, "3D/3dmodel.model"))
        config = ET.fromstring(archive_metadata(archive, "Metadata/model_settings.config"))
    indices = {
        item.attrib["objectid"]: str(index)
        for index, item in enumerate(model.findall(f"{{{CORE}}}build/{{{CORE}}}item"), 1)
    }
    result = ET.Element("objects")
    for plate, saved in zip(plates, config.findall("plate"), strict=True):
        if not plate.layer_ranges:
            continue
        for instance in saved.findall("model_instance"):
            obj = ET.SubElement(result, "object", id=indices[metadata(instance)["object_id"]])
            for segment in plate.layer_ranges:
                span = ET.SubElement(obj, "range", min_z=str(segment.min_z_mm), max_z=str(segment.max_z_mm))
                ET.SubElement(span, "option", opt_key="layer_height").text = str(segment.layer_height_mm)
    return result


def same_setting(actual: object, expected: object) -> bool:
    """Studio normalizes native numeric strings when saving, e.g. 0.20 -> 0.2."""
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(same_setting(actual[k], v) for k, v in expected.items())
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(same_setting(a, b) for a, b in zip(actual, expected, strict=True))
    if actual == expected:
        return True
    if isinstance(actual, str) and isinstance(expected, str):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    return False


def validate_project(
    path: Path,
    plates: list[PrintPlate],
    meshes: dict[str, MeshInfo],
    colors: list[str],
    filament_map: list[int],
    machine: dict[str, object],
    process: dict[str, object],
    filaments: list[dict[str, object]],
    job: PrintJob,
) -> None:
    """Reopen parts, assignments, plate membership and actual transformed mesh bounds."""
    expected_ranges = layer_ranges_config(path, plates)
    tower_x, tower_y = tower_coordinates(plates)
    with zipfile.ZipFile(path) as archive:
        if len(expected_ranges):
            saved_ranges = ET.fromstring(archive_metadata(archive, LAYER_RANGES_FILE))

            # Studio changes whitespace and float spelling when saving.
            def ranges(tree: ET.Element) -> dict[str, list[tuple[float, float, float]]]:
                return {
                    obj.attrib["id"]: [
                        (float(span.attrib["min_z"]), float(span.attrib["max_z"]), float(span.find("option").text))
                        for span in obj.findall("range")
                    ]
                    for obj in tree.findall("object")
                }

            if ranges(saved_ranges) != ranges(expected_ranges):
                raise ValueError("Project lost layer ranges")
        settings = json.loads(archive_metadata(archive, "Metadata/project_settings.config"))
        validate_flush_volumes(settings)
        expected = {
            "filament_colour": colors,
            "filament_map": [str(extruder) for extruder in filament_map],
            "printer_settings_id": machine["name"],
            "print_settings_id": process["name"],
            **{
                key: process[key]
                for key in (
                    "layer_height",
                    "initial_layer_print_height",
                    "wall_loops",
                    "sparse_infill_density",
                    "enable_support",
                    "support_type",
                    "support_filament",
                    "support_interface_filament",
                    "support_threshold_angle",
                    "support_on_build_plate_only",
                    "enable_prime_tower",
                )
            },
            **job.native_overrides.process,
            **job.overrides.printer,
            **{k: v for k, v in job.project.items() if k not in ("wipe_tower_x", "wipe_tower_y")},
        }
        changed = [k for k, v in expected.items() if not same_setting(settings.get(k), v)]
        if changed:
            raise ValueError(f"Unexpected printer configuration: {changed}")
        differences = settings["different_settings_to_system"]
        groups = [
            job.native_overrides.process,
            *(job.native_overrides.filaments or [{} for _ in colors]),
            job.overrides.printer,
        ]
        if len(differences) != len(groups) or any(
            not set(group).issubset(saved.split(";")) for group, saved in zip(groups, differences, strict=True)
        ):
            raise ValueError("GUI preset loading would discard intentional overrides")
        if any(settings.get(k) != value for k, value in machine.items() if k.endswith("gcode")):
            raise ValueError("Project lost inherited printer commands")
        filament_overrides = job.native_overrides.filaments or [{} for _ in filaments]
        expected_filaments = {
            key: [group.get(key, profile[key][0]) for profile, group in zip(filaments, filament_overrides, strict=True)]
            for key in ("filament_type", "filament_density", "filament_diameter")
        }
        for slot, group in enumerate(filament_overrides):
            if any(
                not same_setting(settings[key][entry], value)
                for key, value in group.items()
                for entry in filament_setting_indices(settings, key, slot)
            ):
                raise ValueError(f"Project lost filament overrides for {job.references[slot]}")
        if (
            settings["filament_settings_id"] != [profile["name"] for profile in filaments]
            or settings["filament_type"] != expected_filaments["filament_type"]
            or any(
                [float(value) for value in settings[key]] != [float(value) for value in expected_filaments[key]]
                for key in ("filament_density", "filament_diameter")
            )
        ):
            raise ValueError("Project lost selected filament profiles or material properties")
        config = ET.fromstring(archive_metadata(archive, "Metadata/model_settings.config"))
        saved_plates = config.findall("plate")
        if len(saved_plates) != len(plates):
            raise ValueError("Incorrect plate count")
        objects = {o.attrib["id"]: o for o in config.findall("object")}
        process_keys = (
            set(settings)
            | {key for p in plates for key in p.print_params}
            | {key for p in plates for o in p.objects for key in o.print_params}
        ) - {"extruder", "name"}
        root = ET.fromstring(archive_metadata(archive, "3D/3dmodel.model"))
        resources = {o.attrib["id"]: o for o in root.findall(f"{{{CORE}}}resources/{{{CORE}}}object")}
        items = {o.attrib["objectid"]: o for o in root.findall(f"{{{CORE}}}build/{{{CORE}}}item")}
        for number, (plate, saved) in enumerate(zip(plates, saved_plates, strict=True)):
            saved_metadata = metadata(saved)
            # Studio may reset the global mode when loading the printer; the
            # explicit plate mode and assignments must still preserve routing.
            if (
                saved_metadata.get("filament_map_mode") != "Manual"
                or [int(value) for value in saved_metadata.get("filament_maps", "").split()] != filament_map
            ):
                raise ValueError(f"Plate {number + 1} overrides intended nozzle routing")
            if plate.prime_tower_xy and (
                float(settings["wipe_tower_x"][number]) != float(tower_x[number])
                or float(settings["wipe_tower_y"][number]) != float(tower_y[number])
            ):
                raise ValueError(f"Plate {number + 1} lost its active tower placement")
            columns = math.ceil(math.sqrt(len(plates)))
            bed = printable_bounds(machine, set())
            offset = (number % columns * (bed[1] - bed[0]) * 1.2, -(number // columns) * (bed[3] - bed[2]) * 1.2, 0)
            object_ids = [metadata(instance)["object_id"] for instance in saved.findall("model_instance")]
            expected_count = sum(o.assembly == 0 for o in plate.objects) + len(
                {o.assembly for o in plate.objects if o.assembly}
            )
            # Studio resets single-part labels when saving a slice. Labels are
            # authored by plate_labels_config; here check printable membership.
            if len(object_ids) != expected_count:
                raise ValueError(f"Missing plate objects: plate {number + 1}, {object_ids}")
            actual_parts = []
            remaining = list(plate.objects)
            for object_id in object_ids:
                obj = objects[object_id]
                obj_meta = metadata(obj)
                # Reject redundant process overrides as well as missing exceptions.
                overrides = {key: value for key, value in obj_meta.items() if key in process_keys}
                expected_overrides = []
                translation = [float(v) for v in items[object_id].attrib["transform"].split()]
                if translation[:9] != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
                    raise ValueError("Unexpected build rotation")
                components = resources[object_id].findall(f"{{{CORE}}}components/{{{CORE}}}component")
                for part, component in zip(obj.findall("part"), components, strict=True):
                    info = metadata(part)
                    if process_keys.intersection(info):
                        raise ValueError("Color part has redundant print overrides")
                    source_name = info["source_file"]
                    observation = inspect_component(archive, component, items[object_id])
                    if observation.transform[:9] != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
                        raise ValueError("Slicer changed part orientation")
                    actual_bounds = [v - offset[i // 2] for i, v in enumerate(observation.bounds_mm)]
                    matches = [
                        o
                        for o in remaining
                        if Path(o.path).name == source_name
                        and all(
                            math.isclose(actual, value + (o.x, o.y, 0)[i // 2], abs_tol=0.002)
                            for i, (actual, value) in enumerate(
                                zip(actual_bounds, meshes[o.path].bounds_mm, strict=True)
                            )
                        )
                    ]
                    if len(matches) != 1 or observation.triangles != meshes[matches[0].path].triangles:
                        raise ValueError(f"Slicer changed mesh or placement: {source_name}")
                    actual_parts.append((matches[0].path, int(info.get("extruder", obj_meta.get("extruder", "0")))))
                    expected_overrides.append(
                        native_settings(plate.print_params | matches[0].print_params, job.references)
                    )
                    remaining.remove(matches[0])
                if not expected_overrides or any(
                    not same_setting(overrides, expected) for expected in expected_overrides
                ):
                    raise ValueError(f"Unexpected object print overrides: {overrides}")
            if sorted(actual_parts) != sorted((o.path, job.references.index(o.filament) + 1) for o in plate.objects):
                raise ValueError("Incorrect per-part filament assignments")


def validate_slice(
    directory: Path,
    studio_version_value: str,
    stocks: list[Stock],
    plan: PrintLayout,
    materials: list[Material],
    machine: dict[str, object],
    process: dict[str, object],
) -> SliceReport:
    report, analyses = read_slice(
        directory,
        dict(enumerate(materials, 1)),
        studio_version_value,
    )
    if len(report["sliced_plates"]) != len(plan.plates):
        raise ValueError("Failed or incomplete slice")
    totals = [0.0] * len(stocks)
    plates = []
    for number, (plate, analysis, planned) in enumerate(
        zip(report["sliced_plates"], analyses, plan.plates, strict=True), 1
    ):
        masses = {int(f["id"]): float(f["total_used_g"]) for f in plate["filaments"]}
        with (directory / f"plate_{number}.gcode").open() as stream:
            routing = next(line for line in stream if line.startswith("; filament_map = "))
            nozzles = next((line for line in stream if line.startswith("; filament_nozzle_map = ")), None)
        if [int(value) for value in routing.split("=", 1)[1].split(",")] != plan.filament_map:
            raise ValueError(f"Slice changed intended nozzle routing on plate {number}")
        nozzle_map = [int(value) for value in nozzles.split("=", 1)[1].split(",")] if nozzles else None
        effective = process | native_settings(planned.print_params, plan.references)

        model_slots_expected = {plan.references.index(obj.filament) + 1 for obj in planned.objects}
        interface_slots = {
            int((effective | native_settings(obj.print_params, plan.references))["support_interface_filament"])
            for obj in planned.objects
        }
        body_slots_expected = {
            int((effective | native_settings(obj.print_params, plan.references))["support_filament"])
            for obj in planned.objects
        }
        used_slots = {slot for slot, mass in masses.items() if mass > 0}
        allowed = model_slots_expected | interface_slots | body_slots_expected
        if 0 in allowed:
            allowed.update(model_slots_expected)
        if not model_slots_expected.issubset(used_slots) or not used_slots.issubset(allowed):
            raise ValueError(f"Incorrect material usage on plate {number}: {masses}")
        actual_interfaces = {
            slot for slot, length in analysis.feature_lengths_mm.get("Support interface", {}).items() if length > 0
        }
        if planned.separate_interface_hotends and actual_interfaces:
            if not nozzle_map:
                raise ValueError("Separate interface hotends require a slicer nozzle map")
            for support in actual_interfaces:
                if any(
                    (plan.filament_map[support - 1], nozzle_map[support - 1])
                    == (plan.filament_map[model - 1], nozzle_map[model - 1])
                    for model in model_slots_expected
                ):
                    raise ValueError(f"Support interface shares a model hotend on plate {number}")
        for slot, mass in masses.items():
            totals[slot - 1] += mass
        features = set(analysis.feature_bounds_xy_mm)
        layer_heights = analysis.layer_heights_mm
        feature_bounds = analysis.feature_bounds_xy_mm
        if ("Prime tower" in features) != (len(used_slots) > 1 and effective["enable_prime_tower"] == "1") or float(
            effective["layer_height"]
        ) not in layer_heights:
            raise ValueError(f"Unexpected toolpaths or layer heights on plate {number}: {features}, {layer_heights}")
        area = printable_bounds(machine, {plan.filament_map[slot - 1] for slot in used_slots})
        for feature_name, limits in feature_bounds.items():
            if limits[0] < area[0] or limits[1] > area[1] or limits[2] < area[2] or limits[3] > area[3]:
                raise ValueError(f"{feature_name} extrusion outside common nozzle area: {limits}")
        analysis.footprints.validate(planned.minimum_clearance_mm)
        analysis.footprints.render(directory / f"plate_{number}_footprints.webp")
        summary = PlateReport(
            name=planned.name,
            filaments=[
                FilamentUsage(**asdict(plan.references[int(f["id"]) - 1]), total_used_g=f["total_used_g"])
                for f in plate["filaments"]
            ],
            support_mass_g=analysis.feature_mass_g.get("Support", 0),
            support_interface_mass_g=analysis.feature_mass_g.get("Support interface", 0),
            supported_layers=len(analysis.supported_layers),
            prime_tower_mass_g=analysis.feature_mass_g.get("Prime tower", 0),
            layer_heights_mm=sorted(layer_heights),
            extrusion_bounds_xy_mm=feature_bounds,
            minimum_clearance_checked_mm=planned.minimum_clearance_mm,
        )
        plates.append(summary)
        if planned.layer_ranges:
            layers = model_layers(directory / f"plate_{number}.gcode")
            for z, height in layers:
                segment = next((s for s in planned.layer_ranges if s.min_z_mm < z <= s.max_z_mm + 0.001), None)
                if segment is None:
                    continue  # Outside authored bands, the process/object layer height applies.
                if not math.isclose(height, segment.layer_height_mm, abs_tol=0.001):
                    raise ValueError(f"Incorrect layer height at Z={z}: {height}")
        model_slots = {
            slot
            for feature, lengths in analysis.feature_lengths_mm.items()
            if feature not in ("Support", "Support interface", "Prime tower", "Skirt", "Brim")
            for slot, length in lengths.items()
            if length > 0
        }
        for feature, expected in (("Support interface", interface_slots), ("Support", body_slots_expected)):
            actual = {slot for slot, length in analysis.feature_lengths_mm.get(feature, {}).items() if length > 0}
            allowed_support = (expected - {0}) | (model_slots_expected if 0 in expected else set())
            if not actual.issubset(allowed_support):
                raise ValueError(f"Wrong {feature} material on plate {number}: {actual}")
        if model_slots != model_slots_expected:
            raise ValueError(f"Missing intended model colors on plate {number}: {model_slots}")
    consumption = {}
    for total, spool in zip(totals, stocks, strict=True):
        consumption[spool.id] = consumption.get(spool.id, 0) + total
    if any(consumption[spool.id] > spool.remaining_g for spool in stocks):
        raise ValueError("Insufficient filament stock")
    return SliceReport(
        plates,
        [FilamentUsage(**asdict(ref), total_used_g=total) for ref, total in zip(plan.references, totals, strict=True)],
    )


def build_job(
    job: PrintJob,
    mesh_root: Path,
    directory: Path,
    validation: Path,
    studio: Path,
    profiles: Path,
    environment: dict[str, str],
    *,
    stock: list[Stock],
) -> SliceReport:
    plan, stocks = resolve_job(job, stock)
    validation.mkdir(parents=True)
    directory.mkdir(parents=True, exist_ok=True)
    log = validation / "studio.log"
    plates = plan.plates
    colors = plan.filament_colors
    machine = resolve_profile(profiles / job.machine_profile)
    process = resolve_profile(profiles / job.process_profile)
    filaments = [resolve_profile(profiles / f.profile) for f in job.filaments]
    materials = [
        Material(
            float(group.get("filament_density", profile["filament_density"][0])),
            float(group.get("filament_diameter", profile["filament_diameter"][0])),
        )
        for profile, group in zip(filaments, job.native_overrides.filaments or [{} for _ in filaments], strict=True)
    ]
    studio_version_value = studio_version(studio)
    print_meshes = {
        obj.path: validate_stl(mesh_root / obj.path, require_one_shell=False)
        for plate in plates
        for obj in plate.objects
    }
    machine_path, process_path = [validation / f"{n}.json" for n in ("machine", "process")]
    filament_paths = [validation / f"filament_{slot}.json" for slot in range(1, len(stocks) + 1)]
    for path, settings in zip(
        [machine_path, process_path, *filament_paths], [machine, process, *filaments], strict=True
    ):
        path.write_text(json.dumps(settings))
    assemble = validation / "assemble.json"
    assemble.write_text(
        json.dumps(
            {
                "plates": [
                    {
                        "plate_name": p.name,
                        "need_arrange": False,
                        "objects": [o.bambu(mesh_root, p.print_params, job.references) for o in p.objects],
                        # Touching color parts inherit overrides from their assembled object.
                        "assembled_params": [
                            {
                                "assemble_index": index,
                                "print_params": native_settings(
                                    p.print_params | next(o.print_params for o in p.objects if o.assembly == index),
                                    job.references,
                                ),
                            }
                            for index in sorted({o.assembly for o in p.objects if o.assembly})
                        ],
                    }
                    for p in plates
                ]
            }
        )
    )
    raw = validation / "raw.3mf"
    run(
        studio_command(
            studio,
            "--load-assemble-list",
            str(assemble),
            "--export-3mf",
            str(raw),
            "--load-settings",
            f"{machine_path};{process_path}",
            "--load-filaments",
            ";".join(str(path) for path in filament_paths),
        ),
        log,
        validation,
        environment,
    )
    replacements = {"Metadata/model_settings.config": plate_labels_config(raw, plates)}
    if any(plate.layer_ranges for plate in plates):
        replacements[LAYER_RANGES_FILE] = ET.tostring(
            layer_ranges_config(raw, plates), encoding="utf-8", xml_declaration=True
        )
    package_project(raw, directory / job.filename, job.patch(plan), replacements)
    validate_project(
        directory / job.filename, plates, print_meshes, colors, plan.filament_map, machine, process, filaments, job
    )
    sliced = validation / "slice"
    sliced.mkdir()
    print(f"Reopening and slicing all {len(plates)} print plates", flush=True)
    run(
        studio_command(
            studio,
            "--slice",
            "0",
            "--export-3mf",
            SLICED_PROJECT,
            "--outputdir",
            str(sliced),
            str(directory / job.filename),
        ),
        log,
        directory,
        environment,
    )
    validate_project(
        sliced / SLICED_PROJECT,
        plates,
        print_meshes,
        colors,
        plan.filament_map,
        machine,
        process,
        filaments,
        job,
    )
    result = validate_slice(
        sliced,
        studio_version_value,
        stocks,
        plan,
        materials,
        machine | job.overrides.printer,
        process | job.native_overrides.process,
    )
    plan.write(directory / "print_layout.json")
    (directory / "slice_report.json").write_text(dumps_report(asdict(result)))
    return result


def describe_usage(report: SliceReport) -> str:
    return "Estimated filament use: " + "; ".join(
        f"{entry.type} / {entry.color}: {entry.total_used_g:.1f} g" for entry in report.filament_usage
    )
