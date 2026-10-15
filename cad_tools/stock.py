"""Read explicit filament stock supplied by the calling project."""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

MAX_STOCK_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Stock:
    id: str
    brand: str
    material: str
    color: str
    color_hex: str
    remaining_g: float

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.id, self.brand, self.material, self.color)
        ):
            raise ValueError("Stock requires nonempty string id, brand, material, and color")
        if not isinstance(self.color_hex, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", self.color_hex):
            raise ValueError("Stock color_hex must have the form #RRGGBB")
        if isinstance(self.remaining_g, bool) or not isinstance(self.remaining_g, int | float):
            raise ValueError("Stock remaining_g must be a positive finite number")
        try:
            valid_mass = self.remaining_g > 0 and math.isfinite(self.remaining_g)
        except OverflowError:
            valid_mass = False
        if not valid_mass:
            raise ValueError("Stock remaining_g must be a positive finite number")


def read_stock(path: Path) -> list[Stock]:
    with path.open("rb") as stream:
        content = stream.read(MAX_STOCK_BYTES + 1)
    if len(content) > MAX_STOCK_BYTES:
        raise ValueError("Stock JSON exceeds 2 MiB")
    records = json.loads(content)
    if not isinstance(records, list):
        raise ValueError("Stock JSON must contain a list of stock records")
    stocks = []
    identities = set()
    for number, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Stock record {number} must be an object")
        try:
            stock = Stock(**record)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid stock record {number}: {error}") from error
        if stock.id in identities:
            raise ValueError(f"Duplicate stock id: {stock.id}")
        identities.add(stock.id)
        stocks.append(stock)
    return stocks
