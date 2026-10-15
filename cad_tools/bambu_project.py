"""Bambu 3MF operations, with job choices supplied explicitly by the caller."""

import json
import math
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from cad_tools.bambu_profiles import (
    Purge,
    configure_flush_volumes,
    validate_flush_volumes,
)
from cad_tools.metadata import archive_metadata
from cad_tools.workspace import workspace_root

SETTINGS = "Metadata/project_settings.config"

RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


@dataclass(frozen=True)
class PresetOverrides:
    """Intentional GUI-preserved differences in process, N filaments, printer order."""

    process: dict[str, object] = field(default_factory=dict)
    filaments: list[dict[str, object]] = field(default_factory=list)
    printer: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectPatch:
    colors: list[str]
    filament_map: list[int]
    purge: Purge
    overrides: PresetOverrides
    filament_map_mode: str
    # Package-only/UI settings do not belong to any preset group.
    project: dict[str, object] = field(default_factory=dict)
    omit_images: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ProjectPatch":
        return cls(
            **{
                **value,
                "purge": Purge.from_dict(value["purge"]),
                "overrides": PresetOverrides(**value["overrides"]),
            }
        )


def settings_from_archive(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive_metadata(archive, SETTINGS))


def filament_setting_indices(settings: dict[str, object], key: str, slot: int) -> list[int]:
    """Resolve scalar overrides to filament entries, including hotend variants."""
    values = settings.get(key)
    count = len(settings["filament_settings_id"])
    if not isinstance(values, list):
        raise ValueError(f"Expected filament setting array: {key}")
    # Studio's filament_options_with_variant flattens these by filament_self_index.
    # A scalar limit applies to every hotend variant of the selected filament.
    # See PrintConfig.cpp and PresetBundle.cpp in Studio v02.08.02.61.
    if len(values) == count:
        return [slot]
    if key in {"filament_flow_ratio", "filament_max_volumetric_speed"}:
        owners = settings.get("filament_self_index")
        if not isinstance(owners, list) or len(owners) != len(values):
            raise ValueError(f"Missing hotend-variant ownership for {key}")
        owners = [int(owner) for owner in owners]
        if set(owners) != set(range(1, count + 1)):
            raise ValueError(f"Invalid hotend-variant ownership for {key}")
        return [index for index, owner in enumerate(owners) if owner == slot + 1]
    raise ValueError(f"Expected per-filament setting array: {key}")


def metadata(element: ET.Element) -> dict[str, str]:
    return {item.attrib["key"]: item.attrib["value"] for item in element.findall("metadata") if "key" in item.attrib}


@dataclass(frozen=True)
class ComponentMesh:
    member: str
    object_id: str
    transform: list[float]
    build_transform: list[float]
    triangles: int
    bounds_mm: list[float]


def inspect_component(archive: zipfile.ZipFile, component: ET.Element, build_item: ET.Element) -> ComponentMesh:
    """Observe a production-extension leaf mesh in build coordinates.

    Studio emits mesh components in external model members. Support their full
    affine 3MF transforms; recipes decide which orientations/placements are valid.
    This operator deliberately handles leaf meshes, not recursive CAD assemblies.
    """

    def transform(element: ET.Element) -> list[float]:
        values = [float(v) for v in element.attrib.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0").split()]
        if len(values) != 12 or any(not math.isfinite(v) for v in values):
            raise ValueError("Invalid 3MF affine transform")
        return values

    def point(values: tuple[float, ...], matrix: list[float]) -> tuple[float, ...]:
        return tuple(sum(values[j] * matrix[j * 3 + i] for j in range(3)) + matrix[9 + i] for i in range(3))

    local, placement = transform(component), transform(build_item)
    member = component.attrib[f"{{{PRODUCTION}}}path"].lstrip("/")
    object_id = component.attrib["objectid"]
    minimum, maximum = [math.inf] * 3, [-math.inf] * 3
    active = found = False
    triangles = vertices = 0
    stack: list[ET.Element] = []
    with archive.open(member) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                stack.append(element)
                if element.tag == f"{{{CORE}}}object":
                    active = element.attrib["id"] == object_id
                    found = found or active
            elif event == "end":
                if active and element.tag == f"{{{CORE}}}vertex":
                    values = point(
                        point(tuple(float(element.attrib[k]) for k in "xyz"), local),
                        placement,
                    )
                    if any(not math.isfinite(v) for v in values):
                        raise ValueError("Nonfinite 3MF vertex")
                    vertices += 1
                    for axis, value in enumerate(values):
                        minimum[axis], maximum[axis] = (
                            min(minimum[axis], value),
                            max(maximum[axis], value),
                        )
                elif active and element.tag == f"{{{CORE}}}triangle":
                    triangles += 1
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
                stack.pop()
    if not found or not vertices or not triangles:
        raise ValueError(f"Missing leaf mesh: {member} object {object_id}")
    bounds = [v for pair in zip(minimum, maximum, strict=True) for v in pair]
    return ComponentMesh(member, object_id, local, placement, triangles, bounds)


def inspect_project(path: Path, *, full_settings: bool = False) -> dict[str, object]:
    """Read bounded package metadata; recipes own layout/geometry expectations."""
    with zipfile.ZipFile(path) as archive:
        settings = json.loads(archive_metadata(archive, SETTINGS))
        validate_flush_volumes(settings)
        model = ET.fromstring(archive_metadata(archive, "Metadata/model_settings.config"))
        root = ET.fromstring(archive_metadata(archive, "3D/3dmodel.model"))
        resources = {obj.attrib["id"]: obj for obj in root.findall(f"{{{CORE}}}resources/{{{CORE}}}object")}
        items = root.findall(f"{{{CORE}}}build/{{{CORE}}}item")

        def summary(component: ET.Element, item: ET.Element) -> dict[str, object]:
            mesh = inspect_component(archive, component, item)
            return {
                "member": mesh.member,
                "object_id": mesh.object_id,
                "transform": " ".join(str(v) for v in mesh.transform),
                "triangles": mesh.triangles,
                "bounds_mm": mesh.bounds_mm,
            }

        # One resource object can occur more than once in the build. Keep each
        # item with its own placement and mesh observations, never keyed by ID.
        build_items = [
            {
                "build_index": index,
                **dict(item.attrib),
                "component_meshes": [
                    summary(component, item)
                    for component in resources[item.attrib["objectid"]].findall(
                        f"{{{CORE}}}components/{{{CORE}}}component"
                    )
                ],
            }
            for index, item in enumerate(items)
        ]
        return {
            "settings": settings
            if full_settings
            else {
                key: settings[key]
                for key in (
                    "printer_settings_id",
                    "print_settings_id",
                    "filament_settings_id",
                    "filament_colour",
                    "filament_map",
                    "filament_map_mode",
                    "nozzle_diameter",
                    "curr_bed_type",
                    "layer_height",
                    "wall_loops",
                    "sparse_infill_density",
                    "enable_support",
                    "support_type",
                    "support_filament",
                    "support_interface_filament",
                    "enable_prime_tower",
                    "wipe_tower_x",
                    "wipe_tower_y",
                    "different_settings_to_system",
                )
                if key in settings
            },
            "purge_dimensions": {
                key: len(settings[key])
                for key in (
                    "flush_volumes_matrix",
                    "flush_volumes_vector",
                    "flush_multiplier",
                    "flush_multiplier_fast",
                )
            },
            "plates": [
                {
                    "metadata": metadata(plate),
                    "instances": [metadata(i) for i in plate.findall("model_instance")],
                }
                for plate in model.findall("plate")
            ],
            "objects": [
                {
                    "id": obj.attrib["id"],
                    "metadata": metadata(obj),
                    "parts": [{"id": part.attrib["id"], "metadata": metadata(part)} for part in obj.findall("part")],
                }
                for obj in model.findall("object")
            ],
            "build_items": build_items,
        }


def package_project(
    source_path: Path,
    target: Path,
    patch: ProjectPatch,
    replacements: dict[str, bytes] | None = None,
) -> None:
    """Atomically patch settings/relationships and stream unchanged archive members."""
    count = len(patch.colors)
    if (
        not count
        or len(patch.filament_map) != count
        or any(
            not isinstance(c, str)
            or len(c) != 7
            or c[0] != "#"
            or any(x not in "0123456789abcdefABCDEF" for x in c[1:])
            for c in patch.colors
        )
    ):
        raise ValueError("One #RRGGBB color and physical-extruder mapping per filament are required")
    if patch.overrides.filaments and len(patch.overrides.filaments) != count:
        raise ValueError("Filament preset overrides require one group per filament")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as source:
        settings = json.loads(archive_metadata(source, SETTINGS))
        for key, value in list(settings.items()):
            if key.startswith("filament_") and isinstance(value, list) and len(value) == 1:
                settings[key] = value * count
        settings.update(patch.project)
        overrides = patch.overrides
        settings.update(overrides.process)
        settings.update(overrides.printer)
        for index, group in enumerate(overrides.filaments):
            for key, value in group.items():
                for entry in filament_setting_indices(settings, key, index):
                    settings[key][entry] = value
        settings.update(
            filament_colour=patch.colors,
            filament_map_mode=patch.filament_map_mode,
            filament_map=[str(i) for i in patch.filament_map],
        )
        extruders = len(settings["nozzle_diameter"])
        if any(not isinstance(i, int) or not 1 <= i <= extruders for i in patch.filament_map):
            raise ValueError("Filament mapping must reference an available physical extruder")
        # PresetBundle.cpp orders groups as process, N filaments, printer. Keep
        # inherited difference flags and add each override to its owning group.
        differences = settings["different_settings_to_system"]
        if not isinstance(differences, list) or len(differences) != count + 2:
            raise ValueError("Incorrect preset difference group count")
        groups = [
            overrides.process,
            *(overrides.filaments or [{} for _ in range(count)]),
            overrides.printer,
        ]
        for i, group in enumerate(groups):
            differences[i] = ";".join(sorted(set(filter(None, differences[i].split(";"))) | set(group)))
        configure_flush_volumes(settings, patch.purge)
        edited = dict(replacements or {})
        edited[SETTINGS] = json.dumps(settings, separators=(",", ":")).encode()
        # CLI exports give each plate an automatic-routing override; replace it
        # with the project mode. Missing/Default metadata loads as auto in Studio.
        model_settings = "Metadata/model_settings.config"
        config = ET.fromstring(edited.get(model_settings, archive_metadata(source, model_settings)))
        for plate in config.findall("plate"):
            for item in list(plate.findall("metadata")):
                if item.attrib["key"] in ("filament_map_mode", "filament_maps", "filament_volume_maps"):
                    plate.remove(item)
            ET.SubElement(plate, "metadata", key="filament_map_mode", value=patch.filament_map_mode)
            ET.SubElement(plate, "metadata", key="filament_maps", value=" ".join(str(i) for i in patch.filament_map))
        edited[model_settings] = ET.tostring(config, encoding="utf-8", xml_declaration=True)
        if patch.omit_images:
            relationships = ET.fromstring(archive_metadata(source, "_rels/.rels"))
            for relationship in list(relationships):
                kind = relationship.attrib["Type"]
                if kind == f"{RELATIONSHIPS}/metadata/thumbnail" or kind.startswith(
                    "http://schemas.bambulab.com/package/2021/cover-thumbnail-"
                ):
                    relationships.remove(relationship)
            ET.register_namespace("", RELATIONSHIPS)
            edited["_rels/.rels"] = ET.tostring(relationships, encoding="utf-8", xml_declaration=True)
        staged = workspace_root() / "bambu-project" / f"{target.name}.pending"
        staged.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as destination:
                for entry in source.infolist():
                    if (
                        patch.omit_images
                        and entry.filename.startswith(
                            (
                                "Metadata/plate_",
                                "Metadata/bbl_thumbnail",
                                "Auxiliaries/.thumbnails/",
                            )
                        )
                        and entry.filename.endswith(".png")
                    ):
                        continue
                    if entry.filename in edited:
                        destination.writestr(entry, edited[entry.filename])
                    else:
                        with (
                            source.open(entry) as incoming,
                            destination.open(entry, "w") as outgoing,
                        ):
                            shutil.copyfileobj(incoming, outgoing)
                for name in sorted(edited.keys() - set(source.namelist())):
                    destination.writestr(name, edited[name])
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
