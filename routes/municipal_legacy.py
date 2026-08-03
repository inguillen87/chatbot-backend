import os
import unicodedata
from io import BytesIO
import importlib.util
from typing import Any, Iterator, Pattern, Union

import pandas as pd
from flask import Blueprint, jsonify, request, current_app, send_file, g
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from datetime import datetime, timedelta, timezone
from utils.time_utils import get_local_now
from utils.permissions import require_role
# from routes.crm import _obtener_clientes
from services.municipio_responder import TODAS_LAS_CATEGORIAS_UNICAS
from routes.categorias import _bootstrap_municipio_categories, _serialize_categoria
from routes.tramites import listar_tramites, obtener_tramite
from sqlalchemy import func, or_
from models import Categoria, Conversacion, MunicipioTicket, MunicipioPost, User, db, TenantProfile
from utils.municipio_utils import get_numeric_municipio_id
from routes.ticket import TICKET_ALLOWED_STATES
from services.encuestas_service import list_public_encuestas_for_tenant, serialize_public_encuesta
from config import ALLOWED_ORIGINS as DEFAULT_ALLOWED_ORIGINS
from services.municipal_stats import build_stats_for_municipio, StatsFilters
from services.employee_ticket_access import apply_employee_ticket_category_scope
from services.tenant_ticket_scope import (
    municipio_ticket_scope_filter,
    resolve_unique_tenant_for_owner,
    scoped_municipio_ticket_query,
)
from socket_service import emit_tenant_update

municipal_bp = Blueprint('municipal_legacy', __name__, url_prefix='/municipal')


AllowedOrigin = Union[str, Pattern[str]]


def _iter_allowed_origins() -> Iterator[AllowedOrigin]:
    """Yield the configured CORS origins in priority order."""

    env_value = os.getenv("CORS_ALLOWED_ORIGINS")
    if env_value is not None:
        stripped = env_value.strip()
        if stripped == "*":
            yield "*"
            return

        seen = set()
        for raw in stripped.split(","):
            candidate = raw.strip().rstrip("/")
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            yield candidate
        if seen:
            return

    for origin in DEFAULT_ALLOWED_ORIGINS:
        yield origin


def _resolve_cors_origin(origin: str | None) -> tuple[str | None, bool]:
    """Return the header value and whether credentials are allowed."""

    if not origin:
        return None, False

    normalized = origin.rstrip("/")
    for allowed in _iter_allowed_origins():
        if allowed == "*":
            return "*", False
        if isinstance(allowed, str):
            if normalized == allowed.rstrip("/"):
                return origin, True
        elif hasattr(allowed, "match") and allowed.match(origin):
            return origin, True

    return None, False


def _merge_header_values(response, header_name, values):
    """Ensure the given response header contains the provided comma-separated values."""

    existing = response.headers.get(header_name, "")
    items = [item.strip() for item in existing.split(",") if item.strip()]

    updated = list(items)
    for value in values:
        if value not in updated:
            updated.append(value)

    if updated:
        response.headers[header_name] = ", ".join(updated)


@municipal_bp.after_request
def apply_cors_headers(response):
    """Apply permissive CORS defaults for municipal endpoints."""

    allowed_origin, allow_credentials = _resolve_cors_origin(
        request.headers.get("Origin")
    )

    if allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin

        if allowed_origin != "*":
            _merge_header_values(response, "Vary", ["Origin"])
            if allow_credentials:
                response.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            response.headers.pop("Access-Control-Allow-Credentials", None)

        _merge_header_values(
            response,
            "Access-Control-Allow-Headers",
            [
                "Authorization",
                "Content-Type",
                "Origin",
                "Accept",
                "X-Entity-Token",
                "X-Chat-Session-Id",
                "X-Anon-Id",
                "Anon-Id",
            ],
        )

        _merge_header_values(
            response,
            "Access-Control-Allow-Methods",
            ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
    else:
        response.headers.pop("Access-Control-Allow-Credentials", None)

    return response


@municipal_bp.route("/whatsapp", methods=["GET", "OPTIONS"])
def municipal_whatsapp_placeholder():
    """Placeholder que evita 404 en la sección de WhatsApp del panel.

    Devuelve un cuerpo JSON mínimo para que el frontend pueda mostrar un
    estado coherente incluso cuando aún no se configuró la integración.
    """

    if request.method == "OPTIONS":
        return "", 204

    payload = {
        "ok": True,
        "integraciones": [],
        "mensaje": "Integración de WhatsApp no configurada en este entorno",
    }
    return jsonify(payload)


@municipal_bp.route("/integrations", methods=["GET", "OPTIONS"])
def municipal_integrations_placeholder():
    """Placeholder para la vista de integraciones del panel admin.

    Responde con un arreglo vacío y un mensaje descriptivo para evitar que el
    frontend reciba un 404/HTML y muestre pantallas en blanco.
    """

    if request.method == "OPTIONS":
        return "", 204

    payload = {
        "ok": True,
        "integraciones": [],
        "mensaje": "No hay integraciones configuradas en este entorno",
    }
    return jsonify(payload)


def _resolve_current_municipio_id(user) -> Any:
    """Return the municipio identifier associated with the request user.

    Municipal employees are linked to their owner entity through ``empresa_id``
    and ``g.owner_user``. Prefer the municipality configured on the owner when
    available so the endpoints operate on real production data instead of the
    demo placeholders used when no association is found.
    """

    municipio_id = getattr(user, "municipio_id", None)
    if municipio_id is not None:
        return municipio_id

    owner_user = getattr(g, "owner_user", None)
    if owner_user is not None:
        owner_municipio_id = getattr(owner_user, "municipio_id", None)
        if owner_municipio_id is not None:
            return owner_municipio_id
        if getattr(owner_user, "tipo_chat", None) == "municipio":
            owner_id = getattr(owner_user, "id", None)
            if owner_id is not None:
                return owner_id

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id is not None:
        return empresa_id

    if getattr(user, "tipo_chat", None) == "municipio":
        fallback_id = getattr(user, "id", None)
        if fallback_id is not None:
            return fallback_id

    return None


def _resolve_current_municipio_tenant(user) -> TenantProfile | None:
    request_tenant = getattr(g, "tenant_profile", None)
    if request_tenant is not None:
        return request_tenant

    user_tenant_id = getattr(user, "tenant_id", None)
    if user_tenant_id:
        tenant = db.session.get(TenantProfile, user_tenant_id)
        if tenant is not None:
            return tenant

    owner_id = _resolve_current_municipio_id(user)
    if owner_id is None:
        return None
    try:
        resolution = resolve_unique_tenant_for_owner(owner_id)
    except ValueError:
        return None
    return resolution.tenant if resolution.status == "unique" else None


_STATS_RANGE_OPTIONS = [
    {"id": "last_7_days", "label": "Últimos 7 días"},
    {"id": "last_30_days", "label": "Últimos 30 días"},
    {"id": "this_month", "label": "Este mes"},
    {"id": "this_year", "label": "Este año"},
    {"id": "today", "label": "Hoy"},
]


def _normalize_filter_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower().replace("_", " ").replace("-", " ")
    return " ".join(normalized.split())


def _parse_str_list(args, key: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in args.getlist(key):
        if raw:
            trimmed = raw.strip()
            if trimmed:
                values.append(trimmed)
    for raw in args.getlist(f"{key}[]"):
        if raw:
            trimmed = raw.strip()
            if trimmed:
                values.append(trimmed)
    if not values:
        raw = args.get(key)
        if raw:
            values.extend(part.strip() for part in raw.split(",") if part.strip())
    if not values:
        return ()

    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        lower = value.lower()
        if lower not in seen:
            seen.add(lower)
            unique.append(value)
    return tuple(unique)


def _parse_int_list(args, key: str) -> tuple[int, ...]:
    result: list[int] = []
    seen: set[int] = set()
    for raw in _parse_str_list(args, key):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _parse_date_param(value: str | None, tzinfo, *, is_end: bool = False) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo)
    if "T" not in raw and is_end:
        parsed = parsed + timedelta(days=1)
    return parsed


def _resolve_range_datetimes(value: str | None, now: datetime) -> tuple[datetime | None, datetime | None]:
    normalized = _normalize_filter_text(value)
    if not normalized:
        return None, None

    if "7" in normalized and ("dia" in normalized or "day" in normalized):
        return now - timedelta(days=7), now
    if "30" in normalized and ("dia" in normalized or "day" in normalized):
        return now - timedelta(days=30), now
    if "mes" in normalized:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return start, next_month
    if "ano" in normalized or "year" in normalized:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        next_year = start.replace(year=start.year + 1)
        return start, next_year
    if "hoy" in normalized or "today" in normalized:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if "ayer" in normalized or "yesterday" in normalized:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    return None, None


def _build_stats_filters_from_request(args) -> tuple[StatsFilters | None, datetime | None, datetime | None]:
    current_now = get_local_now()
    tzinfo = current_now.tzinfo
    fecha_inicio, fecha_fin = _resolve_range_datetimes(args.get("rango"), current_now)

    explicit_inicio = _parse_date_param(args.get("fecha_inicio"), tzinfo)
    if explicit_inicio:
        fecha_inicio = explicit_inicio

    explicit_fin = _parse_date_param(args.get("fecha_fin"), tzinfo, is_end=True)
    if explicit_fin:
        fecha_fin = explicit_fin

    estados = _parse_str_list(args, "estado")
    categorias = _parse_str_list(args, "categoria")
    distritos = _parse_str_list(args, "distrito")
    canales = _parse_str_list(args, "canal")
    agentes = _parse_int_list(args, "agente_id")
    if not agentes:
        agentes = _parse_int_list(args, "agente")

    filters = StatsFilters(
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        estados=estados or None,
        categorias=categorias or None,
        distritos=distritos or None,
        canales=canales or None,
        agentes=agentes or None,
    )

    if filters.is_empty():
        filters = None

    return filters, fecha_inicio, fecha_fin


def _format_value_for_export(value: Any) -> str:
    """Return a human readable representation for export files."""

    if isinstance(value, bool):
        return "Sí" if value else "No"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value)}"
        return f"{value:.2f}"
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()  # type: ignore[call-arg]
        except Exception:  # pragma: no cover - defensive
            pass
    if value is None:
        return "-"
    return str(value)


def _format_filters_for_export(
    filters: StatsFilters | None,
    fecha_inicio: datetime | None,
    fecha_fin: datetime | None,
) -> list[tuple[str, str]]:
    """Build a list of human-readable filter descriptions."""

    items: list[tuple[str, str]] = []

    if fecha_inicio:
        items.append(("Fecha desde", fecha_inicio.date().isoformat()))
    if fecha_fin:
        items.append(("Fecha hasta", fecha_fin.date().isoformat()))

    if filters:
        if filters.estados:
            items.append(("Estados", ", ".join(filters.estados)))
        if filters.categorias:
            items.append(("Categorías", ", ".join(filters.categorias)))
        if filters.distritos:
            items.append(("Distritos", ", ".join(filters.distritos)))
        if filters.canales:
            items.append(("Canales", ", ".join(filters.canales)))
        if filters.agentes:
            agentes = ", ".join(str(agente) for agente in filters.agentes)
            items.append(("Agentes", agentes))

    if not items:
        items.append(("Filtros", "Sin filtros adicionales"))

    return items


def _sanitize_sheet_name(name: str) -> str:
    sanitized = (name or "").strip() or "Hoja"
    return sanitized[:31]


def _select_excel_engine() -> str:
    """Select an available engine for pandas Excel writer."""

    for candidate in ("xlsxwriter", "openpyxl"):
        if importlib.util.find_spec(candidate) is not None:
            return candidate
    raise RuntimeError(
        "No se encontró un motor para escribir archivos Excel. Instala 'xlsxwriter' o 'openpyxl'."
    )


def _write_key_value_sheet(
    writer: pd.ExcelWriter,
    sheet_name: str,
    items: list[tuple[str, Any]] | list[tuple[str, str]] | list[tuple[str, int]] | list[tuple[str, float]]
) -> None:
    rows = [
        {"Campo": key, "Valor": _format_value_for_export(value)} for key, value in items
    ]
    if not rows:
        rows = [{"Campo": "Sin datos", "Valor": ""}]
    df = pd.DataFrame(rows)
    df.to_excel(writer, sheet_name=_sanitize_sheet_name(sheet_name), index=False)


def _write_table_sheet(writer: pd.ExcelWriter, sheet_name: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    df.to_excel(writer, sheet_name=_sanitize_sheet_name(sheet_name), index=False)


def _build_stats_excel(
    stats: dict[str, Any],
    filtros: list[tuple[str, str]],
    metrics: dict[str, Any] | None = None,
) -> bytes:
    """Create an Excel workbook summarizing stats and optional metrics."""

    engine = _select_excel_engine()
    buffer = BytesIO()

    with pd.ExcelWriter(buffer, engine=engine) as writer:
        _write_key_value_sheet(writer, "Filtros", filtros)

        resumen = stats.get("resumen")
        if isinstance(resumen, dict) and resumen:
            _write_key_value_sheet(writer, "Resumen", list(resumen.items()))

        estados = stats.get("estados") or []
        if estados:
            _write_table_sheet(writer, "Estados", estados)

        categorias_rows: list[dict[str, Any]] = []
        for entry in stats.get("por_categoria", []) or []:
            satisf = entry.get("satisfaccion") or {}
            categorias_rows.append(
                {
                    "Categoría": entry.get("categoria"),
                    "Total": entry.get("total"),
                    "Abiertos": entry.get("abiertos"),
                    "Cerrados": entry.get("cerrados"),
                    "Satisfacción promedio": satisf.get("promedio"),
                    "Satisfacción respuestas": satisf.get("respuestas"),
                }
            )
        _write_table_sheet(writer, "Por categoría", categorias_rows)

        _write_table_sheet(writer, "Por distrito", stats.get("por_distrito") or [])
        _write_table_sheet(writer, "Por canal", stats.get("por_canal") or [])
        _write_table_sheet(writer, "Tendencia mensual", stats.get("tendencia_mensual") or [])
        _write_table_sheet(writer, "Tendencia semanal", stats.get("tendencia_semanal") or [])

        tiempos_respuesta = stats.get("tiempos_respuesta") or {}
        if tiempos_respuesta:
            _write_key_value_sheet(writer, "Tiempos respuesta", list(tiempos_respuesta.items()))

        tiempos_cierre = stats.get("tiempos_cierre") or {}
        if tiempos_cierre:
            _write_key_value_sheet(writer, "Tiempos cierre", list(tiempos_cierre.items()))

        backlog = stats.get("backlog") or {}
        if backlog:
            _write_key_value_sheet(writer, "Backlog", list(backlog.items()))

        geolocalizacion = stats.get("geolocalizacion") or {}
        if geolocalizacion:
            geo_summary = [("Tickets con coordenadas", geolocalizacion.get("con_coordenadas", 0))]
            _write_key_value_sheet(writer, "Geolocalización", geo_summary)
            _write_table_sheet(writer, "Geo por categoría", geolocalizacion.get("por_categoria") or [])

        satisfaccion = stats.get("satisfaccion") or {}
        if satisfaccion:
            resumen_satisfaccion = [
                ("Promedio", satisfaccion.get("promedio")),
                ("Respuestas", satisfaccion.get("respuestas")),
            ]
            _write_key_value_sheet(writer, "Satisfacción", resumen_satisfaccion)
            _write_table_sheet(writer, "Distribución satisfacción", satisfaccion.get("distribucion") or [])

        sugerencias = stats.get("sugerencias") or {}
        if sugerencias:
            _write_key_value_sheet(writer, "Sugerencias", [("Total", sugerencias.get("total", 0))])
            _write_table_sheet(writer, "Sug. por estado", sugerencias.get("por_estado") or [])
            _write_table_sheet(writer, "Sug. por categoría", sugerencias.get("por_categoria") or [])
            _write_table_sheet(writer, "Sug. tendencia", sugerencias.get("tendencia_mensual") or [])

        if metrics:
            cards = metrics.get("cards")
            if cards:
                _write_table_sheet(writer, "Métricas cards", cards)
            summary = metrics.get("summary")
            if isinstance(summary, dict) and summary:
                _write_key_value_sheet(writer, "Resumen métricas", list(summary.items()))

    buffer.seek(0)
    return buffer.getvalue()


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_command(font: str, size: float, x: float, y: float, text: str) -> str:
    escaped = _pdf_escape(text)
    return f"BT /{font} {size:.0f} Tf {x:.2f} {y:.2f} Td ({escaped}) Tj ET\n"


def _chunk_lines_for_pdf(entries: list[tuple[str, bool, int]]) -> list[str]:
    """Convert textual entries into PDF drawing commands for each page."""

    if not entries:
        entries = [("Sin datos disponibles", False, 0)]

    page_height = 792.0
    top_margin = 60.0
    bottom_margin = 60.0
    line_height = 16.0
    indent_width = 18.0

    pages: list[list[str]] = []
    current_page: list[str] = []
    y = page_height - top_margin

    for text, is_title, indent in entries:
        if is_title and current_page:
            y -= line_height / 2
        if y <= bottom_margin:
            pages.append(current_page)
            current_page = []
            y = page_height - top_margin

        font = "F2" if is_title else "F1"
        size = 14 if is_title else 11
        x = 54.0 + max(indent, 0) * indent_width
        current_page.append(_text_command(font, size, x, y, text))
        y -= line_height

    pages.append(current_page)
    return ["".join(page) for page in pages if page]


def _assemble_pdf(pages: list[str]) -> bytes:
    objects: list[bytes | None] = []

    def add_object(body: bytes | None = None) -> int:
        objects.append(body)
        return len(objects)

    catalog_obj = add_object()
    pages_obj = add_object()
    font_regular = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_bold = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    page_numbers: list[int] = []
    for commands in pages or [""]:
        stream_bytes = commands.encode("latin-1", errors="replace")
        stream_obj = add_object(
            b"<< /Length "
            + str(len(stream_bytes)).encode("ascii")
            + b" >>\nstream\n"
            + stream_bytes
            + b"endstream"
        )
        page_obj = add_object(
            (
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 612 792] /Contents {stream_obj} 0 R "
                f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> >>"
            ).encode("ascii")
        )
        page_numbers.append(page_obj)

    kids = " ".join(f"{num} 0 R" for num in page_numbers)
    objects[pages_obj - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>"
    ).encode("ascii")
    objects[catalog_obj - 1] = (
        f"<< /Type /Catalog /Pages {pages_obj} 0 R >>"
    ).encode("ascii")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (len(objects) + 1)
    for idx, body in enumerate(objects, start=1):
        data = body or b"<< >>"
        offsets[idx] = len(pdf)
        pdf.extend(f"{idx} 0 obj\n".encode("ascii"))
        pdf.extend(data)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for idx in range(1, len(objects) + 1):
        pdf.extend(f"{offsets[idx]:010d} 00000 n \n".encode("ascii"))

    pdf.extend(
        (
            f"trailer << /Size {len(objects) + 1} /Root {catalog_obj} 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
        ).encode("ascii")
    )

    return bytes(pdf)


def _build_stats_pdf(
    stats: dict[str, Any],
    filtros: list[tuple[str, str]],
    metrics: dict[str, Any] | None = None,
) -> bytes:
    """Generate a simple PDF summarizing stats and optional metrics."""

    entries: list[tuple[str, bool, int]] = []

    entries.append(("Reporte de estadísticas municipales", True, 0))
    entries.append(("", False, 0))

    entries.append(("Filtros aplicados", True, 0))
    for key, value in filtros:
        entries.append((f"• {key}: {value}", False, 1))

    resumen = stats.get("resumen") or {}
    if resumen:
        entries.append(("", False, 0))
        entries.append(("Resumen", True, 0))
        for key, value in resumen.items():
            entries.append((f"• {key.replace('_', ' ').title()}: {_format_value_for_export(value)}", False, 1))

    def _append_list_section(title: str, rows: list[dict[str, Any]], key_labels: list[tuple[str, str]]):
        if not rows:
            return
        entries.append(("", False, 0))
        entries.append((title, True, 0))
        for row in rows:
            parts = []
            for key, label in key_labels:
                if key in row:
                    parts.append(f"{label}: {_format_value_for_export(row[key])}")
            entries.append((f"• {' | '.join(parts)}", False, 1))

    _append_list_section(
        "Estados",
        stats.get("estados") or [],
        [("estado", "Estado"), ("total", "Total"), ("porcentaje", "%")],
    )

    categorias_rows = []
    for entry in stats.get("por_categoria", []) or []:
        row = {
            "categoria": entry.get("categoria"),
            "total": entry.get("total"),
            "abiertos": entry.get("abiertos"),
            "cerrados": entry.get("cerrados"),
        }
        satisf = entry.get("satisfaccion") or {}
        if satisf:
            row["satisfaccion"] = f"{_format_value_for_export(satisf.get('promedio'))} ({satisf.get('respuestas', 0)} respuestas)"
        categorias_rows.append(row)
    _append_list_section(
        "Tickets por categoría",
        categorias_rows,
        [
            ("categoria", "Categoría"),
            ("total", "Total"),
            ("abiertos", "Abiertos"),
            ("cerrados", "Cerrados"),
            ("satisfaccion", "Satisfacción"),
        ],
    )

    _append_list_section(
        "Tickets por distrito",
        stats.get("por_distrito") or [],
        [("distrito", "Distrito"), ("total", "Total"), ("abiertos", "Abiertos"), ("cerrados", "Cerrados")],
    )
    _append_list_section(
        "Tickets por canal",
        stats.get("por_canal") or [],
        [("canal", "Canal"), ("total", "Total")],
    )
    _append_list_section(
        "Tendencia mensual",
        stats.get("tendencia_mensual") or [],
        [("label", "Periodo"), ("total", "Total"), ("cerrados", "Cerrados"), ("abiertos", "Abiertos")],
    )
    _append_list_section(
        "Tendencia semanal",
        stats.get("tendencia_semanal") or [],
        [("label", "Día"), ("total", "Total"), ("cerrados", "Cerrados"), ("abiertos", "Abiertos")],
    )

    for label, key in ("Tiempos de respuesta", "tiempos_respuesta"), ("Tiempos de cierre", "tiempos_cierre"):
        valores = stats.get(key) or {}
        if valores:
            entries.append(("", False, 0))
            entries.append((label, True, 0))
            for k, v in valores.items():
                entries.append((f"• {k.replace('_', ' ')}: {_format_value_for_export(v)}", False, 1))

    backlog = stats.get("backlog") or {}
    if backlog:
        entries.append(("", False, 0))
        entries.append(("Backlog", True, 0))
        for k, v in backlog.items():
            entries.append((f"• {k.replace('_', ' ')}: {_format_value_for_export(v)}", False, 1))

    geolocalizacion = stats.get("geolocalizacion") or {}
    if geolocalizacion:
        entries.append(("", False, 0))
        entries.append(("Geolocalización", True, 0))
        entries.append((
            f"• Tickets con coordenadas: {_format_value_for_export(geolocalizacion.get('con_coordenadas', 0))}",
            False,
            1,
        ))
        _append_list_section(
            "Categorías geolocalizadas",
            geolocalizacion.get("por_categoria") or [],
            [("categoria", "Categoría"), ("total", "Total")],
        )

    satisfaccion = stats.get("satisfaccion") or {}
    if satisfaccion:
        entries.append(("", False, 0))
        entries.append(("Satisfacción", True, 0))
        entries.append((f"• Promedio: {_format_value_for_export(satisfaccion.get('promedio'))}", False, 1))
        entries.append((f"• Respuestas: {_format_value_for_export(satisfaccion.get('respuestas'))}", False, 1))
        _append_list_section(
            "Distribución de satisfacción",
            satisfaccion.get("distribucion") or [],
            [("puntuacion", "Puntuación"), ("total", "Total")],
        )

    sugerencias = stats.get("sugerencias") or {}
    if sugerencias:
        entries.append(("", False, 0))
        entries.append(("Sugerencias", True, 0))
        entries.append((f"• Total: {_format_value_for_export(sugerencias.get('total', 0))}", False, 1))
        _append_list_section(
            "Sugerencias por estado",
            sugerencias.get("por_estado") or [],
            [("estado", "Estado"), ("total", "Total")],
        )
        _append_list_section(
            "Sugerencias por categoría",
            sugerencias.get("por_categoria") or [],
            [("categoria", "Categoría"), ("total", "Total")],
        )
        _append_list_section(
            "Tendencia de sugerencias",
            sugerencias.get("tendencia_mensual") or [],
            [("label", "Periodo"), ("total", "Total")],
        )

    if metrics:
        cards = metrics.get("cards") or []
        summary = metrics.get("summary") or {}
        if cards or summary:
            entries.append(("", False, 0))
            entries.append(("Métricas de mensajería", True, 0))
            for card in cards:
                label = card.get("label", "")
                value = card.get("value")
                entries.append((f"• {label}: {_format_value_for_export(value)}", False, 1))
            for key, value in summary.items():
                entries.append((f"• {key.replace('_', ' ').title()}: {_format_value_for_export(value)}", False, 1))

    entries.append(("", False, 0))
    entries.append((f"Generado: {get_local_now().isoformat()}", False, 0))

    pages = _chunk_lines_for_pdf(entries)
    return _assemble_pdf(pages)


def _dedupe_sorted(values, fallback: str) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        label = (value or "").strip()
        if not label:
            label = fallback
        key = label.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(label)
    cleaned.sort()
    return cleaned


@municipal_bp.route('/usuarios', methods=['GET', 'POST', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_usuarios(current_user):
    if request.method == 'OPTIONS':
        return "", 204

    tenant_slug = request.args.get("tenant_slug") or request.args.get("tenant") or getattr(current_user, "tenant_slug", None)
    tenant = None
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()

    municipio_ids: list[int] = []
    for candidate in (
        _resolve_current_municipio_id(current_user),
        getattr(current_user, "municipio_id", None),
        getattr(current_user, "empresa_id", None),
        getattr(tenant, "municipio_id", None) if tenant else None,
        getattr(current_user, "id", None) if getattr(current_user, "tipo_chat", None) == "municipio" else None,
    ):
        if candidate and candidate not in municipio_ids:
            municipio_ids.append(candidate)

    tenant_id = getattr(tenant, "id", None) if tenant else getattr(current_user, "tenant_id", None)
    query = User.query.filter(
        User.rol.in_(["admin", "empleado"]),
        or_(
            User.tipo_chat == "municipio",
            User.es_empleado.is_(True),
        ),
    )

    scope_filters = []
    if tenant_id:
        scope_filters.append(User.tenant_id == tenant_id)
    if municipio_ids:
        scope_filters.extend([
            User.municipio_id.in_(municipio_ids),
            User.empresa_id.in_(municipio_ids),
            User.id.in_(municipio_ids),
        ])
    if scope_filters:
        query = query.filter(or_(*scope_filters))

    q = (request.args.get("q") or "").strip().lower()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(func.lower(User.name).like(like), func.lower(User.email).like(like)))

    def serialize_agent(user: User) -> dict[str, Any]:
        categorias = []
        categoria_ids = []
        for cat in (getattr(user, "categorias_ticket", None) or getattr(user, "categorias", None) or []):
            cat_id = getattr(cat, "id", None)
            nombre = getattr(cat, "nombre", None)
            if cat_id is not None:
                categoria_ids.append(cat_id)
            if nombre:
                categorias.append({"id": cat_id, "nombre": nombre})

        for raw_name in getattr(user, "categorias_lista", []) or []:
            if raw_name and not any(c.get("nombre") == raw_name for c in categorias):
                categorias.append({"id": None, "nombre": raw_name})

        return {
            "id": user.id,
            "user_id": user.id,
            "nombre": user.name,
            "nombre_usuario": user.name,
            "name": user.name,
            "email": user.email,
            "telefono": user.telefono,
            "phone": user.telefono,
            "rol": user.rol,
            "role": user.rol,
            "tipo_chat": user.tipo_chat,
            "es_empleado": bool(user.es_empleado or user.rol == "empleado"),
            "avatar_url": user.logo_url,
            "tenant_id": user.tenant_id,
            "municipio_id": user.municipio_id,
            "empresa_id": user.empresa_id,
            "categoria_ids": categoria_ids,
            "categorias": categorias,
        }

    empleados = [serialize_agent(user) for user in query.order_by(User.rol.asc(), User.name.asc()).all()]
    return jsonify({
        "usuarios": empleados,
        "users": empleados,
        "empleados": empleados,
        "employees": empleados,
        "data": empleados,
        "total": len(empleados),
    })

@municipal_bp.route('/categorias', methods=['GET', 'OPTIONS'])
@token_requerido
@require_role('admin', 'empleado')
def municipal_categorias(current_user):
    if request.method == 'OPTIONS':
        return "", 204

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    _bootstrap_municipio_categories(municipio_id)
    categorias = (
        Categoria.query.filter_by(municipio_id=municipio_id)
        .order_by(Categoria.nombre.asc())
        .all()
    )
    serializadas = [_serialize_categoria(cat) for cat in categorias]
    payload = {"categorias": serializadas, "categories": serializadas}
    return jsonify(payload)


@municipal_bp.route("/tickets/categorias", methods=["GET", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado")
def municipal_tickets_categorias(current_user):
    """Alias de categorías pensado para el panel de tickets municipales."""

    if request.method == "OPTIONS":
        return "", 204

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    _bootstrap_municipio_categories(municipio_id)
    categorias = (
        Categoria.query.filter_by(municipio_id=municipio_id)
        .order_by(Categoria.nombre.asc())
        .all()
    )
    serializadas = [_serialize_categoria(cat) for cat in categorias]
    return jsonify({"categorias": serializadas, "categories": serializadas})

@municipal_bp.route('/estados', methods=['GET', 'OPTIONS'])
def municipal_estados():
    """Devuelve la lista pública de estados permitidos para tickets municipales."""

    if request.method == 'OPTIONS':
        return "", 204

    return jsonify({"estados": TICKET_ALLOWED_STATES})


@municipal_bp.route('/encuestas', methods=['GET', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_encuestas_list(current_user):
    """
    Listar encuestas del municipio (legacy endpoint compatibility).
    """
    if request.method == 'OPTIONS':
        return "", 204

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify([])

    tenant = _resolve_current_municipio_tenant(current_user)
    if not tenant:
        return jsonify([])

    try:
        encuestas = list_public_encuestas_for_tenant(tenant.id, limit=50)
    except Exception:
        return jsonify([])

    payload = []
    base_url = current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL") or request.host_url.rstrip("/")

    for encuesta, slug in encuestas:
        data = serialize_public_encuesta(encuesta, slug_publico=slug)
        data["url_publica"] = f"{base_url}/e/{slug}"
        payload.append(data)

    return jsonify(payload)

@municipal_bp.route('/stats', methods=['GET', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats(current_user):
    """Estadísticas profesionales del municipio del usuario."""

    if request.method == 'OPTIONS':
        return "", 204

    municipio_id = _resolve_current_municipio_id(current_user)
    filters, _, _ = _build_stats_filters_from_request(request.args)

    if filters:
        datos = build_stats_for_municipio(municipio_id, filters=filters, actor=current_user)
    else:
        datos = build_stats_for_municipio(municipio_id, actor=current_user)

    return jsonify(datos)


@municipal_bp.route('/stats/export/<string:formato>', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats_export(current_user, formato: str):
    """Permite exportar las estadísticas del municipio en PDF o Excel."""

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify({"error": "El usuario no posee un municipio asociado."}), 404

    filters, fecha_inicio, fecha_fin = _build_stats_filters_from_request(request.args)

    if filters:
        stats = build_stats_for_municipio(municipio_id, filters=filters, actor=current_user)
    else:
        stats = build_stats_for_municipio(municipio_id, actor=current_user)

    filtros_legibles = _format_filters_for_export(filters, fecha_inicio, fecha_fin)
    formato_normalizado = (formato or "").strip().lower()
    timestamp = get_local_now().strftime("%Y%m%d_%H%M%S")

    try:
        if formato_normalizado == "excel":
            contenido = _build_stats_excel(stats, filtros_legibles)
            nombre = f"estadisticas_municipio_{municipio_id}_{timestamp}.xlsx"
            return send_file(
                BytesIO(contenido),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=nombre,
            )
        if formato_normalizado == "pdf":
            contenido = _build_stats_pdf(stats, filtros_legibles)
            nombre = f"estadisticas_municipio_{municipio_id}_{timestamp}.pdf"
            return send_file(
                BytesIO(contenido),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=nombre,
            )
    except RuntimeError as err:
        current_app.logger.error("Error generando exportación de estadísticas: %s", err)
        return jsonify({"error": str(err)}), 500
    except Exception as err:  # pragma: no cover - protección adicional
        current_app.logger.error(
            "Falla inesperada al exportar estadísticas municipales", exc_info=True
        )
        return jsonify({"error": "No se pudo generar la exportación solicitada."}), 500

    return jsonify({"error": "Formato no soportado"}), 400

@municipal_bp.route('/stats/filters', methods=['GET', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats_filters(current_user):
    municipio_id = _resolve_current_municipio_id(current_user)
    tenant = _resolve_current_municipio_tenant(current_user)

    if municipio_id is None:
        return jsonify(
            {
                "categorias": [],
                "estados": sorted(TICKET_ALLOWED_STATES),
                "rangos": _STATS_RANGE_OPTIONS,
                "distritos": [],
                "canales": [],
                "agentes": [],
            }
        )

    ticket_scope = municipio_ticket_scope_filter(tenant)
    categorias_query = (
        db.session.query(MunicipioTicket.categoria)
        .filter(
            ticket_scope,
            MunicipioTicket.categoria.isnot(None),
            MunicipioTicket.categoria != "",
        )
        .distinct()
    )
    categorias = _dedupe_sorted((row[0] for row in categorias_query), "Sin categoría")

    distritos_query = (
        db.session.query(MunicipioTicket.distrito)
        .filter(
            ticket_scope,
            MunicipioTicket.distrito.isnot(None),
            MunicipioTicket.distrito != "",
        )
        .distinct()
    )
    distritos = _dedupe_sorted((row[0] for row in distritos_query), "Sin distrito")

    canales_query = (
        db.session.query(MunicipioTicket.canal_ingreso)
        .filter(
            ticket_scope,
            MunicipioTicket.canal_ingreso.isnot(None),
            MunicipioTicket.canal_ingreso != "",
        )
        .distinct()
    )
    canales = _dedupe_sorted((row[0] for row in canales_query), "Sin especificar")

    owner_user = getattr(g, "owner_user", None)
    owner_id = None
    if owner_user is not None:
        owner_id = getattr(owner_user, "id", None)
    if owner_id is None:
        owner_id = getattr(current_user, "empresa_id", None) or getattr(
            current_user, "id", None
        )

    user_conditions = [User.municipio_id == municipio_id]
    if owner_id is not None:
        user_conditions.append(User.empresa_id == owner_id)
        user_conditions.append(User.id == owner_id)

    if len(user_conditions) == 1:
        combined_condition = user_conditions[0]
    else:
        combined_condition = or_(*user_conditions)

    agentes_rows = (
        db.session.query(User.id, User.name, User.email, User.rol)
        .filter(combined_condition)
        .filter(or_(User.rol.is_(None), User.rol != "usuario"))
        .all()
    )

    agentes: list[dict[str, object]] = []
    vistos: set[int] = set()
    for row in agentes_rows:
        agent_id = int(getattr(row, "id", 0) or 0)
        if not agent_id or agent_id in vistos:
            continue
        vistos.add(agent_id)

        nombre = getattr(row, "name", None) or getattr(row, "email", None) or ""
        nombre = (nombre or "").strip() or f"Agente #{agent_id}"
        rol = getattr(row, "rol", None)

        agentes.append(
            {
                "id": agent_id,
                "name": nombre,
                "label": nombre,
                "value": agent_id,
                "rol": rol,
            }
        )

    agentes.sort(key=lambda item: item["label"].lower())

    return jsonify(
        {
            "categorias": categorias,
            "estados": sorted(TICKET_ALLOWED_STATES),
            "rangos": _STATS_RANGE_OPTIONS,
            "distritos": distritos,
            "canales": canales,
            "agentes": agentes,
        }
    )

@municipal_bp.route('/tramites', methods=['GET'])
def municipal_tramites():
    return listar_tramites()

@municipal_bp.route('/tramites/<string:nombre>', methods=['GET'])
def municipal_tramite(nombre):
    return obtener_tramite(nombre)

@municipal_bp.route('/tickets/map_data', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_tickets_map_data(current_user):
    """
    Devuelve datos de tickets municipales con ubicación para el municipio del
    usuario actual, optimizados para mostrar en un mapa. Se puede filtrar por
    estado (p.ej. ``abierto`` o ``cerrado``); si no se especifica, se incluyen
    todos los estados.
    """
    from services.ticket_service import servicio_tickets  # Importación local

    municipio_id_del_admin = _resolve_current_municipio_id(current_user)
    if municipio_id_del_admin is None:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    estado = request.args.get("estado")
    tickets_con_ubicacion = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket="municipio",
        actor=current_user,
        municipio_id=municipio_id_del_admin,
        estado=estado,
    )
    return jsonify(tickets_con_ubicacion)


@municipal_bp.route('/tickets/locations', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_tickets_locations(current_user):
    """
    Devuelve una lista de coordenadas de tickets para el mapa de calor.
    Formato: [{ "lat": lat, "lng": lng }]
    """
    from services.ticket_service import servicio_tickets

    municipio_id_del_admin = _resolve_current_municipio_id(current_user)
    if municipio_id_del_admin is None:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    locations = servicio_tickets.obtener_locations_de_tickets(
        municipio_id=municipio_id_del_admin,
        actor=current_user,
    )
    return jsonify(locations)


@municipal_bp.route('/incidents', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_incidents(current_user):
    """Lista los tickets municipales abiertos para el municipio del usuario."""

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    try:
        tenant = _resolve_current_municipio_tenant(current_user)
        tickets = (
            apply_employee_ticket_category_scope(
                scoped_municipio_ticket_query(tenant),
                current_user,
                MunicipioTicket,
            )
            .filter(MunicipioTicket.estado != 'cerrado') # Podríamos querer ver todos en el admin, no solo los no cerrados
            .order_by(MunicipioTicket.fecha.desc())
            .all()
        )
    except Exception:
        current_app.logger.exception("Error fetching municipal incidents")
        tickets = []

    resultado = [
        {
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "asunto": getattr(t, "asunto", "N/A"),
            "categoria": getattr(t, "categoria", None),
            "estado": t.estado,
            "fecha": t.fecha.isoformat() if getattr(t, "fecha", None) else None,
            "pregunta": getattr(t, "pregunta", None), # Descripción breve inicial
            "detalles": getattr(t, "detalles", None), # Detalles completos del reclamo
            "direccion": getattr(t, "direccion", None),
            "latitud": getattr(t, "latitud", None),
            "longitud": getattr(t, "longitud", None),
            "archivo_url": getattr(t, "archivo_url", None), # Para la foto
            # Datos del vecino/usuario si están disponibles (requeriría join con User o guardar en ticket)
            "nombre_vecino": getattr(t, "nombre_vecino", None), # Asumiendo que se añada al modelo o se obtenga de User
            "telefono_vecino": getattr(t, "telefono_vecino", None),
            "email_vecino": getattr(t, "email_vecino", None),
        }
        for t in tickets
    ]

    return jsonify(resultado)


def _municipal_message_metrics(
    eid: int,
    *,
    fecha_inicio: datetime | None = None,
    fecha_fin: datetime | None = None,
) -> dict:
    """Calcula métricas de mensajes recibidos en distintos períodos."""

    ahora = get_local_now()

    def _contar_desde(dias: int) -> int:
        desde = ahora - timedelta(days=dias)
        if fecha_inicio:
            desde = max(desde, fecha_inicio)
        query = (
            db.session.query(func.count())
            .select_from(Conversacion)
            .join(User, Conversacion.user_id == User.id)
            .filter(User.empresa_id == eid)
            .filter(Conversacion.timestamp >= desde)
        )
        if fecha_fin:
            query = query.filter(Conversacion.timestamp < fecha_fin)
        return int(query.scalar() or 0)

    cards = [
        {"label": "Mensajes esta semana", "value": _contar_desde(7)},
        {"label": "Mensajes este mes", "value": _contar_desde(30)},
        {"label": "Mensajes este año", "value": _contar_desde(365)},
    ]

    total_general_query = (
        db.session.query(func.count())
        .select_from(Conversacion)
        .join(User, Conversacion.user_id == User.id)
        .filter(User.empresa_id == eid)
    )
    total_general = int(total_general_query.scalar() or 0)

    filtrado_query = (
        db.session.query(func.count())
        .select_from(Conversacion)
        .join(User, Conversacion.user_id == User.id)
        .filter(User.empresa_id == eid)
    )
    if fecha_inicio:
        filtrado_query = filtrado_query.filter(Conversacion.timestamp >= fecha_inicio)
    if fecha_fin:
        filtrado_query = filtrado_query.filter(Conversacion.timestamp < fecha_fin)
    total_filtrado = int(filtrado_query.scalar() or 0)

    summary = {
        "last_7_days": cards[0]["value"],
        "last_30_days": cards[1]["value"],
        "last_365_days": cards[2]["value"],
        "total": total_general,
        "filtered_total": total_filtrado,
    }

    return {"cards": cards, "summary": summary}


@municipal_bp.route('/metrics', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_metrics(current_user):
    """Devuelve cantidad de mensajes de vecinos por rango de tiempo."""

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    return jsonify(_municipal_message_metrics(eid))


import os
import json
from werkzeug.utils import secure_filename
from urllib.parse import urlparse


def _normalize_public_media_url(raw_url: str | None) -> str | None:
    """Return a publicly accessible URL for media stored in the data volume.

    Historically the municipal backend stored image references under
    ``/data/archivos`` – the internal mount point of the persistent volume on
    Render.  Those paths are not reachable from WhatsApp or the public web, so
    replies delivered through Twilio ended up with broken images.  This helper
    rewrites those internal paths to the `/media/` blueprint that proxies files
    from the same volume.  Absolute ``http(s)`` URLs or ``data:`` URIs are left
    untouched so manually provided links continue to work.
    """

    if not raw_url:
        return None

    candidate = str(raw_url).strip()
    if not candidate:
        return None

    # Allow inline images or fully qualified URLs without modification.
    if candidate.startswith("data:"):
        return candidate

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        return candidate

    # Normalise Windows style backslashes that may appear when copying paths.
    candidate = candidate.replace("\\", "/")

    # Common cases coming from agenda text or form uploads.
    if candidate.startswith("/data/archivos/"):
        candidate = candidate.replace("/data/", "/media/", 1)
    elif candidate.startswith("data/archivos/"):
        candidate = "/" + candidate.replace("data/", "media/", 1)
    elif candidate.startswith("/data/"):
        candidate = candidate.replace("/data/", "/media/", 1)
    elif candidate.startswith("data/"):
        candidate = "/" + candidate.replace("data/", "media/", 1)
    elif candidate.startswith("/archivos/"):
        candidate = "/media" + candidate
    elif candidate.startswith("archivos/"):
        candidate = "/media/" + candidate
    elif candidate.startswith("/media/"):
        # Already normalised.
        pass
    else:
        # Treat any other relative path as a file within /media/archivos so it
        # can be resolved by the media blueprint.
        if not candidate.startswith("/"):
            candidate = f"/media/archivos/{candidate}"

    return candidate


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    normalized = candidate.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(normalized.replace(" ", "T", 1))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _month_bounds(month_str: str) -> tuple[datetime | None, datetime | None]:
    try:
        base = datetime.strptime(month_str, "%Y-%m")
    except ValueError:
        return None, None
    start = base.replace(day=1, tzinfo=timezone.utc)
    if base.month == 12:
        end = base.replace(year=base.year + 1, month=1, day=1, tzinfo=timezone.utc)
    else:
        end = base.replace(month=base.month + 1, day=1, tzinfo=timezone.utc)
    return start, end


def _prune_old_posts(municipio_id: int, max_posts: int = 200) -> int:
    db_municipio_id = get_numeric_municipio_id(municipio_id)
    if db_municipio_id is None:
        return 0
    surplus_ids = (
        db.session.query(MunicipioPost.id)
        .filter(MunicipioPost.municipio_id == db_municipio_id)
        .order_by(MunicipioPost.fecha_publicacion.desc(), MunicipioPost.id.desc())
        .offset(max_posts)
        .all()
    )
    ids_to_delete = [row[0] for row in surplus_ids]
    if not ids_to_delete:
        return 0
    deleted = (
        db.session.query(MunicipioPost)
        .filter(MunicipioPost.id.in_(ids_to_delete))
        .delete(synchronize_session=False)
    )
    return deleted or 0


@municipal_bp.route('/posts', methods=['GET'])
def list_municipal_posts():
    """Devuelve los posts municipales almacenados en la base de datos."""
    # Use resolve_tenant_only as resolve_tenant_from_request might be deprecated or missing
    from services.tenant_resolver import resolve_tenant_only as resolve_tenant_from_request

    tenant = resolve_tenant_from_request()
    municipio_id = None
    if tenant and tenant.municipio_id:
        municipio_id = tenant.municipio_id

    if municipio_id is None:
        # Fallback to current_user if authenticated (for admin panel)
        try:
            from flask_jwt_extended import get_current_user
            current_user = get_current_user()
            if current_user:
                municipio_id = _resolve_current_municipio_id(current_user)
        except Exception:
            pass

    limit = request.args.get("limit", type=int) or 20
    limit = max(1, min(limit, 100))
    offset = request.args.get("offset", type=int) or 0
    offset = max(0, offset)

    if municipio_id is None:
        # Allow accessing if specific tenant header/slug context is missing but this is a legacy call often made by public frontend
        # We need a default or return empty
        return jsonify(
            {
                "posts": [],
                "items": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "filters": {},
                "reason_code": "municipio_not_available_for_tenant",
                "message": "No hay publicaciones municipales para el tenant solicitado.",
            }
        ), 200

    db_municipio_id = get_numeric_municipio_id(municipio_id)
    if db_municipio_id is None:
        return jsonify(
            {
                "posts": [],
                "items": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "filters": {},
                "reason_code": "invalid_municipio_id",
                "message": "El identificador del municipio es invalido.",
            }
        ), 200
    month_param = request.args.get("month")
    date_param = request.args.get("date")
    from_param = request.args.get("from_date") or request.args.get("fecha_desde")
    to_param = request.args.get("to_date") or request.args.get("fecha_hasta")
    tipo_post = request.args.get("tipo_post")

    query = MunicipioPost.query.filter(MunicipioPost.municipio_id == db_municipio_id)

    applied_filters: dict[str, Any] = {}

    if tipo_post:
        normalized_tipo = tipo_post.strip().lower()
        query = query.filter(MunicipioPost.tipo_post == normalized_tipo)
        applied_filters["tipo_post"] = normalized_tipo

    if month_param:
        start, end = _month_bounds(month_param)
        if not start or not end:
            return jsonify({"error": "Formato de mes inválido. Usa YYYY-MM."}), 400
        query = query.filter(
            MunicipioPost.fecha_publicacion >= start,
            MunicipioPost.fecha_publicacion < end,
        )
        applied_filters["month"] = month_param

    if date_param:
        day_start = _parse_iso_datetime(date_param)
        if not day_start:
            return jsonify({"error": "Formato de fecha inválido. Usa YYYY-MM-DD."}), 400
        day_end = day_start + timedelta(days=1)
        query = query.filter(
            MunicipioPost.fecha_publicacion >= day_start,
            MunicipioPost.fecha_publicacion < day_end,
        )
        applied_filters["date"] = date_param

    if from_param:
        from_date = _parse_iso_datetime(from_param)
        if not from_date:
            return jsonify({"error": "Formato de fecha_desde inválido. Usa YYYY-MM-DD."}), 400
        query = query.filter(MunicipioPost.fecha_publicacion >= from_date)
        applied_filters["from_date"] = from_param

    if to_param:
        to_date = _parse_iso_datetime(to_param)
        if not to_date:
            return jsonify({"error": "Formato de fecha_hasta inválido. Usa YYYY-MM-DD."}), 400
        query = query.filter(MunicipioPost.fecha_publicacion <= to_date)
        applied_filters["to_date"] = to_param

    total = query.count()
    posts = (
        query.order_by(MunicipioPost.fecha_publicacion.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    payload = [post.to_dict() for post in posts]
    response = jsonify(payload)
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = "true" if offset + limit < total else "false"
    if applied_filters:
        response.headers["X-Applied-Filters"] = json.dumps(applied_filters)
    return response, 200

@municipal_bp.route('/posts', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def create_municipal_post(current_user):
    """
    Crea un nuevo post municipal (evento o noticia) y lo guarda en la base de datos.
    """
    if current_user.tipo_chat != "municipio":
        return jsonify({"error": "Acceso denegado. Se requiere un usuario municipal."}), 403

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify({"error": "No se pudo determinar el municipio asociado al usuario."}), 400

    db_municipio_id = get_numeric_municipio_id(municipio_id)
    if db_municipio_id is None:
        return jsonify({"error": "El identificador del municipio es inválido."}), 400

    # --- Recopilar datos del formulario ---
    titulo = request.form.get('titulo')
    subtitulo = request.form.get('subtitulo')
    contenido = request.form.get('contenido')
    tipo_post = request.form.get('tipo_post', 'noticia')  # 'noticia', 'evento' o 'informacion'
    imagen_url_externa = request.form.get('imagen_url', '')
    enlace = request.form.get('enlace') or request.form.get('url')
    fecha_evento_inicio = request.form.get('fecha_evento_inicio')
    fecha_evento_fin = request.form.get('fecha_evento_fin')
    ubicacion = request.form.get('ubicacion')

    if not all([titulo, contenido, tipo_post]):
        return jsonify({"error": "El título, el contenido y el tipo de post son requeridos."}), 400

    # --- Manejo del archivo de imagen (flyer) ---
    flyer_image_url = ''
    if 'flyer_image' in request.files:
        file = request.files['flyer_image']
        if file.filename != '':
            filename = secure_filename(file.filename)
            # Use persistent data directory when available
            from services.config_loader import BASE_DATA_PATH
            upload_folder = os.path.join(BASE_DATA_PATH, 'archivos')
            os.makedirs(upload_folder, exist_ok=True)
            file_path = os.path.join(upload_folder, filename)
            file.save(file_path)
            # Publicar el archivo a través del blueprint /media en lugar de la
            # ruta interna /data para que WhatsApp y los sitios públicos puedan
            # descargarlo correctamente.
            flyer_image_url = _normalize_public_media_url(f"/data/archivos/{filename}") or ''

    fecha_publicacion = request.form.get('fecha_publicacion')
    fecha_publicacion_dt = _parse_iso_datetime(fecha_publicacion) if fecha_publicacion else get_local_now()
    inicio_dt = _parse_iso_datetime(fecha_evento_inicio)
    fin_dt = _parse_iso_datetime(fecha_evento_fin)

    tags_raw = request.form.getlist('tags') or []
    datos_extra_raw = request.form.get('datos_extra')
    datos_extra: dict[str, Any] | None = None
    if datos_extra_raw:
        try:
            parsed_extra = json.loads(datos_extra_raw)
            if isinstance(parsed_extra, dict):
                datos_extra = parsed_extra
        except json.JSONDecodeError:
            current_app.logger.warning("datos_extra inválido, se ignora.")

    normalized_tipo = tipo_post.strip().lower() if tipo_post else "noticia"

    try:
        nuevo_post = MunicipioPost(
            municipio_id=db_municipio_id,
            titulo=titulo.strip(),
            subtitulo=subtitulo.strip() if subtitulo else None,
            descripcion=contenido,
            tipo_post=normalized_tipo,
            tags=tags_raw or [normalized_tipo],
            imagen_url=_normalize_public_media_url(flyer_image_url or imagen_url_externa),
            enlace=enlace,
            fecha_evento_inicio=inicio_dt,
            fecha_evento_fin=fin_dt,
            fecha_publicacion=fecha_publicacion_dt,
            ubicacion=ubicacion,
            datos_extra=datos_extra,
        )
        db.session.add(nuevo_post)
        db.session.flush()
        _prune_old_posts(db_municipio_id)
        db.session.commit()
        persisted = MunicipioPost.query.get(nuevo_post.id)
        if not persisted:
            return jsonify({
                "message": "El post se creó pero se archivó automáticamente por superar el límite de publicaciones recientes."
            }), 201

        tenant = TenantProfile.query.filter_by(municipio_id=db_municipio_id).first()
        if tenant:
            event_type = 'news_update' if normalized_tipo == 'noticia' else 'events_update'
            emit_tenant_update(tenant.slug, event_type, persisted.to_dict())

        return jsonify(persisted.to_dict()), 201
    except Exception as e:
        current_app.logger.error("Error al guardar el post municipal", exc_info=True)
        db.session.rollback()
        return jsonify({"error": "Error interno al guardar el post."}), 500


@municipal_bp.route('/posts/bulk', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def create_municipal_posts_bulk(current_user):
    """Crea múltiples posts municipales a partir de una lista de eventos."""
    if current_user.tipo_chat != "municipio":
        return jsonify({"error": "Acceso denegado. Se requiere un usuario municipal."}), 403

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify({"error": "No se pudo determinar el municipio asociado al usuario."}), 400

    db_municipio_id = get_numeric_municipio_id(municipio_id)
    if db_municipio_id is None:
        return jsonify({"error": "El identificador del municipio es inválido."}), 400

    raw_payload = request.get_json(silent=True)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    events = payload.get("events")
    tipo_post = payload.get("tipo_post") or request.form.get("tipo_post") or "evento"
    text = None

    if events is None:
        text = payload.get("text") or (raw_payload if isinstance(raw_payload, str) else None) or request.form.get("text")
        body_text = request.get_data(as_text=True).strip()
        if events is None and not text and body_text:
            try:
                maybe_json = json.loads(body_text)
                if isinstance(maybe_json, dict):
                    events = maybe_json.get("events")
                    if text is None:
                        text = maybe_json.get("text")
                elif isinstance(maybe_json, list):
                    events = maybe_json
                else:
                    text = body_text
            except json.JSONDecodeError:
                text = body_text
        if events is None and text:
            from utils.agenda_parser import parse_agenda_text
            events = parse_agenda_text(text)

    if events is None and "file" in request.files:
        # Parse agenda from uploaded file (.txt or .docx)
        file = request.files["file"]
        from utils.agenda_parser import parse_agenda_text
        if file.filename.lower().endswith(".docx"):
            from docx import Document
            document = Document(file)
            text = "\n".join(p.text for p in document.paragraphs)
        else:
            text = file.read().decode("utf-8")
        events = parse_agenda_text(text)

    if not isinstance(events, list):
        return jsonify({"error": "Se requiere un JSON con la lista 'events' o un campo 'text' o archivo 'file'."}), 400

    base_time = get_local_now()
    normalized_tipo_default = (tipo_post or "evento").strip().lower()

    created_posts: list[MunicipioPost] = []

    try:
        for index, ev in enumerate(events):
            title = (ev.get("title") or ev.get("titulo") or "").strip()
            if not title:
                continue

            day = (ev.get("day") or ev.get("dia") or "").strip() or None
            descripcion = (ev.get("description") or ev.get("descripcion") or "").strip()
            location = (ev.get("location") or ev.get("ubicacion") or "").strip() or None
            image_url = (ev.get("imagen_url") or ev.get("imagen") or "").strip() or None
            image_url = _normalize_public_media_url(image_url)
            enlace = (ev.get("enlace") or ev.get("url") or "").strip() or None
            tipo_evento = (ev.get("tipo_post") or normalized_tipo_default).strip().lower()

            if not descripcion:
                descripcion = title

            fecha_inicio_val = (
                ev.get("fecha_evento_inicio")
                or ev.get("fecha_inicio")
                or ev.get("inicio")
            )
            fecha_fin_val = (
                ev.get("fecha_evento_fin")
                or ev.get("fecha_fin")
                or ev.get("fin")
            )
            inicio_dt = _parse_iso_datetime(fecha_inicio_val)
            fin_dt = _parse_iso_datetime(fecha_fin_val)

            hora_val = (ev.get("time") or ev.get("hora") or "").strip()
            subtitulo = day
            if not subtitulo and hora_val:
                subtitulo = hora_val
            elif subtitulo and hora_val:
                subtitulo = f"{subtitulo} {hora_val}".strip()

            tags_value = []
            raw_tags = ev.get("tags")
            if isinstance(raw_tags, list):
                tags_value = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
            if tipo_evento not in tags_value:
                tags_value.insert(0, tipo_evento)

            publication_dt = base_time + timedelta(milliseconds=index)

            datos_extra = {}
            for key, value in ev.items():
                if key in {"title", "titulo", "description", "descripcion", "location", "ubicacion", "enlace", "url", "imagen", "imagen_url", "fecha_evento_inicio", "fecha_inicio", "inicio", "fecha_evento_fin", "fecha_fin", "fin", "tipo_post", "tags"}:
                    continue
                datos_extra[key] = value

            post = MunicipioPost(
                municipio_id=db_municipio_id,
                titulo=title,
                subtitulo=subtitulo,
                descripcion=descripcion,
                tipo_post=tipo_evento,
                tags=tags_value,
                imagen_url=image_url,
                enlace=enlace,
                fecha_evento_inicio=inicio_dt,
                fecha_evento_fin=fin_dt,
                fecha_publicacion=publication_dt,
                ubicacion=location,
                datos_extra=datos_extra or None,
            )
            db.session.add(post)
            created_posts.append(post)

        if not created_posts:
            return jsonify({"error": "No se encontraron eventos válidos para crear."}), 400

        db.session.flush()
        created_ids = [post.id for post in created_posts]
        _prune_old_posts(db_municipio_id)
        db.session.commit()

        persisted_posts = (
            MunicipioPost.query.filter(MunicipioPost.id.in_(created_ids)).all()
            if created_ids
            else []
        )
        persisted_map = {post.id: post for post in persisted_posts}
        payload = [persisted_map[pid].to_dict() for pid in created_ids if pid in persisted_map]

        tenant = TenantProfile.query.filter_by(municipio_id=db_municipio_id).first()
        if tenant:
            event_type = 'news_update' if normalized_tipo_default == 'noticia' else 'events_update'
            emit_tenant_update(tenant.slug, event_type, payload)

        return jsonify({"created": payload}), 201

    except Exception:
        current_app.logger.exception("Error al guardar los posts municipales en lote")
        db.session.rollback()
        return jsonify({"error": "Error interno al guardar los posts."}), 500


@municipal_bp.route('/analytics', methods=['GET', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_analytics(current_user):
    """Alias de ``/metrics`` para compatibilidad con el frontend."""

    if request.method == 'OPTIONS':
        return "", 204

    municipio_id = _resolve_current_municipio_id(current_user)
    filters, fecha_inicio, fecha_fin = _build_stats_filters_from_request(request.args)

    if filters:
        stats = build_stats_for_municipio(municipio_id, filters=filters, actor=current_user)
    else:
        stats = build_stats_for_municipio(municipio_id, actor=current_user)

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    metrics_raw = _municipal_message_metrics(
        eid, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin
    )

    if isinstance(metrics_raw, dict):
        metrics_cards = list(metrics_raw.get("cards", []) or [])
        metrics_summary = dict(metrics_raw.get("summary", {}) or {})
        metrics_payload = metrics_raw
    else:
        metrics_cards = list(metrics_raw or [])
        metrics_summary = {}
        metrics_payload = {"cards": metrics_cards, "summary": metrics_summary}

    response = {
        "stats": stats,
        "metrics": metrics_payload,
        "cards": metrics_cards,
        "summary": metrics_summary,
    }

    return jsonify(response)


@municipal_bp.route('/analytics/export/<string:formato>', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_analytics_export(current_user, formato: str):
    """Exporta estadísticas y métricas combinadas en PDF o Excel."""

    municipio_id = _resolve_current_municipio_id(current_user)
    if municipio_id is None:
        return jsonify({"error": "El usuario no posee un municipio asociado."}), 404

    filters, fecha_inicio, fecha_fin = _build_stats_filters_from_request(request.args)

    if filters:
        stats = build_stats_for_municipio(municipio_id, filters=filters, actor=current_user)
    else:
        stats = build_stats_for_municipio(municipio_id, actor=current_user)

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    metrics_raw = _municipal_message_metrics(
        eid, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin
    )

    if isinstance(metrics_raw, dict):
        metrics_payload = metrics_raw
    else:
        metrics_payload = {"cards": list(metrics_raw or []), "summary": {}}

    filtros_legibles = _format_filters_for_export(filters, fecha_inicio, fecha_fin)
    formato_normalizado = (formato or "").strip().lower()
    timestamp = get_local_now().strftime("%Y%m%d_%H%M%S")

    try:
        if formato_normalizado == "excel":
            contenido = _build_stats_excel(stats, filtros_legibles, metrics_payload)
            nombre = f"analiticas_municipio_{municipio_id}_{timestamp}.xlsx"
            return send_file(
                BytesIO(contenido),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=nombre,
            )
        if formato_normalizado == "pdf":
            contenido = _build_stats_pdf(stats, filtros_legibles, metrics_payload)
            nombre = f"analiticas_municipio_{municipio_id}_{timestamp}.pdf"
            return send_file(
                BytesIO(contenido),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=nombre,
            )
    except RuntimeError as err:
        current_app.logger.error("Error generando exportación de analíticas: %s", err)
        return jsonify({"error": str(err)}), 500
    except Exception as err:  # pragma: no cover - protección adicional
        current_app.logger.error(
            "Falla inesperada al exportar analíticas municipales", exc_info=True
        )
        return jsonify({"error": "No se pudo generar la exportación solicitada."}), 500

    return jsonify({"error": "Formato no soportado"}), 400
