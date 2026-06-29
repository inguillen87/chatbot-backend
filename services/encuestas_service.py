"""Business logic for survey creation, publishing and response handling."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import unicodedata
from collections import Counter
from datetime import datetime, timezone, timedelta
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote_plus

from flask import current_app, g
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func, or_, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, load_only

from config import TIMEZONE_OFFSET as _CONFIG_TIMEZONE_OFFSET
from database import db
from utils.db_utils import ensure_enc_encuesta_schema
from models import (
    EncEncuesta,
    EncPregunta,
    EncOpcion,
    EncRespuesta,
    EncRespuestaDetalle,
    EncLink,
    EncSegmento,
    EncComentario,
    TenantProfile,
    User,
)
from services.rewards import recompensas_service
try:
    from socket_service import emit_survey_update, emit_survey_comment
except ImportError:
    # Fallback to avoid circular import if running in restricted context,
    # though typical usage is safe.
    emit_survey_update = None
    emit_survey_comment = None

try:
    from services.analytics.ingestor import analytics_ingestor
except Exception:  # pragma: no cover - analytics optional in some contexts
    analytics_ingestor = None


_BOOTSTRAP_TENANT_ID: Optional[int] = None
_ENC_COMENTARIO_HAS_REPORT_COUNT: Optional[bool] = None


def _public_schedule_now() -> datetime:
    """Return now in the timezone used by public survey windows.

    SQLite strips timezone data from DateTime columns in tests and local demos.
    Storing survey windows in the same local timezone that models use for
    naive values keeps newly published surveys immediately active.
    """

    try:
        offset_hours = int(current_app.config.get("TIMEZONE_OFFSET", _CONFIG_TIMEZONE_OFFSET))
    except (RuntimeError, TypeError, ValueError):
        offset_hours = int(_CONFIG_TIMEZONE_OFFSET)
    offset_hours = max(-12, min(14, offset_hours))
    return datetime.now(timezone(timedelta(hours=offset_hours)))


def _social_comment_serializer() -> URLSafeTimedSerializer:
    secret = (
        current_app.config.get("SURVEY_SOCIAL_TOKEN_SECRET")
        or current_app.config.get("SECRET_KEY")
        or "chatboc-social-comment-secret"
    )
    salt = current_app.config.get("SURVEY_SOCIAL_TOKEN_SALT", "survey-social-comment")
    return URLSafeTimedSerializer(secret_key=secret, salt=salt)


def issue_social_comment_token(claims: Dict[str, Any]) -> str:
    payload = {
        "provider": str(claims.get("provider") or "").strip().lower(),
        "auth_user_id": str(claims.get("auth_user_id") or "").strip(),
        "auth_email": str(claims.get("auth_email") or "").strip() or None,
        "auth_first_name": str(claims.get("auth_first_name") or "").strip() or None,
        "auth_last_name": str(claims.get("auth_last_name") or "").strip() or None,
    }
    return _social_comment_serializer().dumps(payload)


def verify_social_comment_token(token: str, *, max_age_seconds: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if not token:
        return None

    ttl = max_age_seconds
    if ttl is None:
        raw_ttl = current_app.config.get("SURVEY_SOCIAL_TOKEN_TTL_SECONDS", 900)
        try:
            ttl = int(raw_ttl or 900)
        except (TypeError, ValueError):
            ttl = 900

    try:
        decoded = _social_comment_serializer().loads(token, max_age=max(60, ttl))
        return decoded if isinstance(decoded, dict) else None
    except SignatureExpired:
        return None
    except BadSignature:
        return None


def _enc_comentario_has_report_count() -> bool:
    """Return whether DB schema includes enc_comentario.report_count.

    Some deployments may run app code before the migration lands. Keep
    public survey comments endpoint resilient in that window.
    """

    global _ENC_COMENTARIO_HAS_REPORT_COUNT
    if _ENC_COMENTARIO_HAS_REPORT_COUNT is not None:
        return _ENC_COMENTARIO_HAS_REPORT_COUNT

    try:
        inspector = inspect(db.engine)
        columns = {col.get("name") for col in inspector.get_columns("enc_comentario")}
        _ENC_COMENTARIO_HAS_REPORT_COUNT = "report_count" in columns
    except Exception:
        _ENC_COMENTARIO_HAS_REPORT_COUNT = True

    return bool(_ENC_COMENTARIO_HAS_REPORT_COUNT)


class EncuestaError(Exception):
    """Base exception for survey service errors."""

    def __init__(self, message: str, status_code: int = 400, payload: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}

    def to_dict(self) -> Dict[str, Any]:
        data = {"error": self.message}
        data.update(self.payload)
        return data


def _env_flag(name: str, default: bool = True) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    return normalized not in {"0", "false", "no", "off", "disabled"}


def _parse_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_text_value(value: Any, *, fallback: str = "") -> str:
    """Normalize potentially nested/structured values into a safe string.

    Frontend components should never receive dict/list objects as direct React
    children. This helper keeps comments payloads render-safe.
    """

    if value is None:
        return fallback
    if isinstance(value, str):
        text = value.strip()
        return text or fallback
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        preferred_keys = ("texto", "text", "label", "nombre", "value", "pregunta", "lider")
        for key in preferred_keys:
            candidate = _safe_text_value(value.get(key), fallback="")
            if candidate:
                return candidate
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return fallback
    if isinstance(value, (list, tuple, set)):
        parts = [_safe_text_value(item, fallback="") for item in value]
        parts = [part for part in parts if part]
        return " · ".join(parts) if parts else fallback

    return _safe_text_value(str(value), fallback=fallback)


def _current_app_logger():
    try:
        return current_app.logger
    except RuntimeError:
        return None


_BOOTSTRAP_SAMPLE_ENABLED = _env_flag("ENCUESTAS_BOOTSTRAP_SAMPLE", default=False)


_AUTO_SEED_SEGMENT_KEY = "auto_seed_demo"
_AUTO_SEED_DEFAULT_LABEL = "Emular 100 respuestas demo"


_BOOTSTRAP_CONFIG_ENV_VAR = "ENCUESTAS_BOOTSTRAP_CONFIG_PATH"
_BOOTSTRAP_CONFIG_DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "encuestas_bootstrap"
    / "templates.json"
)


def _demo_seed_runtime_allowed() -> bool:
    try:
        if bool(current_app.config.get("ENABLE_DEMO_MODE", False)):
            return True
        if bool(current_app.config.get("ALLOW_SURVEY_DEMO_SEEDING", False)):
            return True
    except RuntimeError:
        pass
    return _env_flag("ALLOW_SURVEY_DEMO_SEEDING", default=False)


def _bootstrap_config_path() -> Path:
    env_override = os.getenv(_BOOTSTRAP_CONFIG_ENV_VAR)
    if env_override:
        return Path(env_override)

    try:
        config_override = current_app.config.get(_BOOTSTRAP_CONFIG_ENV_VAR)  # type: ignore[attr-defined]
    except RuntimeError:
        config_override = None

    if config_override:
        return Path(config_override)

    return _BOOTSTRAP_CONFIG_DEFAULT_PATH


@lru_cache(maxsize=1)
def _load_bootstrap_data(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        _log = _current_app_logger()
        if _log:
            _log.warning(
                "[encuestas] No se encontró el archivo de plantillas demo en %s", path
            )
        return {"templates": [], "profiles": []}
    except json.JSONDecodeError:
        _log = _current_app_logger()
        if _log:
            _log.exception(
                "[encuestas] Error al parsear el archivo de plantillas demo %s", path
            )
        return {"templates": [], "profiles": []}

    if not isinstance(data, dict):
        return {"templates": [], "profiles": []}

    data.setdefault("templates", [])
    data.setdefault("profiles", [])
    data.setdefault("geo_catalog", {})
    return data


def _get_bootstrap_data() -> Dict[str, Any]:
    path = _bootstrap_config_path()
    return _load_bootstrap_data(str(path))


def _bootstrap_templates() -> Sequence[Dict[str, Any]]:
    templates = _get_bootstrap_data().get("templates", [])
    if not isinstance(templates, list):
        return ()
    return tuple(templates)


def _geo_catalog() -> Dict[str, Any]:
    catalog = _get_bootstrap_data().get("geo_catalog", {})
    if isinstance(catalog, dict):
        return catalog
    return {}


def _resolve_geo_metadata(
    municipality: Optional[str] = None,
    profile_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    catalog = _geo_catalog()
    candidates: List[str] = []
    if profile_key:
        candidates.append(_slugify(profile_key))
    if municipality:
        candidates.append(_slugify(municipality))
    for candidate in candidates:
        entry = catalog.get(candidate)
        if isinstance(entry, dict):
            return entry
    # Fallback: return first catalog entry if available.
    if catalog:
        first_key = next(iter(catalog))
        entry = catalog.get(first_key)
        if isinstance(entry, dict):
            return entry
    return None


def _build_location_question(
    geo_metadata: Dict[str, Any],
    municipality: str,
) -> Optional[Dict[str, Any]]:
    barrios = list(geo_metadata.get("neighborhoods") or [])
    distritos = list(geo_metadata.get("districts") or [])
    options_labels: List[str] = []
    seen: set[str] = set()

    for label in distritos + barrios:
        normalized = (label or "").strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        options_labels.append(normalized)

    if not options_labels:
        return None

    opciones: List[Dict[str, Any]] = [
        {"orden": idx + 1, "texto": label}
        for idx, label in enumerate(options_labels)
    ]

    otros_label = geo_metadata.get("otros_label") or "Otros (especificar ubicación)"
    opciones.append(
        {
            "orden": len(opciones) + 1,
            "texto": str(otros_label),
            "valor": "geo_autocomplete",
        }
    )

    question_text = (
        "¿En qué distrito o barrio de {municipio} residís?"
    ).format(municipio=municipality)

    return {
        "orden": 1,
        "tipo": "opcion_unica",
        "texto": question_text,
        "obligatoria": True,
        "opciones": opciones,
    }


def _augment_payload_with_geo(
    payload: Dict[str, Any],
    geo_metadata: Optional[Dict[str, Any]],
    municipality: str,
) -> Dict[str, Any]:
    if not geo_metadata:
        return payload

    preguntas = list(payload.get("preguntas") or [])
    has_geo_option = any(
        any((opcion.get("valor") == "geo_autocomplete") for opcion in pregunta.get("opciones", []))
        for pregunta in preguntas
    )

    if not has_geo_option:
        location_question = _build_location_question(geo_metadata, municipality)
        if location_question:
            preguntas = [location_question] + preguntas

    for index, pregunta in enumerate(preguntas, start=1):
        pregunta["orden"] = index
    payload["preguntas"] = preguntas

    geo_payload = {
        "center": geo_metadata.get("center"),
        "bounds": geo_metadata.get("bounds"),
        "coordinates": geo_metadata.get("coordinates"),
        "districts": geo_metadata.get("districts"),
        "neighborhoods": geo_metadata.get("neighborhoods"),
    }
    payload.setdefault("metadata", {})["geo"] = geo_payload
    return payload


def _pick_geo_point(
    geo_metadata: Optional[Dict[str, Any]],
    rng: random.Random,
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not geo_metadata:
        return None, None, None

    coordinates = list(geo_metadata.get("coordinates") or [])
    if coordinates:
        sample = rng.choice(coordinates)
        return sample.get("lat"), sample.get("lng"), sample.get("barrio")

    bounds = geo_metadata.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        west, south, east, north = bounds
        lat = rng.uniform(min(south, north), max(south, north))
        lng = rng.uniform(min(west, east), max(west, east))
        return lat, lng, None

    center = geo_metadata.get("center")
    if isinstance(center, (list, tuple)) and len(center) == 2:
        lat = center[0] + rng.uniform(-0.01, 0.01)
        lng = center[1] + rng.uniform(-0.01, 0.01)
        return lat, lng, None

    return None, None, None


def _build_bootstrap_payloads(
    municipality: str,
    inicio: datetime,
    fin: datetime,
    templates: Optional[Sequence[Dict[str, Any]]] = None,
    geo_metadata: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    source_templates = templates or _bootstrap_templates()
    municipality_slug = _slugify(municipality)
    geo_info = geo_metadata or _resolve_geo_metadata(municipality)
    payloads: List[Dict[str, Any]] = []
    for template in source_templates:
        template_copy = deepcopy(template)
        payload: Dict[str, Any] = {
            "titulo": _render_municipality_placeholder(
                template_copy.get("titulo", ""), municipality
            ),
            "slug": f"{template_copy.get('slug', 'encuesta')}-{municipality_slug}",
            "descripcion": _render_municipality_placeholder(
                template_copy.get("descripcion", ""), municipality
            ),
            "tipo": template_copy.get("tipo", "opinion"),
            "anonimo_permitido": bool(template_copy.get("anonimato", True)),
            "requiere_identidad": bool(
                template_copy.get("requiere_datos_contacto", False)
            ),
            "es_votacion_envivo": bool(template_copy.get("es_votacion_envivo", False)),
            "mostrar_resultados_envivo": bool(
                template_copy.get("mostrar_resultados_envivo", False)
            ),
            "permitir_comentarios": bool(template_copy.get("permitir_comentarios", False)),
            "politica_unicidad": template_copy.get("politica_unicidad", "libre"),
            "tags": list(template_copy.get("tags") or []),
            "inicio_at": inicio.isoformat(),
            "fin_at": fin.isoformat(),
        }

        auto_seed_demo = template_copy.get("auto_seed_demo")
        if isinstance(auto_seed_demo, dict):
            payload["auto_seed_demo"] = deepcopy(auto_seed_demo)

        preguntas: List[Dict[str, Any]] = []
        for pregunta_tpl in template_copy.get("preguntas", []):
            pregunta_tipo = pregunta_tpl.get("tipo", "opcion_unica")
            if pregunta_tipo == "multiple":
                pregunta_tipo = "opcion_multiple"
            pregunta: Dict[str, Any] = {
                "orden": int(pregunta_tpl.get("orden", len(preguntas) + 1)),
                "tipo": pregunta_tipo,
                "texto": _render_municipality_placeholder(
                    pregunta_tpl.get("texto", ""), municipality
                ),
                "obligatoria": bool(pregunta_tpl.get("obligatoria", False)),
            }
            if "min_selecciones" in pregunta_tpl:
                pregunta["min_selecciones"] = pregunta_tpl["min_selecciones"]
            if "max_selecciones" in pregunta_tpl:
                pregunta["max_selecciones"] = pregunta_tpl["max_selecciones"]

            if pregunta_tipo in {"opcion_unica", "opcion_multiple"}:
                opciones_payload = pregunta_tpl.get("opciones") or []
                opciones: List[Dict[str, Any]] = []
                for opcion_tpl in opciones_payload:
                    opcion: Dict[str, Any] = {
                        "orden": int(opcion_tpl.get("orden", len(opciones) + 1)),
                        "texto": _render_municipality_placeholder(
                            opcion_tpl.get("texto", ""), municipality
                        ),
                    }
                    if "valor" in opcion_tpl:
                        opcion["valor"] = opcion_tpl["valor"]
                    opciones.append(opcion)
                pregunta["opciones"] = opciones
            preguntas.append(pregunta)

        payload["preguntas"] = preguntas
        payload = _augment_payload_with_geo(payload, geo_info, municipality)
        payloads.append(payload)

    return payloads


def _render_municipality_placeholder(value: Any, municipality: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{municipality}}", municipality)
    return value


def _build_junin_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Junín", inicio, fin)


def _build_san_martin_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("San Martín", inicio, fin)


def _build_rivadavia_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Rivadavia", inicio, fin)


def _build_mendoza_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Mendoza", inicio, fin)


def _build_godoy_cruz_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Godoy Cruz", inicio, fin)


def _find_template_definition(slug: str) -> Optional[Dict[str, Any]]:
    if not slug:
        return None
    for template in _bootstrap_templates():
        if template.get("slug") == slug:
            return deepcopy(template)
    return None


def list_template_catalog(
    municipality: Optional[str] = None,
    template_slugs: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    templates = _bootstrap_templates()
    if template_slugs:
        templates = _select_templates_by_slugs(templates, template_slugs)

    rendered: List[Dict[str, Any]] = []
    geo_profile_key = _slugify(municipality) if municipality else None

    for template in templates:
        template_copy = deepcopy(template)
        slug = template_copy.get("slug")

        def _render(value: Any) -> Any:
            return _render_municipality_placeholder(value, municipality) if municipality else value

        preguntas_rendered: List[Dict[str, Any]] = []
        for pregunta in template_copy.get("preguntas", []):
            opciones_rendered: List[Dict[str, Any]] = []
            for opcion in pregunta.get("opciones", []):
                opciones_rendered.append(
                    {
                        "orden": opcion.get("orden"),
                        "texto": _render(opcion.get("texto")),
                        "texto_template": opcion.get("texto"),
                        "valor": opcion.get("valor"),
                    }
                )

            preguntas_rendered.append(
                {
                    "orden": pregunta.get("orden"),
                    "tipo": pregunta.get("tipo", "opcion_unica"),
                    "texto": _render(pregunta.get("texto")),
                    "texto_template": pregunta.get("texto"),
                    "obligatoria": bool(pregunta.get("obligatoria", False)),
                    "min_selecciones": pregunta.get("min_selecciones"),
                    "max_selecciones": pregunta.get("max_selecciones"),
                    "opciones": opciones_rendered,
                }
            )

        demo_seed = {
            "label": "Emular 100 respuestas demo",
            "cantidad": 100,
            "geo_profile_key": geo_profile_key,
            "municipality_label": municipality,
        }

        rendered.append(
            {
                "slug": slug,
                "titulo": _render(template_copy.get("titulo")),
                "titulo_template": template_copy.get("titulo"),
                "descripcion": _render(template_copy.get("descripcion")),
                "descripcion_template": template_copy.get("descripcion"),
                "tipo": template_copy.get("tipo", "opinion"),
                "politica_unicidad": template_copy.get("politica_unicidad", "libre"),
                "anonimato": bool(template_copy.get("anonimato", True)),
                "requiere_datos_contacto": bool(template_copy.get("requiere_datos_contacto", False)),
                "es_votacion_envivo": bool(template_copy.get("es_votacion_envivo", False)),
                "mostrar_resultados_envivo": bool(
                    template_copy.get("mostrar_resultados_envivo", False)
                ),
                "permitir_comentarios": bool(template_copy.get("permitir_comentarios", False)),
                "tags": list(template_copy.get("tags") or []),
                "preguntas": preguntas_rendered,
                "demo_seed": demo_seed,
                "quick_actions": [
                    {
                        "key": "demo_seed",
                        "label": demo_seed["label"],
                        "cantidad": demo_seed["cantidad"],
                        "geo_profile_key": demo_seed["geo_profile_key"],
                        "municipality_label": demo_seed["municipality_label"],
                    }
                ],
            }
        )

    return rendered


def build_template_draft_from_slug(
    slug: str,
    municipality: Optional[str],
    *,
    start: Optional[Any] = None,
    end: Optional[Any] = None,
) -> Dict[str, Any]:
    template = _find_template_definition(slug)
    if not template:
        raise EncuestaError("Plantilla no encontrada", status_code=404)

    if not municipality:
        raise EncuestaError("La localidad es requerida para generar la plantilla", status_code=400)

    def _ensure_datetime(value: Optional[Any]) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            return _parse_datetime(value)
        return None

    inicio = _ensure_datetime(start) or datetime.now(timezone.utc)
    fin = _ensure_datetime(end)
    if fin is None:
        fin = inicio + timedelta(days=30)

    payloads = _build_bootstrap_payloads(municipality, inicio, fin, templates=[template])
    if not payloads:
        raise EncuestaError("No se pudo generar la plantilla solicitada", status_code=500)

    payload = payloads[0]
    geo_profile_key = _slugify(municipality)
    auto_seed_demo = {
        "enabled": True,
        "cantidad": 100,
        "label": "Emular 100 respuestas demo",
        "geo_profile_key": geo_profile_key,
        "municipality_label": municipality,
    }
    requiere_identidad = bool(payload.get("requiere_identidad", False))
    anonimato_habilitado = bool(payload.get("anonimo_permitido", True)) and not requiere_identidad

    preguntas = []
    for pregunta in payload.get("preguntas", []):
        preguntas.append(
            {
                "orden": pregunta.get("orden"),
                "tipo": "multiple" if pregunta.get("tipo") == "opcion_multiple" else pregunta.get("tipo"),
                "texto": pregunta.get("texto"),
                "obligatoria": bool(pregunta.get("obligatoria", False)),
                "min_selecciones": pregunta.get("min_selecciones"),
                "max_selecciones": pregunta.get("max_selecciones"),
                "opciones": [
                    {
                        "orden": opcion.get("orden"),
                        "texto": opcion.get("texto"),
                        "valor": opcion.get("valor"),
                    }
                    for opcion in pregunta.get("opciones", [])
                ],
            }
        )

    return {
        "slug": payload.get("slug"),
        "municipality": municipality,
        "titulo": payload.get("titulo"),
        "descripcion": payload.get("descripcion"),
        "tipo": payload.get("tipo", "opinion"),
        "inicio_at": payload.get("inicio_at"),
        "fin_at": payload.get("fin_at"),
        "politica_unicidad": payload.get("politica_unicidad", "libre"),
        "anonimato": anonimato_habilitado,
        "requiere_datos_contacto": requiere_identidad,
        "tags": payload.get("tags") or [],
        "preguntas": preguntas,
        "auto_seed_demo": auto_seed_demo,
        "quick_actions": [
            {
                "key": "demo_seed",
                "label": auto_seed_demo["label"],
                "cantidad": auto_seed_demo["cantidad"],
                "geo_profile_key": auto_seed_demo["geo_profile_key"],
                "municipality_label": auto_seed_demo["municipality_label"],
            }
        ],
    }


def _select_templates_by_slugs(
    templates: Sequence[Dict[str, Any]],
    template_slugs: Sequence[str],
) -> List[Dict[str, Any]]:
    slug_set = {slug for slug in template_slugs if slug}
    if not slug_set:
        return list(templates)
    return [template for template in templates if template.get("slug") in slug_set]


def _make_profile_builder(
    municipality: str, template_slugs: Optional[Sequence[str]] = None
) -> Callable[[datetime, datetime], List[Dict[str, Any]]]:
    def _builder(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
        templates = _bootstrap_templates()
        if template_slugs:
            payload_templates = _select_templates_by_slugs(templates, template_slugs)
        else:
            payload_templates = list(templates)
        return _build_bootstrap_payloads(
            municipality,
            inicio,
            fin,
            templates=payload_templates,
        )

    return _builder


def _load_bootstrap_profiles() -> List[Dict[str, Any]]:
    data = _get_bootstrap_data()
    profiles: List[Dict[str, Any]] = []

    default_builders = {
        "junin": _build_junin_bootstrap_payload,
        "san_martin": _build_san_martin_bootstrap_payload,
        "rivadavia": _build_rivadavia_bootstrap_payload,
        "mendoza": _build_mendoza_bootstrap_payload,
        "godoy_cruz": _build_godoy_cruz_bootstrap_payload,
    }

    for raw_profile in data.get("profiles", []):
        if not isinstance(raw_profile, dict):
            continue

        key = raw_profile.get("key")
        municipality = raw_profile.get("municipality")
        if not key and municipality:
            key = _slugify(municipality)
        if not key:
            continue

        template_slugs = raw_profile.get("template_slugs") or []

        builder = None
        if template_slugs and municipality:
            builder = _make_profile_builder(municipality, template_slugs)
        elif template_slugs:
            builder = _make_profile_builder(key, template_slugs)
        elif key in default_builders:
            builder = default_builders[key]
        elif municipality:
            builder = _make_profile_builder(municipality)

        if builder is None:
            fallback_label = municipality or key
            builder = _make_profile_builder(fallback_label)

        profile: Dict[str, Any] = {
            "key": key,
            "tenant_env": raw_profile.get("tenant_env"),
            "fallback_tenant_id": raw_profile.get("fallback_tenant_id"),
            "keywords": tuple(raw_profile.get("keywords", [])),
            "template_slugs": tuple(template_slugs),
            "payload_builder": builder,
            "auto_publish": bool(raw_profile.get("auto_publish", True)),
            "tenant_id": raw_profile.get("tenant_id"),
            "municipality_label": municipality or key.replace("_", " ") if key else None,
            "geo_key": raw_profile.get("geo_key") or key,
        }

        profiles.append(profile)

    if not profiles:
        profiles = [
            {
                "key": "junin",
                "tenant_env": "JUNIN_ENCUESTAS_TENANT_ID",
                "fallback_tenant_id": 4,
                "keywords": ("junin",),
                "payload_builder": _build_junin_bootstrap_payload,
                "auto_publish": True,
                "tenant_id": None,
                "municipality_label": "Junín",
                "geo_key": "junin",
            }
        ]

    return profiles

_BOOTSTRAP_PROFILES: List[Dict[str, Any]] = _load_bootstrap_profiles()


def _bootstrap_skip_registry() -> set:
    """Return the in-memory registry of tenants where bootstrap must be skipped."""

    skip_registry = current_app.config.setdefault("ENCUESTAS_BOOTSTRAP_SKIP_TENANTS", set())
    return skip_registry

_PUBLIC_SLUG_ALIAS_RE = re.compile(r"^(?P<base>.+)-(?P<token>[0-9a-f]{6,})$")


def _slugify(value: str, fallback: Optional[str] = None) -> str:
    text = (value or "").strip().lower()
    if not text and fallback:
        text = fallback
    normalized = unicodedata.normalize("NFKD", text)
    cleaned = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    cleaned = cleaned.replace("/", " ")
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in cleaned)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    cleaned = cleaned.strip("-")
    return cleaned or (fallback or secrets.token_hex(6))


def _slug_exists(slug: str) -> bool:
    if not slug:
        return False
    return (
        db.session.query(EncEncuesta.id)
        .filter(EncEncuesta.slug == slug)
        .first()
        is not None
    )


def _generate_unique_slug(initial_slug: str) -> str:
    """Return a slug that is unique in ``EncEncuesta`` by appending a counter."""

    slug = initial_slug or secrets.token_hex(6)
    if not _slug_exists(slug):
        return slug

    match = re.match(r"^(?P<stem>.+?)(?:-(?P<num>\d+))?$", slug)
    stem = match.group("stem") if match else slug
    counter = int(match.group("num")) + 1 if match and match.group("num") else 2

    while True:
        candidate = f"{stem}-{counter}"
        if not _slug_exists(candidate):
            return candidate
        counter += 1


def _determine_tenant_id(user: Any) -> int:
    tenant_profile = getattr(g, "tenant_profile", None)
    if tenant_profile and getattr(user, "rol", None) == "super_admin":
        return tenant_profile.id

    tenant_candidate = (
        getattr(user, "tenant_id", None)
        or getattr(user, "municipio_id", None)
        or getattr(user, "empresa_id", None)
        or getattr(user, "pyme_id", None)
        or getattr(user, "id", None)
    )
    if not tenant_candidate:
        raise EncuestaError("No se pudo determinar el tenant del usuario", status_code=403)
    return int(tenant_candidate)


def determine_tenant_id_for_user(user: Any) -> int:
    """Public helper used by routes to resolve tenant consistently.

    Keeping this indirection avoids route-level drift where list/create endpoints
    accidentally resolve different tenant IDs for the same authenticated user.
    """

    return _determine_tenant_id(user)


def _ensure_tenant_access(encuesta: EncEncuesta, user: Any) -> None:
    tenant_id = _determine_tenant_id(user)
    if encuesta.tenant_id == tenant_id:
        return

    tenant_profile = getattr(g, "tenant_profile", None)
    if tenant_profile and int(getattr(tenant_profile, "id", 0) or 0) == int(encuesta.tenant_id):
        return

    raise EncuestaError("No tenés permiso para esta encuesta", status_code=403)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EncuestaError(f"Fecha inválida: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_pregunta_tipo(raw_tipo: Any) -> str:
    if raw_tipo is None:
        return "opcion_unica"

    text = str(raw_tipo).strip().lower()
    if not text:
        return "opcion_unica"

    mapping = {
        "multiple": "opcion_multiple",
        "opcion multiple": "opcion_multiple",
        "opcion_multiple": "opcion_multiple",
        "multiple_choice": "opcion_multiple",
        "multiple-choice": "opcion_multiple",
        "multiplechoice": "opcion_multiple",
        "multi_choice": "opcion_multiple",
        "multi-choice": "opcion_multiple",
        "multichoice": "opcion_multiple",
        "multi_select": "opcion_multiple",
        "multi-select": "opcion_multiple",
        "multiselect": "opcion_multiple",
        "checkbox": "opcion_multiple",
        "check": "opcion_multiple",
        "multiple answers": "opcion_multiple",
        "multi": "opcion_multiple",
        "single": "opcion_unica",
        "single_choice": "opcion_unica",
        "single-choice": "opcion_unica",
        "singlechoice": "opcion_unica",
        "radio": "opcion_unica",
        "opcion_unica": "opcion_unica",
        "texto": "abierta",
        "text": "abierta",
        "open": "abierta",
        "open_text": "abierta",
        "open-text": "abierta",
        "rating_emoji": "rating_emoji",
        "emoji": "rating_emoji",
    }

    return mapping.get(text, text)


def _map_pregunta_tipo_for_response(tipo: Optional[str]) -> Tuple[str, Optional[str]]:
    """Return a frontend-friendly question type along with the original value."""

    mapping = {
        "opcion_unica": "single_choice",
        "opcion_multiple": "multiple_choice",
        "abierta": "text",
    }

    original = tipo
    external = mapping.get(tipo, tipo)
    return external, original


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _normalize_option_entries(opciones: Sequence[Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for opt in opciones or []:
        if isinstance(opt, str):
            texto = opt.strip()
            if texto:
                normalized.append({"texto": texto, "orden": len(normalized) + 1})
            continue

        if not isinstance(opt, dict):
            continue

        texto = (
            opt.get("texto")
            or opt.get("label")
            or opt.get("nombre")
            or opt.get("title")
            or opt.get("value")
        )
        texto_limpio = str(texto).strip() if texto is not None else ""
        if not texto_limpio:
            continue

        opcion = dict(opt)
        opcion["texto"] = texto_limpio
        orden = _coerce_int_or_none(opcion.get("orden"))
        if orden is None:
            orden = len(normalized) + 1
        opcion["orden"] = orden
        normalized.append(opcion)

    return normalized


def _validate_pregunta_payload(pregunta: Dict[str, Any], index: int) -> Dict[str, Any]:
    payload = dict(pregunta)
    missing: List[str] = []

    raw_tipo = payload.get("tipo") or payload.get("type")
    if not raw_tipo:
        missing.append("tipo")
    else:
        payload["tipo"] = _normalize_pregunta_tipo(raw_tipo)

    texto = (
        payload.get("texto")
        or payload.get("titulo")
        or payload.get("title")
        or payload.get("pregunta")
    )
    if not texto:
        missing.append("texto")
    else:
        payload["texto"] = texto

    if missing:
        raise EncuestaError(
            f"Pregunta #{index + 1} incompleta: falta {', '.join(missing)}"
        )

    if payload.get("orden") is None:
        payload["orden"] = index + 1
    else:
        try:
            payload["orden"] = int(payload.get("orden"))
        except (TypeError, ValueError):
            payload["orden"] = index + 1

    if "obligatoria" not in payload and "required" in payload:
        payload["obligatoria"] = bool(payload.get("required"))

    if "opciones" not in payload and payload.get("options") is not None:
        payload["opciones"] = payload.get("options") or []

    if "min_selecciones" not in payload and payload.get("minSeleccion") is not None:
        payload["min_selecciones"] = payload.get("minSeleccion")
    if "max_selecciones" not in payload and payload.get("maxSeleccion") is not None:
        payload["max_selecciones"] = payload.get("maxSeleccion")

    tipo = _normalize_pregunta_tipo(payload.get("tipo"))
    payload["tipo"] = tipo

    payload["min_selecciones"] = _coerce_int_or_none(
        payload.get("min_selecciones")
    )
    payload["max_selecciones"] = _coerce_int_or_none(
        payload.get("max_selecciones")
    )

    opciones = _normalize_option_entries(payload.get("opciones") or [])
    payload["opciones"] = opciones
    if tipo in {"opcion_unica", "opcion_multiple"} and not opciones:
        raise EncuestaError(f"Pregunta #{index + 1} requiere opciones")

    return payload


def _apply_common_updates(encuesta: EncEncuesta, data: Dict[str, Any]) -> None:
    encuesta.titulo = data.get("titulo", encuesta.titulo)
    encuesta.descripcion = data.get("descripcion", encuesta.descripcion)
    encuesta.tipo = data.get("tipo", encuesta.tipo)
    encuesta.inicio_at = _parse_datetime(data.get("inicio_at")) or encuesta.inicio_at
    encuesta.fin_at = _parse_datetime(data.get("fin_at")) or encuesta.fin_at
    if "puntos_recompensa" in data:
        encuesta.puntos_recompensa = _coerce_int_or_none(data.get("puntos_recompensa"))
    if "requiere_identidad" in data:
        encuesta.requiere_identidad = bool(data["requiere_identidad"])
    if "politica_unicidad" in data and data["politica_unicidad"]:
        encuesta.politica_unicidad = data["politica_unicidad"]
    if "anonimo_permitido" in data:
        encuesta.anonimo_permitido = bool(data["anonimo_permitido"])
    if "es_votacion_envivo" in data:
        encuesta.es_votacion_envivo = bool(data["es_votacion_envivo"])
    if "mostrar_resultados_envivo" in data:
        encuesta.mostrar_resultados_envivo = bool(data["mostrar_resultados_envivo"])
    if "permitir_comentarios" in data:
        encuesta.permitir_comentarios = bool(data["permitir_comentarios"])
    if "tags" in data:
        _sync_encuesta_tags(encuesta, data.get("tags"))


def _build_pregunta_entities(encuesta: EncEncuesta, preguntas_payload: Sequence[Dict[str, Any]]) -> List[EncPregunta]:
    preguntas: List[EncPregunta] = []
    for idx, pregunta_payload in enumerate(preguntas_payload):
        payload = _validate_pregunta_payload(pregunta_payload, idx)
        pregunta_tipo = _normalize_pregunta_tipo(payload.get("tipo", "opcion_unica"))
        pregunta = EncPregunta(
            encuesta=encuesta,
            orden=int(payload.get("orden", idx + 1)),
            tipo=pregunta_tipo,
            texto=(payload.get("texto") or "").strip(),
            obligatoria=bool(payload.get("obligatoria", False)),
            min_selecciones=_coerce_int_or_none(payload.get("min_selecciones")),
            max_selecciones=_coerce_int_or_none(payload.get("max_selecciones")),
        )
        opciones_payload = payload.get("opciones") or []
        for opt in opciones_payload:
            texto_opcion = (
                opt.get("texto")
                or opt.get("label")
                or opt.get("nombre")
                or ""
            ).strip()
            if not texto_opcion:
                raise EncuestaError(
                    f"Pregunta #{idx + 1} contiene una opción sin texto"
                )
            opcion = EncOpcion(
                pregunta=pregunta,
                orden=int(opt.get("orden", len(pregunta.opciones) + 1)),
                texto=texto_opcion,
                valor=opt.get("valor") or opt.get("value"),
            )
            pregunta.opciones.append(opcion)
        preguntas.append(pregunta)
    return preguntas


def _payload_int_id(payload: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = payload.get(key)
        coerced = _coerce_int_or_none(value)
        if coerced is not None:
            return coerced
    return None


def _apply_non_destructive_question_updates(
    encuesta: EncEncuesta,
    preguntas_payload: Sequence[Dict[str, Any]],
) -> None:
    """Apply text/config edits without invalidating existing answers.

    Public surveys with responses can receive full-form PUT payloads from the
    admin UI. Rebuilding the questions would break historical answer
    references, but rejecting every payload with ``preguntas`` makes normal
    saves fail. This path allows same-shape updates and rejects only structural
    mutations: new/deleted questions, type changes, or new/deleted options.
    """

    existing_questions = {pregunta.id: pregunta for pregunta in encuesta.preguntas if pregunta.id is not None}
    normalized_payloads: List[Dict[str, Any]] = []
    seen_questions: set[int] = set()

    for idx, raw_payload in enumerate(preguntas_payload or []):
        payload = _validate_pregunta_payload(dict(raw_payload or {}), idx)
        question_id = _payload_int_id(payload, "id", "pregunta_id", "question_id")
        if question_id is None or question_id not in existing_questions:
            raise EncuestaError(
                "No se puede modificar la estructura de una encuesta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "La pregunta no existe en la encuesta publicada.",
                    "encuesta_id": encuesta.id,
                },
            )
        if question_id in seen_questions:
            raise EncuestaError(
                "No se puede modificar la estructura de una encuesta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "La misma pregunta aparece mas de una vez en el payload.",
                    "encuesta_id": encuesta.id,
                },
            )
        seen_questions.add(question_id)

        question = existing_questions[question_id]
        if _normalize_pregunta_tipo(payload.get("tipo")) != question.tipo:
            raise EncuestaError(
                "No se puede modificar el tipo de una pregunta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "El tipo de pregunta no puede cambiar despues de recibir respuestas.",
                    "encuesta_id": encuesta.id,
                    "pregunta_id": question_id,
                },
            )

        existing_options = {opcion.id: opcion for opcion in question.opciones if opcion.id is not None}
        incoming_options = list(payload.get("opciones") or [])
        if existing_options or incoming_options:
            if len(incoming_options) != len(existing_options):
                raise EncuestaError(
                    "No se puede modificar las opciones de una encuesta con respuestas registradas",
                    status_code=409,
                    payload={
                        "reason_code": "survey_structure_locked",
                        "detail": "No se pueden agregar o quitar opciones despues de recibir respuestas.",
                        "encuesta_id": encuesta.id,
                        "pregunta_id": question_id,
                    },
                )
            seen_options: set[int] = set()
            for option_payload in incoming_options:
                option_id = _payload_int_id(option_payload, "id", "opcion_id", "option_id")
                if option_id is None or option_id not in existing_options:
                    raise EncuestaError(
                        "No se puede modificar las opciones de una encuesta con respuestas registradas",
                        status_code=409,
                        payload={
                            "reason_code": "survey_structure_locked",
                            "detail": "La opcion no existe en la encuesta publicada.",
                            "encuesta_id": encuesta.id,
                            "pregunta_id": question_id,
                        },
                    )
                if option_id in seen_options:
                    raise EncuestaError(
                        "No se puede modificar las opciones de una encuesta con respuestas registradas",
                        status_code=409,
                        payload={
                            "reason_code": "survey_structure_locked",
                            "detail": "La misma opcion aparece mas de una vez en el payload.",
                            "encuesta_id": encuesta.id,
                            "pregunta_id": question_id,
                        },
                    )
                seen_options.add(option_id)

        normalized_payloads.append(payload)

    if set(existing_questions) != seen_questions:
        raise EncuestaError(
            "No se puede modificar la estructura de una encuesta con respuestas registradas",
            status_code=409,
            payload={
                "reason_code": "survey_structure_locked",
                "detail": "El payload debe conservar todas las preguntas existentes.",
                "encuesta_id": encuesta.id,
                "missing_question_ids": sorted(set(existing_questions) - seen_questions),
            },
        )

    for payload in normalized_payloads:
        question_id = _payload_int_id(payload, "id", "pregunta_id", "question_id")
        if question_id is None:
            continue
        question = existing_questions[question_id]
        question.texto = (payload.get("texto") or "").strip()
        question.obligatoria = bool(payload.get("obligatoria", False))
        question.min_selecciones = _coerce_int_or_none(payload.get("min_selecciones"))
        question.max_selecciones = _coerce_int_or_none(payload.get("max_selecciones"))

        existing_options = {opcion.id: opcion for opcion in question.opciones if opcion.id is not None}
        for option_payload in payload.get("opciones") or []:
            option_id = _payload_int_id(option_payload, "id", "opcion_id", "option_id")
            option = existing_options.get(option_id)
            if option is None:
                continue
            option.texto = (
                option_payload.get("texto")
                or option_payload.get("label")
                or option_payload.get("nombre")
                or ""
            ).strip()
            option.valor = option_payload.get("valor") or option_payload.get("value")


def _normalize_tags(tags: Optional[Sequence[Any]]) -> List[str]:
    if not tags:
        return []
    normalized: List[str] = []
    seen: set = set()
    for raw in tags:
        cleaned = _clean_str(raw, max_length=120)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized


def _sync_encuesta_tags(encuesta: EncEncuesta, tags: Optional[Sequence[Any]]) -> None:
    normalized = _normalize_tags(tags)
    target_keys = {tag.lower() for tag in normalized}
    existing: List[EncSegmento] = [seg for seg in encuesta.segmentos if seg.clave == "tag"]
    existing_lookup = {str(seg.valor or "").lower(): seg for seg in existing}

    # Remove segmentos that are no longer present.
    for seg in list(existing):
        value_key = str(seg.valor or "").lower()
        if value_key not in target_keys:
            encuesta.segmentos.remove(seg)

    for tag in normalized:
        key = tag.lower()
        existing_segment = existing_lookup.get(key)
        if existing_segment and existing_segment in encuesta.segmentos:
            if (existing_segment.valor or "") != tag:
                existing_segment.valor = tag
            continue
        encuesta.segmentos.append(EncSegmento(encuesta=encuesta, clave="tag", valor=tag))


def _guess_auto_seed_defaults(
    *,
    municipality_label: Optional[str],
    slug_hint: Optional[str],
    tenant_id: Optional[int],
) -> Tuple[Optional[str], Optional[str]]:
    geo_key: Optional[str] = None
    municipality_value: Optional[str] = (municipality_label or None)

    catalog = _geo_catalog()
    if slug_hint:
        parts = [part for part in slug_hint.split("-") if part]
        for part in reversed(parts):
            if part.isdigit():
                continue
            entry = catalog.get(part)
            if entry:
                geo_key = part
                if not municipality_value:
                    municipality_value = (
                        entry.get("municipality")
                        or entry.get("label")
                        or entry.get("name")
                    )
                break

    if geo_key is None and tenant_id is not None:
        profile = _match_bootstrap_profile(tenant_id)
        if profile:
            geo_key = profile.get("geo_key") or profile.get("key") or geo_key
            if not municipality_value:
                municipality_value = profile.get("municipality_label")

    if geo_key is None and municipality_value:
        normalized = _slugify(str(municipality_value))
        if normalized:
            geo_key = normalized

    return geo_key, municipality_value


def _normalize_auto_seed_config(
    raw_cfg: Optional[Dict[str, Any]],
    *,
    municipality_label: Optional[str],
    slug_hint: Optional[str],
    tenant_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    config = raw_cfg if isinstance(raw_cfg, dict) else None
    default_geo, default_municipality = _guess_auto_seed_defaults(
        municipality_label=municipality_label,
        slug_hint=slug_hint,
        tenant_id=tenant_id,
    )

    if config is None:
        return None

    enabled = bool(config.get("enabled", True))
    cantidad_raw = (
        config.get("cantidad")
        or config.get("count")
        or config.get("responses")
    )
    try:
        cantidad = int(cantidad_raw) if cantidad_raw is not None else 100
    except (TypeError, ValueError):
        cantidad = 100
    if cantidad < 0:
        cantidad = 0

    municipality_value = (
        config.get("municipality_label")
        or config.get("municipality")
        or config.get("city")
        or default_municipality
    )
    geo_key = (
        config.get("geo_profile_key")
        or config.get("geo_key")
        or config.get("geo")
        or default_geo
    )
    if not geo_key and municipality_value:
        geo_key = _slugify(str(municipality_value))

    normalized = {
        "enabled": enabled,
        "cantidad": cantidad,
        "geo_profile_key": geo_key,
        "municipality_label": municipality_value,
        "label": config.get("label") or _AUTO_SEED_DEFAULT_LABEL,
    }

    if not enabled or cantidad <= 0:
        return None

    return normalized


def _persist_auto_seed_config(
    encuesta: EncEncuesta, config: Optional[Dict[str, Any]]
) -> None:
    existing = [
        segmento
        for segmento in encuesta.segmentos
        if segmento.clave == _AUTO_SEED_SEGMENT_KEY
    ]
    for segmento in existing:
        encuesta.segmentos.remove(segmento)

    if not config:
        return

    serialized = json.dumps(config, ensure_ascii=False)
    encuesta.segmentos.append(
        EncSegmento(
            encuesta=encuesta,
            clave=_AUTO_SEED_SEGMENT_KEY,
            valor=serialized,
        )
    )


def _get_auto_seed_config(encuesta: EncEncuesta) -> Optional[Dict[str, Any]]:
    for segmento in encuesta.segmentos:
        if segmento.clave != _AUTO_SEED_SEGMENT_KEY:
            continue
        try:
            raw_value = json.loads(segmento.valor or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_value, dict):
            continue

        enabled = bool(raw_value.get("enabled", True))
        cantidad_raw = (
            raw_value.get("cantidad")
            or raw_value.get("count")
            or raw_value.get("responses")
        )
        try:
            cantidad = int(cantidad_raw) if cantidad_raw is not None else 100
        except (TypeError, ValueError):
            cantidad = 100

        if not enabled or cantidad <= 0:
            return None

        config = dict(raw_value)
        config["enabled"] = True
        config["cantidad"] = cantidad
        config.setdefault("label", _AUTO_SEED_DEFAULT_LABEL)
        return config

    return None


def create_encuesta(data: Dict[str, Any], user: Any) -> EncEncuesta:
    if not data:
        raise EncuestaError("Payload vacío")

    payload = deepcopy(data)
    raw_auto_seed_cfg = payload.pop("auto_seed_demo", None)
    payload.pop("quick_actions", None)
    municipality_hint = payload.get("municipality") or payload.get("municipio")

    tenant_id = _determine_tenant_id(user)
    titulo = (payload.get("titulo") or "").strip()
    if not titulo:
        raise EncuestaError("El título es requerido")

    slug_seed = payload.get("slug") or f"{tenant_id}-{titulo}"
    slug = _generate_unique_slug(_slugify(slug_seed))

    auto_seed_cfg = _normalize_auto_seed_config(
        raw_auto_seed_cfg,
        municipality_label=municipality_hint,
        slug_hint=slug_seed,
        tenant_id=tenant_id,
    )

    encuesta = EncEncuesta(
        tenant_id=tenant_id,
        slug=slug,
        titulo=titulo,
        descripcion=payload.get("descripcion"),
        tipo=payload.get("tipo", "opinion"),
        estado="borrador",
        inicio_at=_parse_datetime(payload.get("inicio_at")),
        fin_at=_parse_datetime(payload.get("fin_at")),
        requiere_identidad=bool(payload.get("requiere_identidad", False)),
        politica_unicidad=payload.get("politica_unicidad", "libre"),
        anonimo_permitido=bool(payload.get("anonimo_permitido", True)),
        es_votacion_envivo=bool(payload.get("es_votacion_envivo", False)),
        mostrar_resultados_envivo=bool(payload.get("mostrar_resultados_envivo", False)),
        permitir_comentarios=bool(payload.get("permitir_comentarios", False)),
        created_by=getattr(user, "id", None),
        puntos_recompensa=_coerce_int_or_none(payload.get("puntos_recompensa")),
    )

    preguntas_payload = payload.get("preguntas") or []
    encuesta.preguntas = _build_pregunta_entities(encuesta, preguntas_payload)
    _sync_encuesta_tags(encuesta, payload.get("tags"))
    _persist_auto_seed_config(encuesta, auto_seed_cfg)

    db.session.add(encuesta)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo crear la encuesta (slug duplicado?)") from exc

    current_app.logger.info("[encuestas] Encuesta %s creada por %s", encuesta.id, getattr(user, "id", None))

    if auto_seed_cfg:
        seed_encuesta_respuestas_demo(
            encuesta.id,
            user,
            cantidad=auto_seed_cfg.get("cantidad", 100),
            geo_profile_key=auto_seed_cfg.get("geo_profile_key"),
            municipality_label=auto_seed_cfg.get("municipality_label"),
        )

    return encuesta


def update_encuesta(encuesta_id: int, data: Dict[str, Any], user: Any) -> EncEncuesta:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(encuesta, user)

    if encuesta.estado == "cerrada":
        raise EncuestaError(
            "La encuesta está cerrada y no se puede modificar",
            status_code=409,
        )

    if encuesta.estado not in {"borrador", "publicada"}:
        raise EncuestaError("La encuesta no se puede modificar", status_code=409)

    puede_actualizar_estructura = True
    municipality_hint = data.get("municipality") or data.get("municipio")
    has_auto_seed_update = "auto_seed_demo" in data
    raw_auto_seed_cfg = data.get("auto_seed_demo") if has_auto_seed_update else None

    raw_slug = None
    if "slug" in data:
        raw_slug = data.get("slug")
    elif "codigo" in data:
        raw_slug = data.get("codigo")

    if raw_slug is not None:
        candidate_slug = _slugify(str(raw_slug))
        if not candidate_slug:
            raise EncuestaError("El slug no puede quedar vacío", status_code=400)
        if candidate_slug != encuesta.slug:
            exists = (
                db.session.query(EncEncuesta.id)
                .filter(EncEncuesta.slug == candidate_slug, EncEncuesta.id != encuesta.id)
                .first()
            )
            if exists:
                raise EncuestaError("Ya existe una encuesta con ese slug", status_code=409)
            encuesta.slug = candidate_slug

    if encuesta.estado == "publicada" and "preguntas" in data:
        puede_actualizar_estructura = encuesta.respuestas.count() == 0

    _apply_common_updates(encuesta, data)

    if "preguntas" in data:
        if not puede_actualizar_estructura:
            _apply_non_destructive_question_updates(encuesta, data.get("preguntas") or [])
        else:
            encuesta.preguntas.clear()
            db.session.flush()
            nuevas_preguntas = _build_pregunta_entities(encuesta, data.get("preguntas") or [])
            encuesta.preguntas.extend(nuevas_preguntas)

    if has_auto_seed_update:
        auto_seed_cfg = _normalize_auto_seed_config(
            raw_auto_seed_cfg,
            municipality_label=municipality_hint,
            slug_hint=encuesta.slug,
            tenant_id=encuesta.tenant_id,
        )
        _persist_auto_seed_config(encuesta, auto_seed_cfg)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("Error al actualizar la encuesta") from exc

    current_app.logger.info("[encuestas] Encuesta %s actualizada por %s", encuesta.id, getattr(user, "id", None))
    return encuesta


def duplicate_encuesta(encuesta_id: int, data: Optional[Dict[str, Any]], user: Any) -> EncEncuesta:
    """Create an editable draft copy without touching the published source.

    Published surveys with responses can only receive non-destructive edits.
    When the admin needs to add/remove questions or options, the professional
    flow is to create a new draft version and keep historical answers anchored
    to the original survey.
    """

    source = db.session.get(EncEncuesta, encuesta_id)
    if not source:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(source, user)

    payload = data or {}
    requested_title = _clean_str(payload.get("titulo") or payload.get("title"), max_length=255)
    title = requested_title or f"{source.titulo} (nueva version)"
    requested_slug = payload.get("slug") or payload.get("codigo") or f"{source.slug}-nueva-version"
    slug = _generate_unique_slug(_slugify(str(requested_slug)))

    cloned = EncEncuesta(
        tenant_id=source.tenant_id,
        slug=slug,
        titulo=title,
        descripcion=source.descripcion,
        tipo=source.tipo,
        estado="borrador",
        puntos_recompensa=source.puntos_recompensa,
        inicio_at=None,
        fin_at=source.fin_at,
        requiere_identidad=source.requiere_identidad,
        politica_unicidad=source.politica_unicidad,
        anonimo_permitido=source.anonimo_permitido,
        es_votacion_envivo=source.es_votacion_envivo,
        mostrar_resultados_envivo=source.mostrar_resultados_envivo,
        permitir_comentarios=source.permitir_comentarios,
        created_by=getattr(user, "id", None),
    )

    for question in source.preguntas:
        cloned_question = EncPregunta(
            orden=question.orden,
            tipo=question.tipo,
            texto=question.texto,
            obligatoria=question.obligatoria,
            min_selecciones=question.min_selecciones,
            max_selecciones=question.max_selecciones,
        )
        for option in question.opciones:
            cloned_question.opciones.append(
                EncOpcion(
                    orden=option.orden,
                    texto=option.texto,
                    valor=option.valor,
                )
            )
        cloned.preguntas.append(cloned_question)

    _sync_encuesta_tags(cloned, _collect_encuesta_tags(source))
    db.session.add(cloned)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo duplicar la encuesta", status_code=409) from exc

    current_app.logger.info(
        "[encuestas] Encuesta %s duplicada como %s por %s",
        source.id,
        cloned.id,
        getattr(user, "id", None),
    )
    return cloned


def _ensure_publication_window(encuesta: EncEncuesta) -> None:
    if encuesta.inicio_at and encuesta.fin_at and encuesta.inicio_at > encuesta.fin_at:
        raise EncuestaError("La fecha de inicio no puede ser posterior a la de cierre")


def publicar_encuesta(encuesta_id: int, user: Any) -> Tuple[EncEncuesta, EncLink]:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(encuesta, user)
    if encuesta.estado not in {"borrador", "publicada"}:
        raise EncuestaError("La encuesta no se puede publicar", status_code=409)
    if not encuesta.preguntas:
        raise EncuestaError("La encuesta debe tener preguntas para publicarse")

    _ensure_publication_window(encuesta)

    encuesta.estado = "publicada"
    if not encuesta.inicio_at:
        encuesta.inicio_at = _public_schedule_now()

    link = _resolve_current_public_link(encuesta)
    if link is None:
        link = EncLink(
            encuesta_id=encuesta.id,
            slug_publico=encuesta.slug,
            canal="web",
        )
        db.session.add(link)
    slug_publico = link.slug_publico

    auto_seed_cfg = _get_auto_seed_config(encuesta)
    auto_seed_params: Optional[Dict[str, Any]] = None
    if auto_seed_cfg and auto_seed_cfg.get("enabled", True):
        try:
            cantidad_int = int(auto_seed_cfg.get("cantidad", 0))
        except (TypeError, ValueError):
            cantidad_int = 0
        if cantidad_int > 0 and encuesta.respuestas.count() == 0:
            auto_seed_params = {
                "cantidad": cantidad_int,
                "geo_profile_key": auto_seed_cfg.get("geo_profile_key"),
                "municipality_label": auto_seed_cfg.get("municipality_label"),
            }

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo publicar la encuesta") from exc

    current_app.logger.info(
        "[encuestas] Encuesta %s publicada con slug %s por %s",
        encuesta.id,
        link.slug_publico,
        getattr(user, "id", None),
    )

    if auto_seed_params and _demo_seed_runtime_allowed():
        try:
            seed_encuesta_respuestas_demo(
                encuesta.id,
                user,
                cantidad=auto_seed_params["cantidad"],
                geo_profile_key=auto_seed_params.get("geo_profile_key"),
                municipality_label=auto_seed_params.get("municipality_label"),
            )
            current_app.logger.info(
                "[encuestas] Respuestas demo generadas automáticamente al publicar encuesta %s",
                encuesta.id,
            )
        except EncuestaError:
            current_app.logger.exception(
                "[encuestas] Error al generar respuestas demo para la encuesta %s tras publicarla",
                encuesta.id,
            )
    elif auto_seed_params:
        current_app.logger.info(
            "[encuestas] Auto seed demo omitido para encuesta %s: modo demo deshabilitado",
            encuesta.id,
        )

    return encuesta, link


def cerrar_encuesta(encuesta_id: int, user: Any) -> EncEncuesta:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(encuesta, user)
    encuesta.estado = "cerrada"
    encuesta.fin_at = encuesta.fin_at or _public_schedule_now()
    db.session.commit()
    current_app.logger.info("[encuestas] Encuesta %s cerrada por %s", encuesta.id, getattr(user, "id", None))
    return encuesta


def delete_encuesta(encuesta_id: int, user: Any) -> None:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    _ensure_tenant_access(encuesta, user)
    tenant_id = encuesta.tenant_id

    db.session.delete(encuesta)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo eliminar la encuesta") from exc

    _bootstrap_skip_registry().add(tenant_id)
    current_app.logger.info(
        "[encuestas] Encuesta %s eliminada por %s", encuesta_id, getattr(user, "id", None)
    )


def _resolve_profile_tenant_id(profile: Dict[str, Any]) -> Optional[int]:
    tenant_id = profile.get("tenant_id")
    if tenant_id:
        return int(tenant_id)

    env_name = profile.get("tenant_env")
    if env_name:
        env_value = _parse_int(os.getenv(env_name))
        if env_value:
            profile["tenant_id"] = env_value
            return env_value

    fallback = profile.get("fallback_tenant_id")
    if fallback:
        profile["tenant_id"] = fallback
        return fallback

    return None


def _get_bootstrap_user_for_profile(profile: Dict[str, Any], tenant_id: int) -> Optional[User]:
    candidate_tenant = _resolve_profile_tenant_id(profile) or tenant_id

    base_query = User.query.filter(User.tipo_chat == "municipio")
    if candidate_tenant:
        user = (
            base_query.filter(
                or_(
                    User.municipio_id == candidate_tenant,
                    User.id == candidate_tenant,
                )
            )
            .order_by(User.id.asc())
            .first()
        )
        if user:
            profile["tenant_id"] = _determine_tenant_id(user)
            return user

    keywords: Sequence[str] = profile.get("keywords") or ()
    if not keywords:
        return None

    like_filters = []
    for keyword in keywords:
        like = f"%{keyword}%"
        like_filters.extend(
            [
                User.nombre_empresa.ilike(like),
                User.name.ilike(like),
                User.email.ilike(like),
                User.ciudad.ilike(like),
            ]
        )

    keyword_query = User.query.filter(User.tipo_chat == "municipio")
    if tenant_id:
        keyword_query = keyword_query.filter(
            or_(User.municipio_id == tenant_id, User.id == tenant_id)
        )
    if like_filters:
        keyword_query = keyword_query.filter(or_(*like_filters))

    user = keyword_query.order_by(User.id.asc()).first()
    if user:
        profile["tenant_id"] = _determine_tenant_id(user)
        return user

    return None


def _match_bootstrap_profile(tenant_id: int) -> Optional[Dict[str, Any]]:
    for profile in _BOOTSTRAP_PROFILES:
        resolved = _resolve_profile_tenant_id(profile)
        if resolved is not None and resolved == tenant_id:
            return profile

    for profile in _BOOTSTRAP_PROFILES:
        if profile.get("tenant_id") is not None:
            continue
        user = _get_bootstrap_user_for_profile(profile, tenant_id)
        if user and _determine_tenant_id(user) == tenant_id:
            return profile
    return None


def _resolve_geo_metadata_for_tenant(tenant_id: int) -> Optional[Dict[str, Any]]:
    profile = _match_bootstrap_profile(tenant_id)
    if profile:
        return _resolve_geo_metadata(
            municipality=profile.get("municipality_label"),
            profile_key=profile.get("geo_key") or profile.get("key"),
        )
    return None


def _bootstrap_sample_if_needed(tenant_id: int) -> None:
    # Safety net if migrations lag: ensure the reward column exists to avoid 500s
    ensure_enc_encuesta_schema(db.session)

    if not (_BOOTSTRAP_SAMPLE_ENABLED or _demo_seed_runtime_allowed()):
        return

    profile = _match_bootstrap_profile(tenant_id)
    if not profile:
        return

    if tenant_id in _bootstrap_skip_registry():
        return

    existing = EncEncuesta.query.filter_by(tenant_id=tenant_id).count()
    if existing:
        return

    user = _get_bootstrap_user_for_profile(profile, tenant_id)
    if not user:
        current_app.logger.warning(
            "[encuestas] No se encontró un usuario municipal de %s para crear la encuesta demo",
            profile.get("key", "desconocido"),
        )
        _bootstrap_skip_registry().add(tenant_id)
        return

    inicio = datetime.now(timezone.utc)
    fin = inicio + timedelta(days=45)
    payload_builder: Callable[[datetime, datetime], Sequence[Dict[str, Any]]] = profile[
        "payload_builder"
    ]
    raw_payloads = payload_builder(inicio, fin)
    if isinstance(raw_payloads, dict):
        payloads = [raw_payloads]
    else:
        payloads = list(raw_payloads)

    created = 0
    for payload in payloads:
        try:
            encuesta = create_encuesta(payload, user)
        except EncuestaError:
            current_app.logger.exception(
                "[encuestas] No se pudo crear la encuesta demo de %s",
                profile.get("key"),
            )
            continue

        if profile.get("auto_publish", True):
            try:
                encuesta, link = publicar_encuesta(encuesta.id, user)
            except EncuestaError:
                current_app.logger.exception(
                    "[encuestas] No se pudo publicar la encuesta demo de %s",
                    profile.get("key"),
                )
                continue
            current_app.logger.info(
                "[encuestas] Encuesta demo de %s publicada automáticamente con slug %s",
                profile.get("key"),
                link.slug_publico,
            )
        else:
            current_app.logger.info(
                "[encuestas] Encuesta demo de %s creada automáticamente con id %s",
                profile.get("key"),
                encuesta.id,
            )
        created += 1

    if not created:
        _bootstrap_skip_registry().add(tenant_id)


def _prepare_template_payloads(
    raw_payloads: Sequence[Dict[str, Any]] | Dict[str, Any],
    municipio_label: str,
    geo_metadata: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if isinstance(raw_payloads, dict):
        payloads = [deepcopy(raw_payloads)]
    else:
        payloads = [deepcopy(item) for item in raw_payloads]

    if geo_metadata:
        for idx, payload in enumerate(payloads):
            payloads[idx] = _augment_payload_with_geo(payload, geo_metadata, municipio_label)

    return payloads


def _collect_all_template_payloads(
    inicio: datetime, fin: datetime
) -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()

    for profile in _BOOTSTRAP_PROFILES:
        key = profile.get("key")
        municipio_label = (
            profile.get("municipality_label")
            or (key.replace("_", " ") if isinstance(key, str) else None)
            or "Tu municipio"
        )
        builder: Callable[[datetime, datetime], Sequence[Dict[str, Any]]] = profile["payload_builder"]
        raw_payloads = builder(inicio, fin)
        geo_metadata = _resolve_geo_metadata(
            municipality=profile.get("municipality_label"),
            profile_key=profile.get("geo_key") or key,
        )
        payloads = _prepare_template_payloads(raw_payloads, municipio_label, geo_metadata)
        catalog.append(
            {
                "key": key,
                "municipality": municipio_label,
                "templates": payloads,
                "geo": geo_metadata,
            }
        )
        if key:
            seen_keys.add(key)

    if "generic" not in seen_keys:
        generic_geo = _resolve_geo_metadata(municipality="Tu municipio")
        generic_payloads = _prepare_template_payloads(
            _build_bootstrap_payloads("Tu municipio", inicio, fin, geo_metadata=generic_geo),
            "Tu municipio",
            generic_geo,
        )
        catalog.append(
            {
                "key": "generic",
                "municipality": "Tu municipio",
                "templates": generic_payloads,
                "geo": generic_geo,
            }
        )

    return catalog


def list_template_payloads(tenant_id: int, scope: Optional[str] = None) -> Dict[str, Any]:
    inicio = datetime.now(timezone.utc)
    fin = inicio + timedelta(days=45)
    profile = _match_bootstrap_profile(tenant_id)
    geo_metadata = _resolve_geo_metadata_for_tenant(tenant_id)

    if profile:
        builder: Callable[[datetime, datetime], Sequence[Dict[str, Any]]] = profile["payload_builder"]
        raw_payloads = builder(inicio, fin)
        municipio_label = profile.get("municipality_label") or profile.get("key") or "Tu municipio"
    else:
        municipio_label = "Tu municipio"
        raw_payloads = _build_bootstrap_payloads(
            municipio_label,
            inicio,
            fin,
            geo_metadata=geo_metadata,
        )

    payloads = _prepare_template_payloads(raw_payloads, municipio_label, geo_metadata)

    response: Dict[str, Any] = {
        "templates": payloads,
        "municipality": municipio_label,
        "geo": geo_metadata,
    }

    scope_key = (scope or "").strip().lower()
    if scope_key in {"all", "todos", "todas", "full"}:
        response["all_templates"] = _collect_all_template_payloads(inicio, fin)

    return response


def _resolve_public_slug(encuesta: EncEncuesta) -> Optional[str]:
    slug_publico = None
    for link in sorted(encuesta.links, key=lambda link: (link.id or 0), reverse=True):
        if link.slug_publico:
            slug_publico = link.slug_publico
            break

    if not slug_publico and encuesta.estado == "publicada":
        slug_publico = encuesta.slug

    return slug_publico


def _public_slug_matches_current_base(slug_publico: Optional[str], encuesta_slug: Optional[str]) -> bool:
    if not slug_publico or not encuesta_slug:
        return False
    normalized_public = str(slug_publico).strip().lower()
    normalized_base = str(encuesta_slug).strip().lower()
    return normalized_public == normalized_base or normalized_public.startswith(f"{normalized_base}-")


def _resolve_current_public_link(encuesta: EncEncuesta) -> Optional[EncLink]:
    """Return the canonical public link for the survey without creating churn.

    Older deployments created a new random public slug on every publish. The
    admin UI then kept showing an older slug while the publish endpoint returned
    a different one. Keep the latest link when it still belongs to the current
    base slug; otherwise prefer another matching link before creating a new one.
    """

    links = sorted(encuesta.links, key=lambda link: (link.id or 0), reverse=True)
    if not links:
        return None

    latest = next((link for link in links if link.slug_publico), None)
    if latest and _public_slug_matches_current_base(latest.slug_publico, encuesta.slug):
        return latest

    for link in links:
        if _public_slug_matches_current_base(link.slug_publico, encuesta.slug):
            return link

    return latest


def _public_url_for_slug(slug_publico: Optional[str]) -> Optional[str]:
    if not slug_publico:
        return None

    base_url: Optional[str] = None
    try:
        configured = (
            current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
            or current_app.config.get("PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL")
            or current_app.config.get("FRONTEND_URL")
            or current_app.config.get("PUBLIC_FRONTEND_URL")
        )
        if isinstance(configured, str) and configured.strip():
            base_url = configured.rstrip("/")
    except RuntimeError:
        base_url = None

    if not base_url:
        return f"/e/{slug_publico}"

    return f"{base_url}/e/{slug_publico}"


def _public_api_endpoint_for_slug(slug_publico: Optional[str]) -> Optional[str]:
    if not slug_publico:
        return None
    return f"/api/public/encuestas/v1/{slug_publico}"


def list_encuestas(tenant_id: int, estado: Optional[str] = None) -> List[EncEncuesta]:
    _bootstrap_sample_if_needed(tenant_id)
    query = (
        EncEncuesta.query.options(
            joinedload(EncEncuesta.links),
            joinedload(EncEncuesta.segmentos),
        )
        .filter_by(tenant_id=tenant_id)
    )
    if estado:
        query = query.filter_by(estado=estado)
    return query.order_by(EncEncuesta.created_at.desc()).all()


def _public_encuestas_list_options() -> List[Any]:
    options: List[Any] = [joinedload(EncEncuesta.links)]
    try:
        options.append(
            load_only(
                EncEncuesta.id,
                EncEncuesta.tenant_id,
                EncEncuesta.slug,
                EncEncuesta.titulo,
                EncEncuesta.descripcion,
                EncEncuesta.tipo,
                EncEncuesta.estado,
                EncEncuesta.inicio_at,
                EncEncuesta.fin_at,
                EncEncuesta.es_votacion_envivo,
                EncEncuesta.mostrar_resultados_envivo,
                EncEncuesta.permitir_comentarios,
                EncEncuesta.created_at,
                EncEncuesta.updated_at,
            )
        )
    except (AttributeError, TypeError):
        pass
    return [option for option in options if option is not None]


def list_public_encuestas_for_tenant(
    tenant_id: int,
    limit: int = 10,
    offset: int = 0,
) -> List[Tuple[EncEncuesta, str]]:
    """Return active public surveys for a tenant along with their public slugs."""

    try:
        requested_limit = int(limit or 10)
    except (TypeError, ValueError):
        requested_limit = 10
    safe_limit = requested_limit if requested_limit > 0 else 10
    safe_limit = min(safe_limit, 25)
    try:
        requested_offset = int(offset or 0)
    except (TypeError, ValueError):
        requested_offset = 0
    safe_offset = max(0, requested_offset)
    wanted_count = safe_limit + safe_offset
    candidate_limit = max(wanted_count * 4, 25)
    query = (
        EncEncuesta.query.options(*_public_encuestas_list_options())
        .filter(EncEncuesta.tenant_id == tenant_id)
        .filter(EncEncuesta.estado == "publicada")
        .order_by(
            EncEncuesta.created_at.desc(),
            EncEncuesta.id.desc(),
        )
        .limit(candidate_limit)
    )

    encuestas = query.all()
    resultados: List[Tuple[EncEncuesta, str]] = []

    for encuesta in encuestas:
        if not encuesta.esta_activa():
            continue
        slug_publico = _resolve_public_slug(encuesta)
        if not slug_publico:
            continue
        resultados.append((encuesta, slug_publico))
        if len(resultados) >= wanted_count:
            break

    return resultados[safe_offset : safe_offset + safe_limit]


def get_encuesta(encuesta_id: int, tenant_id: Optional[int] = None, user: Any = None) -> EncEncuesta:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    if tenant_id and encuesta.tenant_id != tenant_id:
        raise EncuestaError("Encuesta fuera del tenant", status_code=403)
    if user is not None:
        _ensure_tenant_access(encuesta, user)
    return encuesta






def _is_bootstrap_demo_survey(encuesta: EncEncuesta) -> bool:
    """Return True when the survey can be identified as bootstrap demo content."""

    if not encuesta:
        return False

    templates = _bootstrap_templates()
    if not templates:
        return False

    titulo = (getattr(encuesta, "titulo", "") or "").strip().lower()
    descripcion = (getattr(encuesta, "descripcion", "") or "").strip().lower()

    for template in templates:
        if not isinstance(template, dict):
            continue
        template_title = str(template.get("titulo") or "").strip().lower()
        template_desc = str(template.get("descripcion") or "").strip().lower()

        if template_title and titulo == template_title:
            return True
        if template_title and template_title in titulo:
            return True
        if template_desc and descripcion and template_desc == descripcion:
            return True

    return False

def _ensure_demo_public_window(encuesta: EncEncuesta) -> None:
    """Keep bootstrap demo surveys publicly accessible when their window expired."""

    if not encuesta or encuesta.estado != "publicada":
        return

    profile = _match_bootstrap_profile(getattr(encuesta, "tenant_id", None) or 0)
    if not profile:
        return
    if not _is_bootstrap_demo_survey(encuesta):
        return

    now = _public_schedule_now()
    local_tz = now.tzinfo or timezone.utc
    inicio_at = encuesta.inicio_at
    if inicio_at and inicio_at.tzinfo is None:
        inicio_at = inicio_at.replace(tzinfo=local_tz)
    elif inicio_at:
        inicio_at = inicio_at.astimezone(local_tz)

    fin_at = encuesta.fin_at
    if fin_at and fin_at.tzinfo is None:
        fin_at = fin_at.replace(tzinfo=local_tz)
    elif fin_at:
        fin_at = fin_at.astimezone(local_tz)

    should_update = False
    if inicio_at and inicio_at > now:
        encuesta.inicio_at = now - timedelta(minutes=5)
        should_update = True

    if fin_at and fin_at < now:
        encuesta.fin_at = now + timedelta(days=365)
        should_update = True

    if should_update:
        try:
            db.session.add(encuesta)
            db.session.commit()
            current_app.logger.info(
                "[encuestas] Refreshed public window for bootstrap demo survey %s", encuesta.id
            )
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "[encuestas] Failed to refresh public window for bootstrap demo survey %s",
                getattr(encuesta, "id", None),
            )

def get_public_encuesta(
    slug_publico: str,
    *,
    allow_inactive_for_user: Optional[Any] = None,
    preferred_tenant_id: Optional[int] = None,
) -> EncEncuesta:
    normalized_slug = (slug_publico or "").strip().lower()
    if not normalized_slug:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    preferred_tenant: Optional[int] = None
    if preferred_tenant_id is not None:
        try:
            preferred_tenant = int(preferred_tenant_id)
        except (TypeError, ValueError):
            preferred_tenant = None

    def _pick_best_candidate(items: Sequence[Optional[EncEncuesta]]) -> Optional[EncEncuesta]:
        """Prefer currently active public surveys when multiple rows share a slug."""
        normalized_items = [candidate for candidate in items if candidate is not None]
        if not normalized_items:
            return None

        tenant_filtered = normalized_items
        if preferred_tenant is not None:
            matches = [
                candidate for candidate in normalized_items if int(candidate.tenant_id or 0) == preferred_tenant
            ]
            if matches:
                tenant_filtered = matches

        fallback: Optional[EncEncuesta] = tenant_filtered[0] if tenant_filtered else None
        for candidate in tenant_filtered:
            if candidate.estado == "publicada" and candidate.esta_activa():
                return candidate
        return fallback

    encuesta: Optional[EncEncuesta] = None
    matching_links = (
        EncLink.query.filter(func.lower(EncLink.slug_publico) == normalized_slug)
        .order_by(EncLink.id.desc())
        .all()
    )
    if matching_links:
        encuesta = _pick_best_candidate([link.encuesta for link in matching_links])
    else:
        slug_matches = (
            EncEncuesta.query.filter(func.lower(EncEncuesta.slug) == normalized_slug)
            .order_by(EncEncuesta.id.desc())
            .all()
        )
        encuesta = _pick_best_candidate(slug_matches)
        if encuesta is None:
            alias_match = _PUBLIC_SLUG_ALIAS_RE.match(normalized_slug)
            if alias_match:
                base_slug = alias_match.group("base")
                base_slug_matches = (
                    EncEncuesta.query.filter(func.lower(EncEncuesta.slug) == base_slug)
                    .order_by(EncEncuesta.id.desc())
                    .all()
                )
                encuesta = _pick_best_candidate(base_slug_matches)

    if encuesta is None and re.fullmatch(r"[0-9a-z]{5,12}", normalized_slug):
        short_link = (
            EncLink.query.filter(
                EncLink.slug_publico.ilike(f"%-{normalized_slug}")
            )
            .order_by(EncLink.id.desc())
            .first()
        )
        if short_link:
            encuesta = short_link.encuesta

    if encuesta is None:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    preview_user = allow_inactive_for_user
    if preview_user is not None:
        try:
            _ensure_tenant_access(encuesta, preview_user)
        except EncuestaError:
            preview_user = None
        else:
            return encuesta

    if encuesta.estado != "publicada":
        raise EncuestaError(
            "La encuesta no está activa",
            status_code=403,
            payload={"reason_code": "survey_not_published"},
        )

    if current_app.config.get("ENABLE_DEMO_MODE"):
        _ensure_demo_public_window(encuesta)
    if not encuesta.esta_activa():
        raise EncuestaError(
            "La encuesta no está en su ventana de participación",
            status_code=403,
            payload={"reason_code": "survey_outside_active_window"},
        )
    return encuesta


def build_unique_fingerprint(
    encuesta: EncEncuesta,
    tenant_id: int,
    dni: Optional[str] = None,
    phone: Optional[str] = None,
    ip: Optional[str] = None,
    anon_cookie: Optional[str] = None,
) -> Optional[str]:
    policy = (encuesta.politica_unicidad or "libre").lower()
    if policy == "libre":
        return None

    def _clean_identifier(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
        else:
            cleaned = str(value).strip()
        return cleaned or None

    dni_clean = _clean_identifier(dni)
    phone_clean = _clean_identifier(phone)
    cookie_clean = _clean_identifier(anon_cookie)
    ip_clean = _clean_identifier(ip)

    source_parts: List[str] = [
        f"encuesta:{encuesta.id}",
        f"tenant:{tenant_id}",
        f"policy:{policy}",
    ]

    appended = False

    def _append(tag: str, value: Optional[str]):
        nonlocal appended
        if value is None:
            return
        source_parts.append(f"{tag}:{value}")
        appended = True

    if policy in {"por_dni", "dni"}:
        _append("dni", dni_clean)
    elif policy in {"por_phone", "phone"}:
        _append("phone", phone_clean)
    elif policy in {"por_cookie", "cookie"}:
        _append("cookie", cookie_clean)
    elif policy in {"por_ip", "ip"}:
        _append("ip", ip_clean)
    elif policy in {"por_dni_o_phone", "dni_o_phone"}:
        _append("dni", dni_clean)
        _append("phone", phone_clean)
    else:
        # fallback usa todo lo disponible
        _append("dni", dni_clean)
        _append("phone", phone_clean)
        _append("cookie", cookie_clean)
        _append("ip", ip_clean)

    if not appended:
        # No contamos con la información necesaria para construir una huella estable.
        return None

    canonical = "|".join(source_parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sanitize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = (value or "").strip()
    if not text:
        return None
    # Simple HTML stripping: replace brackets
    text = text.replace("<", " ").replace(">", " ")
    return text[:2000]


def _extract_pregunta_id(item: Mapping[str, Any]) -> Optional[int]:
    candidate_keys = (
        "pregunta_id",
        "preguntaId",
        "question_id",
        "questionId",
        "id",
    )
    for key in candidate_keys:
        value = item.get(key)
        if isinstance(value, Mapping):
            nested = value.get("id")
            if nested is not None:
                value = nested
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_opcion_ids(item: Mapping[str, Any], pregunta: EncPregunta) -> List[int]:
    list_candidate_keys = (
        "opcion_ids",
        "opcionIds",
        "option_ids",
        "optionIds",
        "opciones",
        "opciones_ids",
        "options",
        "selected_option_ids",
        "selectedOptionIds",
    )
    raw: Any = None
    for key in list_candidate_keys:
        if item.get(key) is not None:
            raw = item.get(key)
            break

    if raw is None:
        single_candidate_keys = (
            "opcion_id",
            "opcionId",
            "option_id",
            "optionId",
            "selected_option",
            "selectedOption",
            "valor",
            "value",
        )
        for key in single_candidate_keys:
            value = item.get(key)
            if value is None:
                continue
            raw = value if isinstance(value, (list, tuple, set)) else [value]
            break

    if isinstance(raw, Mapping):
        nested_id = raw.get("id") or raw.get("option_id") or raw.get("value") or raw.get("valor")
        if nested_id is not None:
            raw = [nested_id]
        else:
            raw = list(raw.values())

    if raw is None:
        candidate_values: List[Any] = []
    elif isinstance(raw, (list, tuple, set)):
        candidate_values = list(raw)
    else:
        candidate_values = [raw]

    opciones_validas = {op.id: op for op in pregunta.opciones}
    resolved: List[int] = []
    fallback_labels: List[str] = []

    for value in candidate_values:
        candidate = value
        if isinstance(candidate, Mapping):
            candidate = (
                candidate.get("id")
                or candidate.get("option_id")
                or candidate.get("value")
                or candidate.get("valor")
            )
        if candidate in (None, ""):
            continue
        try:
            resolved.append(int(candidate))
            continue
        except (TypeError, ValueError):
            pass

        text_value = str(candidate).strip()
        if text_value:
            fallback_labels.append(text_value.lower())

    if fallback_labels:
        for label in fallback_labels:
            for opcion in opciones_validas.values():
                candidates = [opcion.valor, opcion.texto]
                for candidate in candidates:
                    if not candidate:
                        continue
                    if str(candidate).strip().lower() == label:
                        resolved.append(opcion.id)
                        break
                else:
                    continue
                break

    unique_resolved: List[int] = []
    seen: set[int] = set()
    for oid in resolved:
        if oid in seen:
            continue
        seen.add(oid)
        unique_resolved.append(oid)
    return unique_resolved


def _extract_texto_libre(item: Mapping[str, Any]) -> Optional[str]:
    candidate_keys = (
        "texto_libre",
        "textoLibre",
        "texto",
        "text",
        "value",
        "valor",
        "respuesta",
        "answer",
        "freeText",
    )
    for key in candidate_keys:
        value = item.get(key)
        if isinstance(value, Mapping):
            nested = (
                value.get("texto")
                or value.get("text")
                or value.get("value")
                or value.get("valor")
            )
            value = nested
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        return str(value)
    return None


def _validate_respuesta_payload(
    encuesta: EncEncuesta,
    respuestas_payload: Sequence[Dict[str, Any]],
) -> List[EncRespuestaDetalle]:
    detalles: List[EncRespuestaDetalle] = []
    preguntas_map = {p.id: p for p in encuesta.preguntas}

    answered_ids = set()
    for item in respuestas_payload:
        pregunta_id = _extract_pregunta_id(item)
        if not pregunta_id:
            raise EncuestaError("Respuesta sin pregunta_id")
        pregunta = preguntas_map.get(pregunta_id)
        if not pregunta:
            raise EncuestaError("Pregunta inválida en respuestas")

        respuesta_detalle = EncRespuestaDetalle(
            pregunta_id=pregunta.id,
        )
        answered_ids.add(pregunta.id)

        if pregunta.tipo in {"opcion_unica", "opcion_multiple"}:
            opcion_ids = _extract_opcion_ids(item, pregunta)
            opciones_validas = {op.id: op for op in pregunta.opciones}
            seleccionadas: List[int] = []
            for opcion_id in opcion_ids:
                opcion = opciones_validas.get(opcion_id)
                if not opcion:
                    raise EncuestaError("Opción inválida seleccionada")
                seleccionadas.append(opcion.id)
                detalle_opcion = EncRespuestaDetalle(
                    pregunta_id=pregunta.id,
                    opcion_id=opcion.id,
                )
                detalles.append(detalle_opcion)
            if pregunta.obligatoria and not seleccionadas:
                raise EncuestaError("Pregunta obligatoria sin opciones seleccionadas")
            min_sel = pregunta.min_selecciones or (1 if pregunta.obligatoria else 0)
            max_sel = pregunta.max_selecciones or len(opciones_validas)
            if not (min_sel <= len(seleccionadas) <= max_sel):
                raise EncuestaError("Cantidad de opciones seleccionadas fuera de rango")
            continue

        if pregunta.tipo == "abierta":
            texto = _extract_texto_libre(item)
            texto = _sanitize_text(texto)
            if pregunta.obligatoria and not texto:
                raise EncuestaError("Pregunta abierta obligatoria sin texto")
            respuesta_detalle.texto_libre = texto
            detalles.append(respuesta_detalle)
            continue

        raise EncuestaError("Tipo de pregunta no soportado")

    for pregunta in encuesta.preguntas:
        if pregunta.obligatoria and pregunta.id not in answered_ids:
            raise EncuestaError("Falta responder una pregunta obligatoria")

    return detalles


def _persist_respuesta_entity(
    respuesta: EncRespuesta,
    detalles: Sequence[EncRespuestaDetalle],
) -> EncRespuesta:
    if not detalles:
        raise EncuestaError("Debe enviar respuestas")

    respuesta.detalles = list(detalles)
    db.session.add(respuesta)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        message = str(getattr(exc, "orig", exc)).lower()
        if "uq_enc_respuesta_huella" in message or "huella_unica" in message:
            raise EncuestaError("Ya registramos tu participación", status_code=409) from exc

        logger = _current_app_logger()
        if logger:
            logger.exception("[encuestas] Error al guardar respuesta")
        raise EncuestaError("No se pudo guardar la respuesta", status_code=500) from exc

    return respuesta


def _clean_str(value: Optional[Any], *, max_length: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
    else:
        cleaned = str(value).strip()
    if not cleaned:
        return None
    if max_length is not None:
        return cleaned[:max_length]
    return cleaned


def _coerce_int(value: Optional[Any]) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Optional[Any]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _infer_age_from_birth_year(year: Optional[int]) -> Optional[int]:
    if not year:
        return None
    now_year = datetime.now(timezone.utc).year
    age = now_year - year
    if age < 0 or age > 120:
        return None
    return age


def _infer_birth_year_from_age(age: Optional[int]) -> Optional[int]:
    if age is None:
        return None
    if age < 0 or age > 120:
        return None
    return datetime.now(timezone.utc).year - age


def _compute_age_group(age: Optional[int]) -> Optional[str]:
    if age is None:
        return None
    buckets = (
        (12, "0-12"),
        (17, "13-17"),
        (24, "18-24"),
        (34, "25-34"),
        (44, "35-44"),
        (54, "45-54"),
        (64, "55-64"),
    )
    for max_age, label in buckets:
        if age <= max_age:
            return label
    return "65+"


def _normalize_genero(value: Optional[Any]) -> Optional[str]:
    text = _clean_str(value, max_length=30)
    if not text:
        return None
    lowered = text.lower()
    mapping = {
        "f": "femenino",
        "fem": "femenino",
        "female": "femenino",
        "femenino": "femenino",
        "m": "masculino",
        "masc": "masculino",
        "male": "masculino",
        "masculino": "masculino",
        "nb": "no_binario",
        "non binary": "no_binario",
        "no binario": "no_binario",
        "no_binario": "no_binario",
    }
    if lowered in mapping:
        return mapping[lowered]
    for key, mapped in mapping.items():
        if lowered == key:
            return mapped
    return text[:30]


def _safe_json_value(raw: Optional[str]) -> Optional[Any]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _normalize_metadata(value: Optional[Any]) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        normalized_list = [_normalize_metadata(item) for item in value]
        return normalized_list
    if isinstance(value, dict):
        normalized_dict: Dict[str, Any] = {}
        for key, item in value.items():
            normalized_dict[str(key)] = _normalize_metadata(item)
        return normalized_dict
    try:
        json_value = json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return None
    return json_value


def _coerce_respuestas_payload(value: Optional[Any]) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        parsed = _safe_json_value(value)
        if parsed is None:
            return []
        return _coerce_respuestas_payload(parsed)
    if isinstance(value, Mapping):
        numeric_children: List[Tuple[int, Mapping[str, Any]]] = []
        for key, item in value.items():
            if isinstance(item, Mapping) and (
                isinstance(key, int) or (isinstance(key, str) and key.isdigit())
            ):
                numeric_children.append((int(key), item))
            else:
                numeric_children = []
                break
        if numeric_children:
            return [dict(child) for _, child in sorted(numeric_children, key=lambda pair: pair[0])]
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                parsed_item = _safe_json_value(item)
                if isinstance(parsed_item, Mapping):
                    normalized.append(dict(parsed_item))
                    continue
            if isinstance(item, Mapping):
                normalized.append(dict(item))
        return normalized
    return []


def save_respuesta(
    slug_publico: str,
    payload: Dict[str, Any],
    request_ctx: Dict[str, Any],
    *,
    preferred_tenant_id: Optional[int] = None,
) -> EncRespuesta:
    encuesta = get_public_encuesta(slug_publico, preferred_tenant_id=preferred_tenant_id)
    if not isinstance(payload, dict):
        if isinstance(payload, Mapping):
            payload = dict(payload)
        else:
            raise EncuestaError("Debe enviar respuestas")

    raw_respuestas = payload.get("respuestas")
    if raw_respuestas is None and "answers" in payload:
        raw_respuestas = payload.get("answers")
    respuestas_payload = _coerce_respuestas_payload(raw_respuestas)
    if not isinstance(respuestas_payload, Sequence) or not respuestas_payload:
        raise EncuestaError("Debe enviar respuestas")

    detalles = _validate_respuesta_payload(encuesta, respuestas_payload)
    metadata_raw = payload.get("metadata")
    metadata = _normalize_metadata(metadata_raw)
    metadata_dict = metadata if isinstance(metadata, dict) else None
    metadata_payload = metadata if isinstance(metadata, (dict, list)) else None

    tenant_id = encuesta.tenant_id
    dni = payload.get("dni")
    phone = payload.get("phone")
    anon_cookie = request_ctx.get("anon_id")
    ip = request_ctx.get("ip")

    fingerprint = build_unique_fingerprint(encuesta, tenant_id, dni=dni, phone=phone, ip=ip, anon_cookie=anon_cookie)
    if fingerprint:
        existing = EncRespuesta.query.filter_by(encuesta_id=encuesta.id, huella_unica=fingerprint).first()
        if existing:
            raise EncuestaError("Ya registramos tu participación", status_code=409)

    genero = _normalize_genero(payload.get("genero") or payload.get("sexo"))
    edad = _coerce_int(payload.get("edad"))
    anio_nacimiento = _coerce_int(payload.get("anio_nacimiento"))
    rango_etario = _clean_str(payload.get("rango_etario"), max_length=30)

    lat = _coerce_float(payload.get("lat"))
    lng = _coerce_float(payload.get("lng"))
    barrio = _clean_str(payload.get("barrio"), max_length=120)
    ciudad = _clean_str(payload.get("ciudad"), max_length=120)
    provincia = _clean_str(payload.get("provincia"), max_length=120)
    pais = _clean_str(payload.get("pais"), max_length=120)

    canal = _clean_str(payload.get("canal"), max_length=64)
    request_canal = _clean_str(request_ctx.get("canal"), max_length=64)
    canal = canal or request_canal or "web"

    submitted_override = None
    metadata_rango = None

    if metadata_dict:
        metadata_canal = _clean_str(metadata_dict.get("canal"), max_length=64)
        if metadata_canal:
            canal = metadata_canal

        submitted_raw = metadata_dict.get("submittedAt") or metadata_dict.get("submitted_at")
        if submitted_raw:
            submitted_override = _parse_datetime(str(submitted_raw))

        demographics = metadata_dict.get("demographics")
        if isinstance(demographics, dict):
            genero = genero or _normalize_genero(
                demographics.get("genero")
                or demographics.get("gender")
                or demographics.get("sexo")
            )
            metadata_rango = _clean_str(
                demographics.get("rangoEtario") or demographics.get("rango_etario"),
                max_length=30,
            )
            if edad is None:
                edad = _coerce_int(demographics.get("edad"))
            if anio_nacimiento is None:
                anio_nacimiento = _coerce_int(
                    demographics.get("anioNacimiento")
                    or demographics.get("anio_nacimiento")
                )
            ubicacion = demographics.get("ubicacion") or demographics.get("ubicación")
            if isinstance(ubicacion, dict):
                if lat is None:
                    lat = _coerce_float(ubicacion.get("lat"))
                if lng is None:
                    lng = _coerce_float(ubicacion.get("lng"))
                barrio = barrio or _clean_str(ubicacion.get("barrio"), max_length=120)
                ciudad = ciudad or _clean_str(ubicacion.get("ciudad"), max_length=120)
                provincia = provincia or _clean_str(ubicacion.get("provincia"), max_length=120)
                pais = pais or _clean_str(ubicacion.get("pais"), max_length=120)

    if anio_nacimiento is None and payload.get("fecha_nacimiento"):
        fecha = _parse_datetime(payload["fecha_nacimiento"])
        if fecha:
            anio_nacimiento = fecha.year
    if edad is None:
        edad = _coerce_int(payload.get("edad_aproximada"))
    if edad is None:
        edad = _infer_age_from_birth_year(anio_nacimiento)
    if anio_nacimiento is None:
        anio_nacimiento = _infer_birth_year_from_age(edad)
    rango_etario = rango_etario or metadata_rango or _compute_age_group(edad)

    submitted_at = submitted_override or datetime.now(timezone.utc)

    respuesta = EncRespuesta(
        encuesta_id=encuesta.id,
        tenant_id=tenant_id,
        huella_unica=fingerprint,
        user_id=payload.get("user_id"),
        dni=dni,
        phone=phone,
        ip=ip,
        ua=request_ctx.get("user_agent"),
        lat=lat,
        lng=lng,
        utm_source=payload.get("utm_source"),
        utm_campaign=payload.get("utm_campaign"),
        canal=canal,
        genero=genero,
        edad=edad,
        anio_nacimiento=anio_nacimiento,
        rango_etario=rango_etario,
        barrio=barrio,
        ciudad=ciudad,
        provincia=provincia,
        pais=pais,
        metadata_payload=metadata_payload,
        submitted_at=submitted_at,
        content_hash=None,
    )

    respuesta = _persist_respuesta_entity(respuesta, detalles)

    current_app.logger.info(
        "[encuestas] Nueva respuesta %s para encuesta %s desde %s",
        respuesta.id,
        encuesta.id,
        ip,
    )

    # Otorgar puntos si corresponde
    if encuesta.puntos_recompensa and encuesta.puntos_recompensa > 0:
        target_user = None
        if respuesta.user_id:
            target_user = db.session.get(User, respuesta.user_id)

        if target_user:
            try:
                tenant = db.session.get(TenantProfile, tenant_id)
                recompensas_service().acreditar_puntos_manual(
                    target_user,
                    tenant,
                    "encuesta",
                    encuesta.puntos_recompensa
                )
            except Exception:
                current_app.logger.exception("[encuestas] Error al otorgar puntos por encuesta")

    # Emitir actualizaciones en tiempo real si corresponde
    if encuesta.mostrar_resultados_envivo and emit_survey_update:
        try:
            public_slug = _resolve_public_slug(encuesta) or encuesta.slug or slug_publico
            try:
                from services.encuestas_analytics_service import calculate_live_results

                live_stats = calculate_live_results(
                    public_slug,
                    preferred_tenant_id=tenant_id,
                    include_heatmap=True,
                )
                live_stats["legacy_results"] = _compute_live_results(encuesta)
            except Exception:
                current_app.logger.exception(
                    "[encuestas] Error calculando live-results v2 para socket; se emite contrato legacy"
                )
                live_stats = _compute_live_results(encuesta)
            emit_slugs = [
                public_slug,
                slug_publico,
            ]
            for emit_slug in dict.fromkeys(str(item).strip() for item in emit_slugs if item):
                emit_survey_update(emit_slug, live_stats)
        except Exception:
            current_app.logger.exception("[encuestas] Error al emitir update socket")

    return respuesta


def _ensure_timezone(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_encuesta_activa(encuesta: EncEncuesta) -> bool:
    return encuesta.esta_activa()


def _pick_seed_submitted_at(
    rng: random.Random,
    *,
    scenario: str,
    now: datetime,
) -> datetime:
    """Build realistic timestamps for demo seeds.

    ``realtime`` concentrates activity in the last hours so live dashboards look
    active during demos; ``balanced`` keeps a broader 30-day spread.
    """

    normalized = (scenario or "balanced").strip().lower()
    if normalized == "realtime":
        roll = rng.random()
        if roll < 0.7:
            return now - timedelta(minutes=rng.randint(0, 120))
        if roll < 0.95:
            return now - timedelta(hours=rng.randint(2, 24), minutes=rng.randint(0, 59))
        return now - timedelta(days=rng.randint(1, 7), hours=rng.randint(0, 23))

    return now - timedelta(days=rng.randint(0, 28), minutes=rng.randint(0, 1440))


def _seed_weighted_choice(rng: random.Random, options: Sequence[str], weights: Sequence[float]) -> str:
    if not options:
        return ""
    return str(rng.choices(list(options), weights=list(weights), k=1)[0])


def seed_encuesta_respuestas_demo(
    encuesta_id: int,
    user: Any,
    cantidad: int = 100,
    *,
    geo_profile_key: Optional[str] = None,
    municipality_label: Optional[str] = None,
    seed: Optional[int] = None,
    reset_data: bool = False,
    scenario: str = "balanced",
) -> Dict[str, Any]:
    if cantidad <= 0:
        raise EncuestaError("Debe solicitar al menos una respuesta demo")

    encuesta = get_encuesta(encuesta_id, user=user)
    tenant_id = encuesta.tenant_id
    geo_metadata = _resolve_geo_metadata_for_tenant(tenant_id)
    if not geo_metadata and geo_profile_key:
        geo_metadata = _resolve_geo_metadata(profile_key=geo_profile_key)
    if not geo_metadata and municipality_label:
        geo_metadata = _resolve_geo_metadata(municipality=municipality_label)
    reset_summary: Optional[Dict[str, int]] = None
    if reset_data:
        reset_summary = _reset_encuesta_demo_data(encuesta)

    rng = random.Random(seed)

    location_question = None
    otros_option = None
    standard_location_options: List[EncOpcion] = []
    for pregunta in encuesta.preguntas:
        if pregunta.tipo == "opcion_unica":
            for opcion in pregunta.opciones:
                if opcion.valor == "geo_autocomplete":
                    location_question = pregunta
                    otros_option = opcion
                    standard_location_options = [
                        opt for opt in pregunta.opciones if opt.id != opcion.id
                    ]
                    break
            if location_question:
                break

    comentarios = [
        "Gracias por escucharnos",
        "Sería bueno reforzar la iluminación en mi cuadra",
        "Excelente iniciativa para planificar mejoras",
        "Ojalá sigan estas encuestas participativas",
        "Necesitamos más controles y presencia ciudadana",
        "La propuesta me parece clara y necesaria",
        "Necesitamos seguimiento y tableros públicos en tiempo real",
    ]
    generos = ["femenino", "masculino", "no_binario", None]

    scenario_normalized = (scenario or "balanced").strip().lower()
    if scenario_normalized not in {"balanced", "realtime"}:
        raise EncuestaError("Scenario inválido. Valores soportados: balanced, realtime")

    if scenario_normalized == "realtime":
        canales = ["web", "whatsapp", "presencial"]
        canales_weights = [0.62, 0.28, 0.10]
        utm_sources = ["web", "qr", "campana"]
        utm_source_weights = [0.58, 0.24, 0.18]
        utm_campaigns = ["debate", "territorio", "digital", "inversionistas"]
        utm_campaign_weights = [0.34, 0.26, 0.22, 0.18]
    else:
        canales = ["web", "whatsapp", "presencial"]
        canales_weights = [0.45, 0.35, 0.20]
        utm_sources = ["web", "qr", "campana"]
        utm_source_weights = [0.40, 0.35, 0.25]
        utm_campaigns = ["demo", "lanzamiento", "presentacion", "inversionistas"]
        utm_campaign_weights = [0.35, 0.30, 0.20, 0.15]

    barrios_catalogo = list((geo_metadata or {}).get("neighborhoods") or [])
    distritos_catalogo = list((geo_metadata or {}).get("districts") or [])
    posibles_barrios = barrios_catalogo + distritos_catalogo

    created = 0
    skipped = 0
    attempts = 0
    now = datetime.now(timezone.utc)
    demo_batch_id = f"seed-{encuesta.id}-{int(now.timestamp())}"
    analytics_counter = {
        "canales": Counter(),
        "utm_source": Counter(),
        "utm_campaign": Counter(),
        "barrios": Counter(),
    }
    dni_usados: set[str] = set()
    phone_usados: set[str] = set()
    fingerprints: set[str] = set()

    while created < cantidad and attempts < cantidad * 6:
        attempts += 1

        dni = f"{rng.randint(20000000, 49999999):08d}"
        if dni in dni_usados:
            continue
        phone = f"+549261{rng.randint(4000000, 9999999):07d}"
        if phone in phone_usados:
            continue

        genero = rng.choice(generos)
        edad = rng.randint(18, 72)
        anio_nacimiento = datetime.now(timezone.utc).year - edad
        rango_etario = _compute_age_group(edad)

        lat, lng, barrio_hint = _pick_geo_point(geo_metadata, rng)
        barrio_label = barrio_hint or (rng.choice(posibles_barrios) if posibles_barrios else None)

        if lat is None or lng is None:
            # Fallback coordinates around Gran Mendoza to asegurar mapa visible.
            lat = rng.uniform(-33.2, -32.8)
            lng = rng.uniform(-68.9, -68.3)

        submitted_at = _pick_seed_submitted_at(
            rng,
            scenario=scenario_normalized,
            now=now,
        )

        respuestas_items: List[Dict[str, Any]] = []
        for pregunta in encuesta.preguntas:
            if pregunta is location_question:
                opcion_ids: List[int] = []
                if standard_location_options and rng.random() > 0.2:
                    opcion = rng.choice(standard_location_options)
                    opcion_ids = [opcion.id]
                    barrio_label = barrio_label or opcion.texto
                elif otros_option is not None:
                    opcion_ids = [otros_option.id]
                    if not barrio_label:
                        barrio_label = f"Barrio sin registrar {rng.randint(1, 90)}"
                elif pregunta.opciones:
                    opcion = rng.choice(pregunta.opciones)
                    opcion_ids = [opcion.id]
                    barrio_label = barrio_label or opcion.texto
                if opcion_ids:
                    respuestas_items.append({"pregunta_id": pregunta.id, "opcion_ids": opcion_ids})
                continue

            if pregunta.tipo == "opcion_unica" and pregunta.opciones:
                opcion = rng.choice(pregunta.opciones)
                respuestas_items.append({"pregunta_id": pregunta.id, "opcion_ids": [opcion.id]})
                continue

            if pregunta.tipo == "opcion_multiple" and pregunta.opciones:
                opciones = list(pregunta.opciones)
                max_sel = pregunta.max_selecciones or len(opciones)
                max_sel = min(max_sel, len(opciones))
                min_sel = pregunta.min_selecciones or (1 if pregunta.obligatoria else 0)
                min_sel = max(0, min_sel)
                if max_sel <= 0:
                    continue
                cantidad_sel = rng.randint(max(1, min_sel), max_sel)
                seleccionadas = rng.sample(opciones, k=cantidad_sel)
                respuestas_items.append({
                    "pregunta_id": pregunta.id,
                    "opcion_ids": [op.id for op in seleccionadas],
                })
                continue

            texto = rng.choice(comentarios)
            respuestas_items.append({
                "pregunta_id": pregunta.id,
                "texto_libre": texto,
            })

        if not respuestas_items:
            skipped += 1
            continue

        payload_data = {
            "dni": dni,
            "phone": phone,
            "respuestas": respuestas_items,
            "genero": genero,
            "edad": edad,
            "anio_nacimiento": anio_nacimiento,
            "rango_etario": rango_etario,
            "lat": lat,
            "lng": lng,
            "barrio": barrio_label,
            "ciudad": (geo_metadata or {}).get("municipality"),
            "provincia": (geo_metadata or {}).get("state"),
            "pais": (geo_metadata or {}).get("country"),
            "utm_source": _seed_weighted_choice(rng, utm_sources, utm_source_weights),
            "utm_campaign": _seed_weighted_choice(rng, utm_campaigns, utm_campaign_weights),
            "canal": _seed_weighted_choice(rng, canales, canales_weights),
        }

        request_ctx = {
            "ip": f"10.0.0.{rng.randint(1, 254)}",
            "anon_id": f"seed-{encuesta.id}-{attempts}",
            "user_agent": "demo-seed",
            "canal": payload_data["canal"],
        }

        try:
            detalles = _validate_respuesta_payload(encuesta, payload_data["respuestas"])
        except EncuestaError:
            skipped += 1
            continue

        fingerprint = build_unique_fingerprint(
            encuesta,
            tenant_id,
            dni=dni,
            phone=phone,
            ip=request_ctx["ip"],
            anon_cookie=request_ctx["anon_id"],
        )

        if fingerprint:
            if fingerprint in fingerprints:
                skipped += 1
                continue
            existing = EncRespuesta.query.filter_by(
                encuesta_id=encuesta.id,
                huella_unica=fingerprint,
            ).first()
            if existing:
                skipped += 1
                continue

        respuesta = EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=tenant_id,
            metadata_payload={
                "is_demo_seed": True,
                "demo_batch_id": demo_batch_id,
                "demo_scenario": scenario_normalized,
            },
            huella_unica=fingerprint,
            dni=dni,
            phone=phone,
            ip=request_ctx["ip"],
            ua=request_ctx.get("user_agent"),
            lat=payload_data["lat"],
            lng=payload_data["lng"],
            utm_source=payload_data["utm_source"],
            utm_campaign=payload_data["utm_campaign"],
            canal=payload_data["canal"],
            genero=_normalize_genero(payload_data.get("genero")),
            edad=payload_data.get("edad"),
            anio_nacimiento=payload_data.get("anio_nacimiento"),
            rango_etario=payload_data.get("rango_etario"),
            barrio=_clean_str(payload_data.get("barrio"), max_length=120),
            ciudad=_clean_str(payload_data.get("ciudad"), max_length=120),
            provincia=_clean_str(payload_data.get("provincia"), max_length=120),
            pais=_clean_str(payload_data.get("pais"), max_length=120),
            submitted_at=submitted_at,
        )

        try:
            _persist_respuesta_entity(respuesta, detalles)
        except EncuestaError:
            skipped += 1
            continue

        dni_usados.add(dni)
        phone_usados.add(phone)
        if fingerprint:
            fingerprints.add(fingerprint)
        created += 1
        analytics_counter["canales"][payload_data["canal"]] += 1
        analytics_counter["utm_source"][payload_data["utm_source"]] += 1
        analytics_counter["utm_campaign"][payload_data["utm_campaign"]] += 1
        if payload_data.get("barrio"):
            analytics_counter["barrios"][payload_data["barrio"]] += 1

    current_app.logger.info(
        "[encuestas] Seed demo agregó %s respuestas a la encuesta %s (saltadas=%s)",
        created,
        encuesta.id,
        skipped,
    )

    return {
        "encuesta_id": encuesta.id,
        "creadas": created,
        "omitidas": skipped,
        "objetivo": cantidad,
        "seed": seed,
        "scenario": scenario_normalized,
        "reset": reset_summary,
        "demo_batch_id": demo_batch_id,
        "analytics_preview": {
            "canales": dict(analytics_counter["canales"]),
            "utm_source": dict(analytics_counter["utm_source"]),
            "utm_campaign": dict(analytics_counter["utm_campaign"]),
            "top_barrios": [
                {"label": label, "value": value}
                for label, value in analytics_counter["barrios"].most_common(5)
            ],
        },
    }


def _reset_encuesta_demo_data(encuesta: EncEncuesta) -> Dict[str, int]:
    respuesta_ids = [
        respuesta_id
        for (respuesta_id,) in (
            db.session.query(EncRespuesta.id)
            .filter_by(encuesta_id=encuesta.id)
            .all()
        )
    ]
    respuestas_count = len(respuesta_ids)
    comentarios_count = (
        db.session.query(EncComentario.id)
        .filter_by(encuesta_id=encuesta.id)
        .count()
    )

    if respuesta_ids:
        (
            db.session.query(EncRespuestaDetalle)
            .filter(EncRespuestaDetalle.respuesta_id.in_(respuesta_ids))
            .delete(synchronize_session=False)
        )
        (
            db.session.query(EncRespuesta)
            .filter(EncRespuesta.id.in_(respuesta_ids))
            .delete(synchronize_session=False)
        )

    db.session.query(EncComentario).filter_by(encuesta_id=encuesta.id).delete(
        synchronize_session=False
    )
    db.session.commit()

    current_app.logger.info(
        "[encuestas] Reset demo datos encuesta %s (respuestas=%s comentarios=%s)",
        encuesta.id,
        respuestas_count,
        comentarios_count,
    )

    return {
        "respuestas": respuestas_count,
        "comentarios": comentarios_count,
    }


def _collect_recent_geo_points(
    encuestas: Sequence[EncEncuesta],
    limit_per_encuesta: int = 200,
) -> Dict[int, List[Dict[str, Any]]]:
    encuesta_ids = [encuesta.id for encuesta in encuestas if encuesta.id]
    if not encuesta_ids:
        return {}

    max_rows = limit_per_encuesta * len(encuesta_ids)
    query = (
        EncRespuesta.query.options(
            load_only(
                EncRespuesta.encuesta_id,
                EncRespuesta.lat,
                EncRespuesta.lng,
                EncRespuesta.barrio,
                EncRespuesta.ciudad,
                EncRespuesta.provincia,
                EncRespuesta.submitted_at,
            )
        )
        .filter(EncRespuesta.encuesta_id.in_(encuesta_ids))
        .filter(EncRespuesta.lat.isnot(None))
        .filter(EncRespuesta.lng.isnot(None))
        .order_by(EncRespuesta.submitted_at.desc(), EncRespuesta.id.desc())
        .limit(max_rows)
    )

    points: Dict[int, List[Dict[str, Any]]] = {encuesta_id: [] for encuesta_id in encuesta_ids}
    for respuesta in query:
        bucket = points.get(respuesta.encuesta_id)
        if bucket is None:
            continue
        if len(bucket) >= limit_per_encuesta:
            continue
        bucket.append(
            {
                "lat": respuesta.lat,
                "lng": respuesta.lng,
                "barrio": respuesta.barrio,
                "ciudad": respuesta.ciudad,
                "provincia": respuesta.provincia,
                "submitted_at": respuesta.submitted_at.isoformat()
                if respuesta.submitted_at
                else None,
            }
        )
    return points


def _collect_admin_panel_stats(
    encuestas: Sequence[EncEncuesta],
) -> Dict[int, Dict[str, Any]]:
    encuesta_ids = [encuesta.id for encuesta in encuestas if encuesta.id]
    if not encuesta_ids:
        return {}

    raw_stats = {
        encuesta_id: {
            "total_respuestas": 0,
            "respuestas_ultimas_24h": 0,
            "respuestas_con_coordenadas": 0,
            "participantes_unicos": set(),
            "ultima_respuesta_at": None,
            "canales": Counter(),
            "utm": Counter(),
        }
        for encuesta_id in encuesta_ids
    }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    respuestas = (
        EncRespuesta.query.options(
            load_only(
                EncRespuesta.id,
                EncRespuesta.encuesta_id,
                EncRespuesta.submitted_at,
                EncRespuesta.lat,
                EncRespuesta.lng,
                EncRespuesta.huella_unica,
                EncRespuesta.user_id,
                EncRespuesta.dni,
                EncRespuesta.phone,
                EncRespuesta.ip,
                EncRespuesta.canal,
                EncRespuesta.utm_source,
                EncRespuesta.utm_campaign,
            )
        )
        .filter(EncRespuesta.encuesta_id.in_(encuesta_ids))
        .all()
    )

    for respuesta in respuestas:
        stats = raw_stats.get(respuesta.encuesta_id)
        if not stats:
            continue

        stats["total_respuestas"] += 1

        submitted_at = respuesta.submitted_at
        if submitted_at is not None:
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            else:
                submitted_at = submitted_at.astimezone(timezone.utc)
        if submitted_at and submitted_at >= cutoff:
            stats["respuestas_ultimas_24h"] += 1
        if submitted_at and (
            stats["ultima_respuesta_at"] is None
            or submitted_at > stats["ultima_respuesta_at"]
        ):
            stats["ultima_respuesta_at"] = submitted_at

        if respuesta.lat is not None and respuesta.lng is not None:
            stats["respuestas_con_coordenadas"] += 1

        fingerprint = (
            respuesta.huella_unica
            or (respuesta.user_id and f"user:{respuesta.user_id}")
            or (
                respuesta.dni
                and respuesta.dni.strip()
                and f"dni:{respuesta.dni.strip()}"
            )
            or (
                respuesta.phone
                and respuesta.phone.strip()
                and f"phone:{respuesta.phone.strip()}"
            )
            or (respuesta.ip and f"ip:{respuesta.ip}")
        )
        stats["participantes_unicos"].add(
            fingerprint or f"anon:{respuesta.encuesta_id}:{respuesta.id}"
        )

        canal = respuesta.canal or "sin_canal"
        stats["canales"][canal] += 1
        utm_key = (respuesta.utm_source or "n/a", respuesta.utm_campaign or "n/a")
        stats["utm"][utm_key] += 1

    result: Dict[int, Dict[str, Any]] = {}
    for encuesta_id, data in raw_stats.items():
        ultima_dt = data["ultima_respuesta_at"]
        ultima = _ensure_timezone(ultima_dt).isoformat() if ultima_dt else None
        result[encuesta_id] = {
            "total_respuestas": data["total_respuestas"],
            "respuestas_ultimas_24h": data["respuestas_ultimas_24h"],
            "respuestas_con_coordenadas": data["respuestas_con_coordenadas"],
            "participantes_unicos": len(data["participantes_unicos"]),
            "ultima_respuesta_at": ultima,
            "canales": {canal: count for canal, count in data["canales"].items()},
            "utm": [
                {
                    "utm_source": source,
                    "utm_campaign": campaign,
                    "conteo": count,
                }
                for (source, campaign), count in sorted(
                    data["utm"].items(), key=lambda item: item[1], reverse=True
                )
            ],
        }

    return result


def _empty_panel_metrics() -> Dict[str, Any]:
    return {
        "total_respuestas": 0,
        "respuestas_ultimas_24h": 0,
        "respuestas_con_coordenadas": 0,
        "participantes_unicos": 0,
        "ultima_respuesta_at": None,
        "canales": {},
        "utm": [],
    }


def build_admin_list_payload(
    encuestas: Sequence[EncEncuesta],
) -> Dict[str, Any]:
    stats_map = _collect_admin_panel_stats(encuestas)
    geo_points = _collect_recent_geo_points(encuestas)
    encuestas_payload: List[Dict[str, Any]] = []
    estados = Counter()
    total_respuestas = 0
    total_geo = 0
    total_24h = 0
    activas = 0
    con_respuestas = 0

    seed_profiles_map = _geo_catalog()
    for encuesta in encuestas:
        data = serialize_encuesta(encuesta)
        metricas = stats_map.get(encuesta.id or -1, _empty_panel_metrics())
        data["metricas"] = metricas
        data["esta_activa"] = _is_encuesta_activa(encuesta)
        data["slug_publico"] = _resolve_public_slug(encuesta)
        geo_metadata = _resolve_geo_metadata_for_tenant(encuesta.tenant_id)
        data["geo"] = {
            "points": geo_points.get(encuesta.id or -1, []),
            "bounds": geo_metadata.get("bounds") if geo_metadata else None,
            "center": geo_metadata.get("center") if geo_metadata else None,
        }

        auto_seed_cfg = _get_auto_seed_config(encuesta) or {}
        if not auto_seed_cfg:
            default_geo, default_municipality = _guess_auto_seed_defaults(
                municipality_label=None,
                slug_hint=encuesta.slug,
                tenant_id=encuesta.tenant_id,
            )
            auto_seed_cfg = {
                "cantidad": 100,
                "geo_profile_key": default_geo,
                "municipality_label": default_municipality,
                "label": _AUTO_SEED_DEFAULT_LABEL,
            }
        data["seed_demo"] = {
            "label": auto_seed_cfg.get("label") or _AUTO_SEED_DEFAULT_LABEL,
            "cantidad": auto_seed_cfg.get("cantidad", 100),
            "geo_profile_key": auto_seed_cfg.get("geo_profile_key"),
            "municipality_label": auto_seed_cfg.get("municipality_label"),
            "endpoint": f"/api/encuestas/{encuesta.id}/seed-demo",
        }
        encuestas_payload.append(data)

        estados[encuesta.estado] += 1
        total_respuestas += metricas["total_respuestas"]
        total_geo += metricas["respuestas_con_coordenadas"]
        total_24h += metricas["respuestas_ultimas_24h"]
        if metricas["total_respuestas"] > 0:
            con_respuestas += 1
        if data["esta_activa"]:
            activas += 1

    resumen = {
        "total": len(encuestas_payload),
        "por_estado": dict(estados),
        "activas": activas,
        "con_respuestas": con_respuestas,
        "total_respuestas": total_respuestas,
        "respuestas_con_coordenadas": total_geo,
        "respuestas_ultimas_24h": total_24h,
    }

    seed_profiles = []
    for key, entry in (seed_profiles_map or {}).items():
        if not isinstance(entry, dict):
            continue
        seed_profiles.append(
            {
                "key": key,
                "municipality": entry.get("municipality"),
                "state": entry.get("state"),
                "country": entry.get("country"),
                "label": entry.get("municipality")
                or entry.get("label")
                or key.replace("_", " ").title(),
                "bounds": entry.get("bounds"),
                "center": entry.get("center"),
            }
        )

    seed_defaults = {
        "label": _AUTO_SEED_DEFAULT_LABEL,
        "cantidad": 100,
        "geo_profile_key": seed_profiles[0]["key"] if seed_profiles else None,
        "municipality_label": seed_profiles[0]["municipality"] if seed_profiles else None,
    }

    return {
        "encuestas": encuestas_payload,
        "resumen": resumen,
        "seed_demo": {"defaults": seed_defaults, "profiles": seed_profiles},
    }


def serialize_respuesta(respuesta: EncRespuesta) -> Dict[str, Any]:
    detalles_serializados: List[Dict[str, Any]] = []
    for detalle in respuesta.detalles:
        pregunta = detalle.pregunta
        opcion = detalle.opcion
        detalles_serializados.append(
            {
                "pregunta_id": detalle.pregunta_id,
                "pregunta_texto": pregunta.texto if pregunta else None,
                "pregunta_orden": pregunta.orden if pregunta else None,
                "pregunta_tipo": pregunta.tipo if pregunta else None,
                "opcion_id": detalle.opcion_id,
                "opcion_texto": opcion.texto if opcion else None,
                "texto_libre": detalle.texto_libre,
            }
        )

    return {
        "id": respuesta.id,
        "encuesta_id": respuesta.encuesta_id,
        "submitted_at": respuesta.submitted_at.isoformat() if respuesta.submitted_at else None,
        "canal": respuesta.canal,
        "utm_source": respuesta.utm_source,
        "utm_campaign": respuesta.utm_campaign,
        "dni": respuesta.dni,
        "phone": respuesta.phone,
        "ip": respuesta.ip,
        "lat": respuesta.lat,
        "lng": respuesta.lng,
        "user_id": respuesta.user_id,
        "genero": respuesta.genero,
        "edad": respuesta.edad,
        "anio_nacimiento": respuesta.anio_nacimiento,
        "rango_etario": respuesta.rango_etario,
        "barrio": respuesta.barrio,
        "ciudad": respuesta.ciudad,
        "provincia": respuesta.provincia,
        "pais": respuesta.pais,
        "metadata": respuesta.metadata_payload,
        "detalles": detalles_serializados,
    }


def list_respuestas(
    encuesta_id: int,
    user: Any,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[EncEncuesta, List[EncRespuesta], int, int, int]:
    encuesta = get_encuesta(encuesta_id, user=user)

    try:
        limit_value = int(limit) if limit is not None else 50
    except (TypeError, ValueError) as exc:
        raise EncuestaError("Parámetro 'limit' inválido") from exc
    if limit_value <= 0:
        raise EncuestaError("El parámetro 'limit' debe ser mayor a 0")
    limit_value = min(limit_value, 200)

    try:
        offset_value = int(offset) if offset is not None else 0
    except (TypeError, ValueError) as exc:
        raise EncuestaError("Parámetro 'offset' inválido") from exc
    if offset_value < 0:
        raise EncuestaError("El parámetro 'offset' no puede ser negativo")

    total = EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count()

    query = (
        EncRespuesta.query.options(
            joinedload(EncRespuesta.detalles).joinedload(EncRespuestaDetalle.pregunta),
            joinedload(EncRespuesta.detalles).joinedload(EncRespuestaDetalle.opcion),
        )
        .filter_by(encuesta_id=encuesta.id)
        .order_by(EncRespuesta.submitted_at.desc(), EncRespuesta.id.desc())
        .offset(offset_value)
        .limit(limit_value)
    )

    respuestas = query.all()
    return encuesta, respuestas, total, limit_value, offset_value


def _collect_encuesta_tags(encuesta: EncEncuesta) -> List[str]:
    tags: List[str] = []
    seen: set = set()
    ordered_segmentos = sorted(
        encuesta.segmentos,
        key=lambda segmento: (str(segmento.valor or "").lower(), segmento.id or 0),
    )
    for segmento in ordered_segmentos:
        if segmento.clave != "tag":
            continue
        value = _clean_str(segmento.valor, max_length=120)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(value)
    return tags


def serialize_encuesta(encuesta: EncEncuesta) -> Dict[str, Any]:
    def _serialize_question(pregunta: EncPregunta) -> Dict[str, Any]:
        response_type, internal_type = _map_pregunta_tipo_for_response(pregunta.tipo)
        question_payload = {
            "id": pregunta.id,
            "orden": pregunta.orden,
            "tipo": response_type,
            "type": response_type,
            "tipo_interno": internal_type,
            "texto": pregunta.texto,
            "obligatoria": pregunta.obligatoria,
            "min_selecciones": pregunta.min_selecciones,
            "max_selecciones": pregunta.max_selecciones,
            "opciones": [
                {
                    "id": opcion.id,
                    "orden": opcion.orden,
                    "texto": opcion.texto,
                    "valor": opcion.valor,
                }
                for opcion in pregunta.opciones
            ],
        }
        return question_payload

    slug_publico = _resolve_public_slug(encuesta)
    url_publica = _public_url_for_slug(slug_publico)

    return {
        "id": encuesta.id,
        "tenant_id": encuesta.tenant_id,
        "slug": encuesta.slug,
        "slug_publico": slug_publico,
        "canonical_slug": slug_publico or encuesta.slug,
        "url_publica": url_publica,
        "share_url": url_publica,
        "public_api_endpoint": _public_api_endpoint_for_slug(slug_publico),
        "titulo": encuesta.titulo,
        "descripcion": encuesta.descripcion,
        "tipo": encuesta.tipo,
        "estado": encuesta.estado,
        "inicio_at": encuesta.inicio_at.isoformat() if encuesta.inicio_at else None,
        "fin_at": encuesta.fin_at.isoformat() if encuesta.fin_at else None,
        "puntos_recompensa": encuesta.puntos_recompensa or 0,
        "requiere_identidad": encuesta.requiere_identidad,
        "politica_unicidad": encuesta.politica_unicidad,
        "anonimo_permitido": encuesta.anonimo_permitido,
        "es_votacion_envivo": encuesta.es_votacion_envivo,
        "mostrar_resultados_envivo": encuesta.mostrar_resultados_envivo,
        "permitir_comentarios": encuesta.permitir_comentarios,
        "tags": _collect_encuesta_tags(encuesta),
        "preguntas": [_serialize_question(pregunta) for pregunta in encuesta.preguntas],
    }


def serialize_public_encuesta(encuesta: EncEncuesta, slug_publico: Optional[str] = None) -> Dict[str, Any]:
    data = serialize_encuesta(encuesta)
    canonical_slug = _resolve_public_slug(encuesta) or encuesta.slug
    requested_slug = slug_publico or canonical_slug
    data["slug"] = requested_slug
    data["slug_publico"] = canonical_slug
    data["canonical_slug"] = canonical_slug
    data["requested_slug"] = requested_slug
    data["slug_alias_used"] = bool(requested_slug and canonical_slug and requested_slug != canonical_slug)
    data["url_publica"] = _public_url_for_slug(canonical_slug)
    data["share_url"] = data["url_publica"]
    share_text = (
        f"Participa en {data.get('titulo') or 'esta encuesta'}: {data['url_publica']}"
        if data.get("url_publica")
        else None
    )
    data["whatsapp_share_text"] = share_text
    data["whatsapp_share_url"] = (
        f"https://wa.me/?text={quote_plus(share_text)}" if share_text else None
    )
    data["share_whatsapp_url"] = data["whatsapp_share_url"]
    data["public_api_endpoint"] = _public_api_endpoint_for_slug(canonical_slug)
    # Public payload hides estado and flags not needed
    data.pop("estado", None)

    if encuesta.mostrar_resultados_envivo:
        data["resultados_envivo"] = _compute_live_results(encuesta)

    if encuesta.permitir_comentarios:
        # Include recent comments or link to comments endpoint
        pass

    return data


def _compute_live_results(encuesta: EncEncuesta) -> Dict[str, Any]:
    """Aggregate results for live display."""
    results = {
        "total_respuestas": EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count(),
        "preguntas": {}
    }

    # Simple aggregation for closed questions
    for pregunta in encuesta.preguntas:
        if pregunta.tipo in ("opcion_unica", "opcion_multiple", "rating_emoji"):
            # Count details per option
            counts = (
                db.session.query(EncRespuestaDetalle.opcion_id, func.count(EncRespuestaDetalle.id))
                .filter(EncRespuestaDetalle.pregunta_id == pregunta.id)
                .group_by(EncRespuestaDetalle.opcion_id)
                .all()
            )
            opcion_counts = {oid: count for oid, count in counts if oid}

            opciones_data = []
            for opcion in pregunta.opciones:
                opciones_data.append({
                    "id": opcion.id,
                    "texto": opcion.texto,
                    "votos": opcion_counts.get(opcion.id, 0)
                })

            results["preguntas"][pregunta.id] = {
                "tipo": pregunta.tipo,
                "opciones": opciones_data
            }

    return results


def create_comentario(encuesta_id: int, payload: Dict[str, Any], user: Optional[User]) -> EncComentario:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta or not encuesta.permitir_comentarios:
        raise EncuestaError("Comentarios no habilitados para esta encuesta", status_code=403)

    texto = (payload.get("texto") or "").strip()
    if not texto:
        raise EncuestaError("El comentario no puede estar vacío")

    comment_mode = str(payload.get("mode") or payload.get("comment_mode") or "").strip().lower()
    comment_mode = "social" if comment_mode == "social" else "anon"

    auth_provider = (
        payload.get("auth_provider")
        or payload.get("provider")
        or payload.get("social_provider")
    )
    auth_provider = str(auth_provider or "").strip().lower() or None
    if auth_provider and auth_provider not in {"facebook", "google", "instagram"}:
        auth_provider = None

    auth_user_id = str(payload.get("auth_user_id") or "").strip() or None
    auth_email = str(payload.get("auth_email") or "").strip() or None
    auth_first_name = str(payload.get("auth_first_name") or "").strip()
    auth_last_name = str(payload.get("auth_last_name") or "").strip()

    if comment_mode == "social":
        if not auth_provider:
            raise EncuestaError("Proveedor social requerido para comentarios registrados", status_code=400)
        if not (auth_user_id or auth_email or (user and getattr(user, "id", None))):
            raise EncuestaError("Identidad social incompleta para publicar comentario", status_code=400)

    display_name = (
        " ".join(part for part in [auth_first_name, auth_last_name] if part).strip()
        if comment_mode == "social"
        else ""
    )
    if not display_name:
        display_name = (payload.get("nombre") or payload.get("nombre_autor") or "").strip()
    if not display_name and comment_mode == "social":
        display_name = auth_email or (f"Usuario {auth_provider.title()}" if auth_provider else "")

    anon_id = payload.get("anon_id")
    if comment_mode == "social":
        social_identity = auth_user_id or auth_email or str(getattr(user, "id", "")).strip()
        safe_identity = re.sub(r"[^a-zA-Z0-9_\-@.]", "", social_identity or "")[:80]
        if safe_identity:
            anon_id = f"social:{auth_provider}:{safe_identity}"

    comentario = EncComentario(
        encuesta_id=encuesta.id,
        user_id=getattr(user, "id", None) if user else None,
        anon_id=anon_id,
        nombre_autor=display_name or None,
        texto=texto,
        estado="publicado"
    )

    db.session.add(comentario)
    db.session.commit()

    # Best-effort analytics instrumentation for comment UX funnel.
    if analytics_ingestor:
        try:
            mode_changed_from = str(payload.get("previous_mode") or payload.get("prev_mode") or "").strip().lower()
            base_payload = {
                "encuesta_id": encuesta.id,
                "slug": _resolve_public_slug(encuesta) or encuesta.slug,
                "comment_mode": comment_mode,
                "auth_provider": auth_provider,
                "has_user_id": bool(getattr(user, "id", None)),
            }
            if mode_changed_from and mode_changed_from in {"anon", "social"} and mode_changed_from != comment_mode:
                analytics_ingestor.track(
                    tenant_id=encuesta.tenant_id,
                    event_name="survey_comment_mode_changed",
                    payload={**base_payload, "from_mode": mode_changed_from, "to_mode": comment_mode},
                    user_id=getattr(user, "id", None),
                    anon_id=anon_id,
                    channel=payload.get("channel") or "public_survey",
                    tenant_type="municipio",
                    entity_ref=str(encuesta.id),
                )

            analytics_ingestor.track(
                tenant_id=encuesta.tenant_id,
                event_name="survey_comment_submitted",
                payload=base_payload,
                user_id=getattr(user, "id", None),
                anon_id=anon_id,
                channel=payload.get("channel") or "public_survey",
                tenant_type="municipio",
                entity_ref=str(encuesta.id),
            )
        except Exception:
            logger = _current_app_logger()
            if logger:
                logger.exception(
                    "[encuestas] analytics ingest failed for comment encuesta_id=%s",
                    encuesta.id,
                )

    # Emit live event
    if emit_survey_comment:
        try:
            slug_publico = _resolve_public_slug(encuesta) or encuesta.slug
            data = {
                "id": comentario.id,
                "texto": _safe_text_value(comentario.texto, fallback=""),
                "nombre_autor": _safe_text_value(comentario.nombre_autor or (user.name if user else "Anónimo"), fallback="Anónimo"),
                "fecha": comentario.created_at.isoformat(),
                "user_id": comentario.user_id
            }
            if isinstance(comentario.anon_id, str) and comentario.anon_id.startswith("social:"):
                parts = comentario.anon_id.split(":", 2)
                if len(parts) == 3:
                    data["comment_mode"] = "social"
                    data["auth_provider"] = parts[1]
            emit_survey_comment(slug_publico, data)
        except Exception:
            current_app.logger.exception("[encuestas] Error al emitir comentario socket")

    return comentario


def list_comentarios(encuesta_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    base_query = (
        EncComentario.query.filter_by(encuesta_id=encuesta_id, estado="publicado")
        .order_by(EncComentario.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    try:
        query = base_query
        if _enc_comentario_has_report_count():
            query = query.options(load_only(
                EncComentario.id,
                EncComentario.texto,
                EncComentario.nombre_autor,
                EncComentario.created_at,
                EncComentario.user_id,
                EncComentario.anon_id,
                EncComentario.estado,
                EncComentario.report_count,
            ))
        else:
            query = query.options(load_only(
                EncComentario.id,
                EncComentario.texto,
                EncComentario.nombre_autor,
                EncComentario.created_at,
                EncComentario.user_id,
                EncComentario.anon_id,
                EncComentario.estado,
            ))
        rows = query.all()
    except Exception as exc:
        logger = _current_app_logger()
        if logger:
            logger.warning("[encuestas] list_comentarios degraded due to schema mismatch: %s", exc)
        rows = (
            base_query
            .with_entities(
                EncComentario.id,
                EncComentario.texto,
                EncComentario.nombre_autor,
                EncComentario.created_at,
                EncComentario.user_id,
                EncComentario.anon_id,
            )
            .all()
        )
        return [
            {
                "id": row.id,
                "texto": _safe_text_value(row.texto, fallback=""),
                "nombre_autor": _safe_text_value(row.nombre_autor, fallback="Anónimo"),
                "fecha": row.created_at.isoformat() if row.created_at else None,
                "user_id": row.user_id,
                "anon_id": row.anon_id,
            }
            for row in rows
        ]

    results = []
    for c in rows:
        comment_mode = "anon"
        auth_provider = None
        auth_user_id = None
        anon_ref = c.anon_id
        if isinstance(anon_ref, str) and anon_ref.startswith("social:"):
            parts = anon_ref.split(":", 2)
            if len(parts) == 3:
                comment_mode = "social"
                auth_provider = parts[1] or None
                auth_user_id = parts[2] or None

        results.append({
            "id": c.id,
            "texto": _safe_text_value(c.texto, fallback=""),
            "nombre_autor": _safe_text_value(c.nombre_autor or (c.user.name if c.user else "Anónimo"), fallback="Anónimo"),
            "fecha": c.created_at.isoformat(),
            "user_id": c.user_id,
            "anon_id": c.anon_id,
            "comment_mode": comment_mode,
            "auth_provider": auth_provider,
            "auth_user_id": auth_user_id,
        })
    return results


def reportar_comentario(comentario_id: int) -> EncComentario:
    comentario = db.session.get(EncComentario, comentario_id)
    if not comentario:
        raise EncuestaError("Comentario no encontrado", status_code=404)

    comentario.report_count += 1
    # Auto-ocultar si recibe muchos reportes (ej: 5)
    if comentario.report_count >= 5 and comentario.estado == "publicado":
        comentario.estado = "revision"

    db.session.commit()
    return comentario


def administrar_comentario(comentario_id: int, accion: str, user: Any) -> EncComentario:
    comentario = db.session.get(EncComentario, comentario_id)
    if not comentario:
        raise EncuestaError("Comentario no encontrado", status_code=404)

    encuesta = db.session.get(EncEncuesta, comentario.encuesta_id)
    _ensure_tenant_access(encuesta, user)

    if accion == "aprobar":
        comentario.estado = "publicado"
        comentario.report_count = 0 # Reset reports on approval
    elif accion == "ocultar":
        comentario.estado = "oculto"
    elif accion == "eliminar":
        comentario.estado = "eliminado" # Soft delete logic or actual delete
    else:
        raise EncuestaError("Acción inválida", status_code=400)

    db.session.commit()
    return comentario


def list_all_comentarios_admin(encuesta_id: int, user: Any, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    encuesta = get_encuesta(encuesta_id, user=user)

    query = (
        EncComentario.query.filter_by(encuesta_id=encuesta.id)
        .order_by(EncComentario.report_count.desc(), EncComentario.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    results = []
    for c in query:
        results.append({
            "id": c.id,
            "texto": _safe_text_value(c.texto, fallback=""),
            "nombre_autor": _safe_text_value(c.nombre_autor, fallback="Anónimo"),
            "fecha": c.created_at.isoformat(),
            "estado": c.estado,
            "report_count": c.report_count,
            "user_id": c.user_id,
        })
    return results

def get_public_encuesta_by_id(encuesta_id: int) -> EncEncuesta:
    """Retrieves a public survey by ID directly, ensuring it's published."""
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    if encuesta.estado != "publicada":
        raise EncuestaError(
            "La encuesta no está activa",
            status_code=403,
            payload={"reason_code": "survey_not_published"},
        )
    return encuesta
