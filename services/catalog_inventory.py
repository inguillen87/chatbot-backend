from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Mapping


INVENTORY_STOCK_KEYS = (
    "stock_quantity",
    "stock",
    "cantidad",
    "existencias",
    "inventory",
    "inventario",
    "available_quantity",
    "qty",
)

INVENTORY_PRICE_KEYS = (
    "precio",
    "price",
    "precio_monetario",
    "precio_unitario",
    "unit_price",
)


def first_non_empty(row: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
        upper = key.upper()
        if upper in row and row.get(upper) not in (None, ""):
            return row.get(upper)
        title = key.title()
        if title in row and row.get(title) not in (None, ""):
            return row.get(title)
    return None


def stock_value_from_row(row: Mapping[str, Any]) -> Any:
    return first_non_empty(row, INVENTORY_STOCK_KEYS)


def parse_stock_quantity(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"sin stock", "agotado", "no disponible", "out of stock"}:
        return 0.0
    match = re.search(r"-?\d+(?:[\.,]\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def inventory_status(
    raw_stock: Any,
    *,
    available: bool = True,
    low_stock_threshold: float = 5,
) -> str:
    if not available:
        return "not_available"
    quantity = parse_stock_quantity(raw_stock)
    if quantity is None:
        return "stock_unknown"
    if quantity <= 0:
        return "out_of_stock"
    if quantity <= low_stock_threshold:
        return "low_stock"
    return "in_stock"


def inventory_contract(
    raw_stock: Any,
    *,
    available: bool = True,
    low_stock_threshold: float = 5,
    source: str = "catalog",
    updated_at: datetime | str | None = None,
) -> dict[str, Any]:
    quantity = parse_stock_quantity(raw_stock)
    status = inventory_status(
        raw_stock,
        available=available,
        low_stock_threshold=low_stock_threshold,
    )
    if isinstance(updated_at, datetime):
        updated_at_out = updated_at.astimezone(timezone.utc).isoformat()
    else:
        updated_at_out = updated_at
    return {
        "contract_version": "catalog.inventory_item.v1",
        "stock": raw_stock,
        "stock_quantity": quantity,
        "stock_status": status,
        "available_to_sell": bool(available and quantity is not None and quantity > 0),
        "can_start_order": bool(available and status not in {"not_available", "out_of_stock"}),
        "can_confirm_order": bool(available and quantity is not None and quantity > 0),
        "low_stock_threshold": low_stock_threshold,
        "inventory_source": source,
        "updated_at": updated_at_out,
    }


def inventory_columns_contract() -> dict[str, Any]:
    return {
        "contract_version": "catalog.inventory_columns.v1",
        "stock_columns": list(INVENTORY_STOCK_KEYS),
        "price_columns": list(INVENTORY_PRICE_KEYS),
        "supported_import_modes": ["upsert", "replace", "stock_only"],
    }


def new_catalog_version(tenant_id: int | str) -> str:
    return f"cat_{tenant_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
