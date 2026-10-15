"""Serialize measured report values with precision appropriate to their units."""

import json


def _rounded(value: object, decimals: int | None = None) -> object:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            # These contain authored dimensions, including exact CLI overrides.
            if key in ("dimensions_mm", "parameters", "variants"):
                result[key] = item
                continue
            digits = decimals
            if name.endswith("_mm3"):
                digits = 2
            elif key in ("bbox", "layer_height") or name.endswith("_mm"):
                digits = 3
            elif name.endswith("_g"):
                digits = 1
            result[key] = _rounded(item, digits)
        return result
    if isinstance(value, (list, tuple)):
        return [_rounded(item, decimals) for item in value]
    if isinstance(value, float) and decimals is not None:
        rounded = round(value, decimals)
        return 0.0 if rounded == 0 else rounded
    return value


def dumps_report(value: object) -> str:
    """Round known measurements without mutating calculation or configuration data."""
    return json.dumps(_rounded(value), indent=2, allow_nan=False) + "\n"
