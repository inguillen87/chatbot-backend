from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re
from difflib import SequenceMatcher
from statistics import median
from typing import Any

from models import CatalogUpload, CatalogoItem, MarketOrder, TenantProfile
from services.common_utils import parse_precio_flexible


def _safe_count(query) -> int:
    try:
        return int(query.count() or 0)
    except Exception:
        return 0


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    """Compat parser used by document-intelligence catalog tests and imports."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.-]", "", text)
    if not text:
        return None

    if "," in text and "." in text:
        last_comma = text.rfind(",")
        last_dot = text.rfind(".")
        decimal_sep = "," if last_comma > last_dot else "."
        thousand_sep = "." if decimal_sep == "," else ","
        normalized = text.replace(thousand_sep, "").replace(decimal_sep, ".")
    elif "," in text:
        parts = text.split(",")
        if len(parts[-1]) in (1, 2):
            normalized = "".join(parts[:-1]).replace(".", "") + "." + parts[-1]
        else:
            normalized = "".join(parts).replace(".", "")
    elif "." in text:
        parts = text.split(".")
        if len(parts[-1]) in (1, 2):
            normalized = "".join(parts[:-1]).replace(",", "") + "." + parts[-1]
        else:
            normalized = "".join(parts).replace(",", "")
    else:
        normalized = text

    try:
        return float(normalized)
    except ValueError:
        return None


def evaluate_catalog_quality(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate normalized catalog rows without blocking import.

    This is the legacy document-intelligence quality hook. It intentionally
    returns the same list shape with extra fields so existing import routes keep
    working while newer admin panels use ``build_catalog_quality_payload``.
    """

    prices = [_to_float(item.get("precio") or item.get("price")) for item in items]
    valid_prices = [price for price in prices if price is not None and price > 0]
    median_price = median(valid_prices) if valid_prices else None

    evaluated: list[dict[str, Any]] = []
    known_names: list[str] = []

    for item in items:
        score = 1.0
        issues: list[str] = []
        nombre = str(item.get("nombre") or item.get("title") or "").strip()
        categoria = str(item.get("categoria") or item.get("category") or "").strip()
        precio = _to_float(item.get("precio") or item.get("price"))

        if len(nombre) < 3:
            score -= 0.35
            issues.append("nombre_demasiado_corto")
        if not categoria:
            score -= 0.2
            issues.append("categoria_vacia")
        if precio is None or precio <= 0:
            score -= 0.35
            issues.append("precio_invalido")
        elif median_price and median_price > 0:
            ratio = precio / median_price
            if ratio > 8 or ratio < 0.12:
                score -= 0.25
                issues.append("precio_outlier")

        duplicate = False
        for previous_name in known_names:
            similarity = SequenceMatcher(None, nombre.lower(), previous_name.lower()).ratio()
            if similarity >= 0.93:
                duplicate = True
                break
        if duplicate:
            score -= 0.2
            issues.append("duplicado_aproximado")
        known_names.append(nombre)

        score = max(0.0, min(1.0, round(score, 2)))
        enriched = dict(item)
        enriched["confidence_score"] = enriched.get("confidence_score", score)
        enriched["quality_issues"] = issues
        enriched["review_required"] = float(enriched["confidence_score"] or 0) < 0.65 or bool(issues)
        evaluated.append(enriched)
    return evaluated


def _price_value(item: CatalogoItem) -> float | None:
    parsed = None
    try:
        _, parsed, _ = parse_precio_flexible(item.precio)
    except Exception:
        parsed = None
    if parsed is not None and parsed > 0:
        return float(parsed)

    points = _number(getattr(item, "precio_puntos", None))
    if points is not None and points > 0:
        return points
    return None


def _stock_value(item: CatalogoItem) -> float | None:
    raw = getattr(item, "cantidad", None)
    if raw in (None, ""):
        return None
    text = str(raw).strip().lower()
    if text in {"sin stock", "agotado", "no disponible"}:
        return 0
    value = _number(text)
    return value


def _metadata(item: CatalogoItem) -> dict[str, Any]:
    return item.extra_metadata if isinstance(item.extra_metadata, dict) else {}


def _gallery_urls(item: CatalogoItem) -> list[str]:
    metadata = _metadata(item)
    raw = metadata.get("gallery_urls") or metadata.get("imagenes") or metadata.get("images") or []
    if isinstance(raw, list):
        return [str(url).strip() for url in raw if str(url).strip()][:12]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _item_ref(item: CatalogoItem) -> dict[str, Any]:
    metadata = _metadata(item)
    image_url = (getattr(item, "imagen_url", None) or "").strip() or None
    return {
        "id": item.id,
        "catalogo_item_id": item.id,
        "name": item.nombre,
        "nombre": item.nombre,
        "sku": item.sku,
        "category": item.categoria,
        "categoria": item.categoria,
        "brand": item.marca,
        "marca": item.marca,
        "image_url": image_url,
        "imagen_url": image_url,
        "gallery_urls": _gallery_urls(item),
        "price": item.precio,
        "stock": item.cantidad,
        "available": bool(item.disponible is not False),
        "quality_tags": metadata.get("quality_tags") if isinstance(metadata.get("quality_tags"), list) else [],
        "updated_at": _iso(getattr(item, "timestamp", None)),
    }


def _queue_item(item: CatalogoItem, reason_code: str, action: str) -> dict[str, Any]:
    return {
        **_item_ref(item),
        "reason_code": reason_code,
        "suggested_action": action,
        "patch_endpoint": f"/api/admin/tenants/{{tenant_slug}}/catalog/items/{item.id}",
    }


def _latest_imports(tenant_id: int, limit: int = 5) -> list[dict[str, Any]]:
    uploads = (
        CatalogUpload.query.filter_by(tenant_id=tenant_id)
        .order_by(CatalogUpload.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for upload in uploads:
        stats = upload.stats if isinstance(upload.stats, dict) else {}
        preview = upload.preview_data if isinstance(upload.preview_data, dict) else {}
        image_summary = preview.get("image_summary") if isinstance(preview.get("image_summary"), dict) else {}
        items.append(
            {
                "id": upload.id,
                "upload_id": upload.id,
                "filename": upload.filename,
                "status": upload.status,
                "engine_used": upload.engine_used,
                "stats": stats,
                "image_summary": image_summary,
                "warnings": upload.warnings if isinstance(upload.warnings, list) else [],
                "errors": upload.errors if isinstance(upload.errors, list) else [],
                "created_at": _iso(upload.created_at),
            }
        )
    return items


def build_catalog_quality_payload(tenant: TenantProfile, *, limit: int = 20) -> dict[str, Any]:
    products = (
        CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
        .filter(CatalogoItem.tenant_id == tenant.id)
        .order_by(CatalogoItem.timestamp.desc().nullslast(), CatalogoItem.id.desc())
        .all()
    )

    missing_images = [item for item in products if not (getattr(item, "imagen_url", None) or "").strip()]
    missing_price = [item for item in products if _price_value(item) is None and str(item.modalidad or "").lower() not in {"donacion"}]
    missing_stock = [item for item in products if _stock_value(item) is None and str(item.modalidad or "").lower() in {"venta", "producto", ""}]
    unavailable = [item for item in products if item.disponible is False]
    missing_description = [item for item in products if not (getattr(item, "descripcion", None) or "").strip()]
    with_images = len(products) - len(missing_images)
    with_price = len(products) - len(missing_price)
    with_stock = len(products) - len(missing_stock)
    ready_to_sell = [
        item
        for item in products
        if item not in missing_images
        and item not in missing_price
        and item not in missing_stock
        and item.disponible is not False
    ]

    orders_count = 0
    pending_orders = 0
    try:
        orders_count = MarketOrder.legacy_safe_count(tenant_id=tenant.id)
        pending_orders = MarketOrder.legacy_safe_count(MarketOrder.status.in_(["pending", "created", "confirmed"]), tenant_id=tenant.id)
    except Exception:
        orders_count = 0
        pending_orders = 0

    total = len(products)
    def rate(amount: int) -> float:
        return round((amount / total) * 100, 2) if total else 100.0

    alerts = []
    if missing_images:
        alerts.append({"severity": "medium", "reason_code": "products_missing_images", "count": len(missing_images)})
    if missing_price:
        alerts.append({"severity": "high", "reason_code": "products_missing_price", "count": len(missing_price)})
    if missing_stock:
        alerts.append({"severity": "medium", "reason_code": "products_missing_stock", "count": len(missing_stock)})

    return {
        "contract_version": "catalog.quality.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "vertical": tenant.vertical,
        },
        "summary": {
            "products": total,
            "ready_to_sell": len(ready_to_sell),
            "missing_images": len(missing_images),
            "missing_price": len(missing_price),
            "missing_stock": len(missing_stock),
            "missing_description": len(missing_description),
            "unavailable": len(unavailable),
            "orders": orders_count,
            "pending_orders": pending_orders,
            "image_coverage_rate": rate(with_images),
            "price_coverage_rate": rate(with_price),
            "stock_coverage_rate": rate(with_stock),
            "ready_rate": rate(len(ready_to_sell)),
        },
        "queues": {
            "missing_images": [_queue_item(item, "missing_image", "upload_or_set_image_url") for item in missing_images[:limit]],
            "missing_price": [_queue_item(item, "missing_price", "set_price") for item in missing_price[:limit]],
            "missing_stock": [_queue_item(item, "missing_stock", "set_stock") for item in missing_stock[:limit]],
            "unavailable": [_queue_item(item, "unavailable", "review_availability") for item in unavailable[:limit]],
            "missing_description": [_queue_item(item, "missing_description", "write_short_description") for item in missing_description[:limit]],
        },
        "imports": {
            "latest": _latest_imports(tenant.id, limit=5),
            "accepted_file_types": ["csv", "xlsx", "xls", "txt", "pdf", "png", "jpg", "jpeg", "webp"],
            "image_columns": ["imagen_url", "image_url", "foto", "foto_url", "gallery_urls", "imagenes", "images"],
            "image_extraction_from_import": True,
        },
        "media_capabilities": {
            "manual_image_url_edit": True,
            "gallery_urls": True,
            "bulk_import_images": True,
            "pdf_catalog_generation": True,
            "qdrant_vector_sync": True,
        },
        "endpoints": {
            "items": f"/api/admin/tenants/{tenant.slug}/catalog/items",
            "item_patch_template": f"/api/admin/tenants/{tenant.slug}/catalog/items/{{item_id}}",
            "bulk_import_legacy": "/api/admin/catalogo/importar",
            "bulk_import_v2": "/api/admin/catalog/import",
            "vector_sync": "/api/admin/catalog/vector-sync",
            "orders": f"/api/admin/tenants/{tenant.slug}/orders",
            "public_market": f"/market/{tenant.slug}",
        },
        "alerts": alerts,
        "recommended_actions": [
            action
            for action in [
            {"kind": "fix_missing_price", "priority": "high", "label": "Completar precios"} if missing_price else None,
            {"kind": "fix_missing_images", "priority": "medium", "label": "Agregar imagenes"} if missing_images else None,
            {"kind": "publish_catalog", "priority": "medium", "label": "Publicar catalogo listo"} if ready_to_sell else None,
            ]
            if action
        ],
        "frontend_contract": {
            "render_as": "catalog_quality_command_center",
            "primary_view": "quality_board",
            "queue_tabs": ["missing_images", "missing_price", "missing_stock", "unavailable", "missing_description"],
            "allow_inline_patch": True,
            "empty_state_behavior": "show_import_and_first_product_actions",
        },
    }
