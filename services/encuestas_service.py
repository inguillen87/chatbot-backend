"""Business logic for survey creation, publishing and response handling."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import unicodedata
from collections import Counter
from datetime import datetime, timezone, timedelta
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, load_only

from database import db
from models import (
    EncEncuesta,
    EncPregunta,
    EncOpcion,
    EncRespuesta,
    EncRespuestaDetalle,
    EncLink,
    User,
)


_BOOTSTRAP_TENANT_ID: Optional[int] = None


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


def _current_app_logger():
    try:
        return current_app.logger
    except RuntimeError:
        return None


_BOOTSTRAP_SAMPLE_ENABLED = _env_flag("ENCUESTAS_BOOTSTRAP_SAMPLE", default=True)


_BOOTSTRAP_CONFIG_ENV_VAR = "ENCUESTAS_BOOTSTRAP_CONFIG_PATH"
_BOOTSTRAP_CONFIG_DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "encuestas_bootstrap"
    / "templates.json"
)


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
    return data


def _get_bootstrap_data() -> Dict[str, Any]:
    path = _bootstrap_config_path()
    return _load_bootstrap_data(str(path))


def _bootstrap_templates() -> Sequence[Dict[str, Any]]:
    templates = _get_bootstrap_data().get("templates", [])
    if not isinstance(templates, list):
        return ()
    return tuple(templates)


def _build_bootstrap_payloads(
    municipality: str,
    inicio: datetime,
    fin: datetime,
    templates: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    source_templates = templates or _bootstrap_templates()
    municipality_slug = _slugify(municipality)
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
            "politica_unicidad": template_copy.get("politica_unicidad", "libre"),
            "inicio_at": inicio.isoformat(),
            "fin_at": fin.isoformat(),
        }

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
            "payload_builder": builder,
            "auto_publish": bool(raw_profile.get("auto_publish", True)),
            "tenant_id": raw_profile.get("tenant_id"),
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
    tenant_candidate = (
        getattr(user, "municipio_id", None)
        or getattr(user, "empresa_id", None)
        or getattr(user, "pyme_id", None)
        or getattr(user, "id", None)
    )
    if not tenant_candidate:
        raise EncuestaError("No se pudo determinar el tenant del usuario", status_code=403)
    return int(tenant_candidate)


def _ensure_tenant_access(encuesta: EncEncuesta, user: Any) -> None:
    tenant_id = _determine_tenant_id(user)
    if encuesta.tenant_id != tenant_id:
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


def _validate_pregunta_payload(pregunta: Dict[str, Any], index: int) -> Dict[str, Any]:
    required_fields = {"orden", "tipo", "texto"}
    missing = [campo for campo in required_fields if campo not in pregunta]
    if missing:
        raise EncuestaError(f"Pregunta #{index + 1} incompleta: falta {', '.join(missing)}")
    opciones = pregunta.get("opciones") or []
    if pregunta["tipo"] in {"opcion_unica", "opcion_multiple"} and not opciones:
        raise EncuestaError(f"Pregunta #{index + 1} requiere opciones")
    return pregunta


def _apply_common_updates(encuesta: EncEncuesta, data: Dict[str, Any]) -> None:
    encuesta.titulo = data.get("titulo", encuesta.titulo)
    encuesta.descripcion = data.get("descripcion", encuesta.descripcion)
    encuesta.tipo = data.get("tipo", encuesta.tipo)
    encuesta.inicio_at = _parse_datetime(data.get("inicio_at")) or encuesta.inicio_at
    encuesta.fin_at = _parse_datetime(data.get("fin_at")) or encuesta.fin_at
    if "requiere_identidad" in data:
        encuesta.requiere_identidad = bool(data["requiere_identidad"])
    if "politica_unicidad" in data and data["politica_unicidad"]:
        encuesta.politica_unicidad = data["politica_unicidad"]
    if "anonimo_permitido" in data:
        encuesta.anonimo_permitido = bool(data["anonimo_permitido"])


def _build_pregunta_entities(encuesta: EncEncuesta, preguntas_payload: Sequence[Dict[str, Any]]) -> List[EncPregunta]:
    preguntas: List[EncPregunta] = []
    for idx, pregunta_payload in enumerate(preguntas_payload):
        payload = _validate_pregunta_payload(pregunta_payload, idx)
        pregunta = EncPregunta(
            encuesta=encuesta,
            orden=int(payload.get("orden", idx + 1)),
            tipo=payload.get("tipo", "opcion_unica"),
            texto=(payload.get("texto") or "").strip(),
            obligatoria=bool(payload.get("obligatoria", False)),
            min_selecciones=payload.get("min_selecciones"),
            max_selecciones=payload.get("max_selecciones"),
        )
        opciones_payload = payload.get("opciones") or []
        for opt in opciones_payload:
            opcion = EncOpcion(
                pregunta=pregunta,
                orden=int(opt.get("orden", len(pregunta.opciones) + 1)),
                texto=(opt.get("texto") or "").strip(),
                valor=opt.get("valor"),
            )
            pregunta.opciones.append(opcion)
        preguntas.append(pregunta)
    return preguntas


def create_encuesta(data: Dict[str, Any], user: Any) -> EncEncuesta:
    if not data:
        raise EncuestaError("Payload vacío")

    tenant_id = _determine_tenant_id(user)
    titulo = (data.get("titulo") or "").strip()
    if not titulo:
        raise EncuestaError("El título es requerido")

    slug_seed = data.get("slug") or f"{tenant_id}-{titulo}"
    slug = _generate_unique_slug(_slugify(slug_seed))

    encuesta = EncEncuesta(
        tenant_id=tenant_id,
        slug=slug,
        titulo=titulo,
        descripcion=data.get("descripcion"),
        tipo=data.get("tipo", "opinion"),
        estado="borrador",
        inicio_at=_parse_datetime(data.get("inicio_at")),
        fin_at=_parse_datetime(data.get("fin_at")),
        requiere_identidad=bool(data.get("requiere_identidad", False)),
        politica_unicidad=data.get("politica_unicidad", "libre"),
        anonimo_permitido=bool(data.get("anonimo_permitido", True)),
        created_by=getattr(user, "id", None),
    )

    preguntas_payload = data.get("preguntas") or []
    encuesta.preguntas = _build_pregunta_entities(encuesta, preguntas_payload)

    db.session.add(encuesta)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo crear la encuesta (slug duplicado?)") from exc

    current_app.logger.info("[encuestas] Encuesta %s creada por %s", encuesta.id, getattr(user, "id", None))
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
    if encuesta.estado == "publicada" and "preguntas" in data:
        respuestas_registradas = encuesta.respuestas.count()
        puede_actualizar_estructura = respuestas_registradas == 0
        if not puede_actualizar_estructura:
            raise EncuestaError(
                "No se puede modificar la estructura de una encuesta con respuestas registradas",
                status_code=409,
                payload={"encuesta_id": encuesta.id, "respuestas": respuestas_registradas},
            )

    _apply_common_updates(encuesta, data)

    if "preguntas" in data:
        if not puede_actualizar_estructura:
            raise EncuestaError(
                "No se puede modificar la estructura de una encuesta con respuestas registradas",
                status_code=409,
            )
        encuesta.preguntas.clear()
        db.session.flush()
        nuevas_preguntas = _build_pregunta_entities(encuesta, data.get("preguntas") or [])
        encuesta.preguntas.extend(nuevas_preguntas)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("Error al actualizar la encuesta") from exc

    current_app.logger.info("[encuestas] Encuesta %s actualizada por %s", encuesta.id, getattr(user, "id", None))
    return encuesta


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
        encuesta.inicio_at = datetime.now(timezone.utc)

    slug_publico = _slugify(f"{encuesta.slug}-{secrets.token_hex(3)}")
    link = EncLink(
        encuesta=encuesta,
        slug_publico=slug_publico,
        canal="web",
    )
    encuesta.links.append(link)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo publicar la encuesta") from exc

    current_app.logger.info(
        "[encuestas] Encuesta %s publicada con slug %s por %s",
        encuesta.id,
        slug_publico,
        getattr(user, "id", None),
    )
    return encuesta, link


def cerrar_encuesta(encuesta_id: int, user: Any) -> EncEncuesta:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(encuesta, user)
    encuesta.estado = "cerrada"
    encuesta.fin_at = encuesta.fin_at or datetime.now(timezone.utc)
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


def _bootstrap_sample_if_needed(tenant_id: int) -> None:
    if not _BOOTSTRAP_SAMPLE_ENABLED:
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


def _resolve_public_slug(encuesta: EncEncuesta) -> Optional[str]:
    slug_publico = None
    for link in sorted(encuesta.links, key=lambda link: (link.id or 0), reverse=True):
        if link.slug_publico:
            slug_publico = link.slug_publico
            break

    if not slug_publico and encuesta.estado == "publicada":
        slug_publico = encuesta.slug

    return slug_publico


def list_encuestas(tenant_id: int, estado: Optional[str] = None) -> List[EncEncuesta]:
    _bootstrap_sample_if_needed(tenant_id)
    query = EncEncuesta.query.options(joinedload(EncEncuesta.links)).filter_by(tenant_id=tenant_id)
    if estado:
        query = query.filter_by(estado=estado)
    return query.order_by(EncEncuesta.created_at.desc()).all()


def list_public_encuestas_for_tenant(
    tenant_id: int,
    limit: int = 10,
) -> List[Tuple[EncEncuesta, str]]:
    """Return active public surveys for a tenant along with their public slugs."""

    _bootstrap_sample_if_needed(tenant_id)
    now = datetime.now(timezone.utc)
    query = (
        EncEncuesta.query.options(joinedload(EncEncuesta.links))
        .filter(EncEncuesta.tenant_id == tenant_id)
        .filter(EncEncuesta.estado == "publicada")
        .filter(or_(EncEncuesta.inicio_at.is_(None), EncEncuesta.inicio_at <= now))
        .filter(or_(EncEncuesta.fin_at.is_(None), EncEncuesta.fin_at >= now))
        .order_by(
            EncEncuesta.fin_at.is_(None).desc(),
            EncEncuesta.fin_at.asc(),
            EncEncuesta.inicio_at.desc(),
            EncEncuesta.created_at.desc(),
        )
    )
    if limit and limit > 0:
        query = query.limit(limit)

    encuestas = query.all()
    resultados: List[Tuple[EncEncuesta, str]] = []

    for encuesta in encuestas:
        slug_publico = _resolve_public_slug(encuesta)
        if slug_publico:
            resultados.append((encuesta, slug_publico))

    return resultados


def get_encuesta(encuesta_id: int, tenant_id: Optional[int] = None, user: Any = None) -> EncEncuesta:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    if tenant_id and encuesta.tenant_id != tenant_id:
        raise EncuestaError("Encuesta fuera del tenant", status_code=403)
    if user is not None:
        _ensure_tenant_access(encuesta, user)
    return encuesta


def get_public_encuesta(
    slug_publico: str, *, allow_inactive_for_user: Optional[Any] = None
) -> EncEncuesta:
    normalized_slug = (slug_publico or "").strip().lower()
    if not normalized_slug:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    link = (
        EncLink.query.filter(func.lower(EncLink.slug_publico) == normalized_slug)
        .first()
    )
    encuesta: Optional[EncEncuesta]

    if link:
        encuesta = link.encuesta
    else:
        encuesta = (
            EncEncuesta.query.filter(func.lower(EncEncuesta.slug) == normalized_slug)
            .first()
        )
        if encuesta is None:
            alias_match = _PUBLIC_SLUG_ALIAS_RE.match(normalized_slug)
            if alias_match:
                base_slug = alias_match.group("base")
                encuesta = (
                    EncEncuesta.query.filter(func.lower(EncEncuesta.slug) == base_slug)
                    .first()
                )

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
        raise EncuestaError("La encuesta no está activa", status_code=403)
    if not encuesta.esta_activa():
        raise EncuestaError("La encuesta no está en su ventana de participación", status_code=403)
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

    source_parts: List[str] = [f"encuesta:{encuesta.id}", f"tenant:{tenant_id}", f"policy:{policy}"]
    if policy in {"por_dni", "dni"} and dni:
        source_parts.append(f"dni:{dni.strip()}")
    elif policy in {"por_phone", "phone"} and phone:
        source_parts.append(f"phone:{phone.strip()}")
    elif policy in {"por_cookie", "cookie"} and anon_cookie:
        source_parts.append(f"cookie:{anon_cookie}")
    elif policy in {"por_ip", "ip"} and ip:
        source_parts.append(f"ip:{ip}" )
    elif policy in {"por_dni_o_phone", "dni_o_phone"}:
        if dni:
            source_parts.append(f"dni:{dni.strip()}")
        if phone:
            source_parts.append(f"phone:{phone.strip()}")
    else:
        # fallback usa todo lo disponible
        if dni:
            source_parts.append(f"dni:{dni.strip()}")
        if phone:
            source_parts.append(f"phone:{phone.strip()}")
        if anon_cookie:
            source_parts.append(f"cookie:{anon_cookie}")
        if ip:
            source_parts.append(f"ip:{ip}")

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


def _validate_respuesta_payload(
    encuesta: EncEncuesta,
    respuestas_payload: Sequence[Dict[str, Any]],
) -> List[EncRespuestaDetalle]:
    detalles: List[EncRespuestaDetalle] = []
    preguntas_map = {p.id: p for p in encuesta.preguntas}

    answered_ids = set()
    for item in respuestas_payload:
        pregunta_id = item.get("pregunta_id")
        if not pregunta_id:
            raise EncuestaError("Respuesta sin pregunta_id")
        pregunta = preguntas_map.get(int(pregunta_id))
        if not pregunta:
            raise EncuestaError("Pregunta inválida en respuestas")

        respuesta_detalle = EncRespuestaDetalle(
            pregunta_id=pregunta.id,
        )
        answered_ids.add(pregunta.id)

        if pregunta.tipo in {"opcion_unica", "opcion_multiple"}:
            opcion_ids = item.get("opcion_ids") or []
            if not isinstance(opcion_ids, (list, tuple)):
                raise EncuestaError("opcion_ids debe ser lista")
            opcion_ids = [int(oid) for oid in opcion_ids]
            opciones_validas = {op.id: op for op in pregunta.opciones}
            seleccionadas = []
            for oid in opcion_ids:
                opcion = opciones_validas.get(oid)
                if not opcion:
                    raise EncuestaError("Opción inválida seleccionada")
                seleccionadas.append(opcion)
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
            texto = _sanitize_text(item.get("texto_libre"))
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
        "m": "masculino",
        "masc": "masculino",
        "male": "masculino",
        "nb": "no_binario",
        "non binary": "no_binario",
    }
    if lowered in mapping:
        return mapping[lowered]
    for key, mapped in mapping.items():
        if lowered == key:
            return mapped
    return text[:30]


def save_respuesta(slug_publico: str, payload: Dict[str, Any], request_ctx: Dict[str, Any]) -> EncRespuesta:
    encuesta = get_public_encuesta(slug_publico)
    respuestas_payload = payload.get("respuestas") or []
    if not isinstance(respuestas_payload, Sequence) or not respuestas_payload:
        raise EncuestaError("Debe enviar respuestas")

    detalles = _validate_respuesta_payload(encuesta, respuestas_payload)

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
    rango_etario = _clean_str(payload.get("rango_etario"), max_length=30) or _compute_age_group(edad)

    respuesta = EncRespuesta(
        encuesta_id=encuesta.id,
        tenant_id=tenant_id,
        huella_unica=fingerprint,
        user_id=payload.get("user_id"),
        dni=dni,
        phone=phone,
        ip=ip,
        ua=request_ctx.get("user_agent"),
        lat=payload.get("lat"),
        lng=payload.get("lng"),
        utm_source=payload.get("utm_source"),
        utm_campaign=payload.get("utm_campaign"),
        canal=payload.get("canal") or request_ctx.get("canal") or "web",
        genero=genero,
        edad=edad,
        anio_nacimiento=anio_nacimiento,
        rango_etario=rango_etario,
        barrio=_clean_str(payload.get("barrio"), max_length=120),
        ciudad=_clean_str(payload.get("ciudad"), max_length=120),
        provincia=_clean_str(payload.get("provincia"), max_length=120),
        pais=_clean_str(payload.get("pais"), max_length=120),
        submitted_at=datetime.now(timezone.utc),
        content_hash=None,
    )

    respuesta.detalles = detalles

    db.session.add(respuesta)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        if "uq_enc_respuesta_huella" in str(exc.orig):
            raise EncuestaError("Respuesta duplicada", status_code=409) from exc
        raise EncuestaError("No se pudo guardar la respuesta") from exc

    current_app.logger.info(
        "[encuestas] Nueva respuesta %s para encuesta %s desde %s",
        respuesta.id,
        encuesta.id,
        ip,
    )
    return respuesta


def _ensure_timezone(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_encuesta_activa(encuesta: EncEncuesta) -> bool:
    if encuesta.estado != "publicada":
        return False
    now = datetime.now(timezone.utc)
    inicio = _ensure_timezone(encuesta.inicio_at)
    fin = _ensure_timezone(encuesta.fin_at)
    if inicio and now < inicio:
        return False
    if fin and now > fin:
        return False
    return True


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
    encuestas_payload: List[Dict[str, Any]] = []
    estados = Counter()
    total_respuestas = 0
    total_geo = 0
    total_24h = 0
    activas = 0
    con_respuestas = 0

    for encuesta in encuestas:
        data = serialize_encuesta(encuesta)
        metricas = stats_map.get(encuesta.id or -1, _empty_panel_metrics())
        data["metricas"] = metricas
        data["esta_activa"] = _is_encuesta_activa(encuesta)
        data["slug_publico"] = _resolve_public_slug(encuesta)
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

    return {"encuestas": encuestas_payload, "resumen": resumen}


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


def serialize_encuesta(encuesta: EncEncuesta) -> Dict[str, Any]:
    return {
        "id": encuesta.id,
        "tenant_id": encuesta.tenant_id,
        "slug": encuesta.slug,
        "titulo": encuesta.titulo,
        "descripcion": encuesta.descripcion,
        "tipo": encuesta.tipo,
        "estado": encuesta.estado,
        "inicio_at": encuesta.inicio_at.isoformat() if encuesta.inicio_at else None,
        "fin_at": encuesta.fin_at.isoformat() if encuesta.fin_at else None,
        "requiere_identidad": encuesta.requiere_identidad,
        "politica_unicidad": encuesta.politica_unicidad,
        "anonimo_permitido": encuesta.anonimo_permitido,
        "preguntas": [
            {
                "id": pregunta.id,
                "orden": pregunta.orden,
                "tipo": pregunta.tipo,
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
            for pregunta in encuesta.preguntas
        ],
    }


def serialize_public_encuesta(encuesta: EncEncuesta, slug_publico: Optional[str] = None) -> Dict[str, Any]:
    data = serialize_encuesta(encuesta)
    data["slug"] = slug_publico or encuesta.slug
    # Public payload hides estado and flags not needed
    data.pop("estado", None)
    data.pop("requiere_identidad", None)
    return data
