"""Resolve Bambu preset inheritance and construct physical-extruder purge tables."""

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Purge:
    """Explicit E×N×N directional tables, 2N load/unload values and E multipliers."""

    transitions_mm3: list[list[list[float]]]
    load_unload_mm3: list[float]
    multipliers: list[float]
    fast_multipliers: list[float]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Purge":
        return cls(**value)


def configure_flush_volumes(settings: dict[str, object], purge: Purge) -> None:
    """Serialize the verified Studio 2.8 physical-extruder purge schema.

    No color/material calibration is inferred. Each table is row-major source to
    destination. The GUI expands multipliers to physical E; all dimensions must
    agree before publication, including transitions on presently unused nozzles.
    """
    extruders = len(settings["nozzle_diameter"])
    filaments = len(settings["filament_colour"])
    if len(purge.transitions_mm3) != extruders or any(
        len(table) != filaments or any(len(row) != filaments for row in table) for table in purge.transitions_mm3
    ):
        raise ValueError("Purge transitions require E tables of N rows and N columns")
    settings.update(
        flush_volumes_matrix=[str(value) for table in purge.transitions_mm3 for row in table for value in row],
        flush_volumes_vector=[str(v) for v in purge.load_unload_mm3],
        flush_multiplier=[str(v) for v in purge.multipliers],
        flush_multiplier_fast=[str(v) for v in purge.fast_multipliers],
    )
    validate_flush_volumes(settings)


def validate_flush_volumes(settings: dict[str, object]) -> None:
    """Check GUI and G-code purge dimensions against physical extruders/colors."""
    extruders = len(settings["nozzle_diameter"])
    filaments = len(settings["filament_colour"])
    if extruders == 0 or filaments == 0:
        raise ValueError("Purge settings require physical extruders and filaments")
    dimensions = {
        "flush_multiplier": extruders,
        "flush_multiplier_fast": extruders,
        "flush_volumes_matrix": extruders * filaments * filaments,
        "flush_volumes_vector": 2 * filaments,
    }
    for name, count in dimensions.items():
        values = settings.get(name)
        if not isinstance(values, list) or len(values) != count:
            raise ValueError(f"{name} needs {count} entries for {extruders} extruders/{filaments} filaments")
        if any(not math.isfinite(float(v)) or float(v) < 0 for v in values):
            raise ValueError(f"Invalid purge values: {name}")
        if name.startswith("flush_multiplier") and any(float(v) <= 0 for v in values):
            raise ValueError(f"Purge multipliers must be positive: {name}")
    matrix = settings["flush_volumes_matrix"]
    if any(
        float(matrix[e * filaments * filaments + f * filaments + f]) != 0
        for e in range(extruders)
        for f in range(filaments)
    ):
        raise ValueError("Purge matrix diagonal must be zero for each extruder")


def resolve_profile(path: Path) -> dict[str, object]:
    """Materialize bundled parents/templates for Bambu's direct JSON loader."""
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"Oversized Bambu profile: {path}")
    profile_type = json.loads(path.read_text()).get("type")
    if profile_type not in ("filament", "machine", "process"):
        raise ValueError(f"Unsupported Bambu profile type: {path}")
    metadata = {
        "alias",
        "description",
        "filament_id",
        "from",
        "include",
        "inherits",
        "instantiation",
        "name",
        "renamed_from",
        "setting_id",
        "type",
        "version",
    }

    def reference(name: object) -> Path:
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"Invalid filament profile reference: {name!r}")
        return path.parent / f"{name}.json"

    def resolve(current: Path, chain: tuple[Path, ...], *, template: bool = False) -> dict[str, object]:
        if current in chain:
            raise ValueError(
                "Filament profile inheritance/include cycle: " + " -> ".join(item.stem for item in (*chain, current))
            )
        if current.stat().st_size > 1024 * 1024:
            raise ValueError(f"Oversized Bambu profile: {current}")
        profile = json.loads(current.read_text())
        if not isinstance(profile, dict) or (
            profile.get("type") != profile_type
            and not (template and "type" not in profile and profile.get("instantiation") == "false")
        ):
            raise ValueError(f"Expected a {profile_type} profile object: {current}")
        chain = (*chain, current)
        resolved = resolve(reference(profile["inherits"]), chain) if profile.get("inherits") else {}
        includes = profile.get("include", [])
        if isinstance(includes, str):
            includes = [includes]
        if not isinstance(includes, list):
            raise TypeError(f"Invalid filament profile includes: {current}")
        # Bambu applies templates after the parent and before the child's settings.
        # Template identity must not replace the selected filament's preset IDs.
        for name in includes:
            resolved.update(
                (key, value)
                for key, value in resolve(reference(name), chain, template=True).items()
                if key not in metadata
            )
        resolved.update(profile)
        resolved.pop("include", None)
        return resolved

    return resolve(path, ())
