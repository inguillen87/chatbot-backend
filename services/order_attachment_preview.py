from __future__ import annotations

from typing import Any

from services.conversation_summaries import build_order_confirmation_payload
from services.llm_utils import extraer_lista_pedido_de_texto_con_llm
from services.pedido_processor_service import buscar_item_en_catalogo, UMBRAL_SIMILITUD_OCR_PEDIDO
from services.common_utils import parse_precio_flexible


def build_order_attachment_preview(*, texto_extraido: str, pyme_id_context: int | None = None, telefono: Any = None, email: Any = None, nombre: Any = None, direccion: Any = None, channel: str | None = None) -> dict[str, Any]:
    items_ocr = extraer_lista_pedido_de_texto_con_llm(texto_extraido, pyme_id_context=pyme_id_context)
    normalized_items: list[dict[str, Any]] = []
    matched_items = 0
    for item in items_ocr[:10]:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("nombre_producto_ocr") or item.get("nombre_ocr") or "").strip()
        if not item_name:
            continue
        cantidad = item.get("cantidad_ocr")
        unidad = str(item.get("unidad_ocr") or "").strip() or None
        normalized = {
            "nombre": item_name,
            "cantidad": cantidad if cantidad is not None else 1,
            "unidad": unidad,
            "catalog_match": None,
        }
        if pyme_id_context:
            matched = buscar_item_en_catalogo(
                pyme_user_id=pyme_id_context,
                nombre_busqueda=item_name,
                sku_busqueda=item.get("sku_ocr"),
                umbral_similitud=UMBRAL_SIMILITUD_OCR_PEDIDO,
                es_ocr=True,
            )
            if matched:
                _, precio_float, moneda = parse_precio_flexible(getattr(matched, "precio", None))
                normalized["catalog_match"] = {
                    "catalog_item_id": matched.id,
                    "nombre": matched.nombre,
                    "sku": matched.sku,
                    "precio": precio_float,
                    "moneda": moneda or "ARS",
                    "stock": getattr(matched, "cantidad", None),
                }
                matched_items += 1
        normalized_items.append(normalized)

    preview_summary = {
        "items_detalle": [
            {
                "nombre_producto": (item.get("catalog_match") or {}).get("nombre") or item["nombre"],
                "cantidad": item["cantidad"],
            }
            for item in normalized_items
        ]
    }
    order_confirmation = build_order_confirmation_payload(
        cart_summary=preview_summary,
        customer={
            "telefono": telefono,
            "email": email,
            "nombre": nombre,
            "direccion": direccion,
        },
        delivery_address=direccion,
        channel=channel,
    )

    if normalized_items:
        items_lines = []
        for item in normalized_items[:5]:
            unidad_suffix = f" {item['unidad']}" if item.get("unidad") else ""
            items_lines.append(f"- {item['cantidad']} x {item['nombre']}{unidad_suffix}")
        message = (
            "Procesé el adjunto y detecté este pedido preliminar:\n\n"
            + "\n".join(items_lines)
            + "\n\nSi está bien, puedo seguir armando el pedido o corregimos lo que haga falta."
        )
        options_list = [
            {"texto": "Confirmar pedido", "action_id": "finalizar_pedido_pyme"},
            {"texto": "Corregir pedido", "action_id": "corregir_datos_pedido"},
        ]
    else:
        message = (
            f"Procesé el adjunto y pude extraer texto, pero todavía no logré separar productos con seguridad.\n\n"
            f"Texto detectado:\n{texto_extraido[:400]}\n\n"
            "¿Quieres que lo intentemos interpretar juntos o me indicas los productos principales?"
        )
        options_list = [
            {"texto": "Indicar productos", "action_id": "pyme_hacer_pedido"},
            {"texto": "Hablar con asesor", "action_id": "pyme_hablar_agente"},
        ]

    return {
        "message": message,
        "items_detectados": normalized_items,
        "catalog_match_summary": {
            "matched": matched_items,
            "unmatched": max(len(normalized_items) - matched_items, 0),
            "total": len(normalized_items),
        },
        "order_confirmation": order_confirmation,
        "confirmation_card": order_confirmation,
        "options_list": options_list,
    }
