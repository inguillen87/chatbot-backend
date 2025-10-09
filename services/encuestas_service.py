"""Business logic for survey creation, publishing and response handling."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

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


_BOOTSTRAP_SAMPLE_ENABLED = _env_flag("ENCUESTAS_BOOTSTRAP_SAMPLE", default=True)
_BOOTSTRAP_TENANT_ID = _parse_int(os.getenv("JUNIN_ENCUESTAS_TENANT_ID")) or 4

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
    if encuesta.estado != "borrador":
        raise EncuestaError("Solo se puede modificar encuestas en borrador", status_code=409)

    _apply_common_updates(encuesta, data)

    if "preguntas" in data:
        encuesta.preguntas.clear()
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


def _find_bootstrap_user() -> Optional[User]:
    if _BOOTSTRAP_TENANT_ID is None:
        return None

    user = (
        User.query.filter(
            or_(
                User.municipio_id == _BOOTSTRAP_TENANT_ID,
                User.id == _BOOTSTRAP_TENANT_ID,
            )
        )
        .order_by(User.id.asc())
        .first()
    )
    if user:
        return user

    like_pattern = "%junin%"
    return (
        User.query.filter(User.tipo_chat == "municipio")
        .filter(
            or_(
                User.nombre_empresa.ilike(like_pattern),
                User.name.ilike(like_pattern),
                User.email.ilike(like_pattern),
                User.ciudad.ilike(like_pattern),
            )
        )
        .order_by(User.id.asc())
        .first()
    )


def _bootstrap_sample_if_needed(tenant_id: int) -> None:
    if not _BOOTSTRAP_SAMPLE_ENABLED or _BOOTSTRAP_TENANT_ID is None:
        return
    if tenant_id != _BOOTSTRAP_TENANT_ID:
        return

    existing = EncEncuesta.query.filter_by(tenant_id=tenant_id).count()
    if existing:
        return

    user = _find_bootstrap_user()
    if not user:
        current_app.logger.warning(
            "[encuestas] No se encontró un usuario municipal de Junín para crear la encuesta demo"
        )
        return

    ahora = datetime.now(timezone.utc)
    cierre = ahora + timedelta(days=45)
    payload = {
        "titulo": "Participación Ciudadana Junín 2025",
        "slug": "junin-participa",
        "descripcion": (
            "Queremos conocer tus prioridades para planificar obras, seguridad y "
            "actividades en todo Junín. Contanos qué es importante para tu barrio."
        ),
        "tipo": "opinion",
        "anonimo_permitido": True,
        "requiere_identidad": False,
        "politica_unicidad": "por_cookie",
        "inicio_at": ahora.isoformat(),
        "fin_at": cierre.isoformat(),
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Qué proyecto priorizarías para tu barrio?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Mejoras de iluminación y seguridad"},
                    {"orden": 2, "texto": "Pavimentación y mantenimiento de calles"},
                    {"orden": 3, "texto": "Espacios verdes y recreativos"},
                    {"orden": 4, "texto": "Programas deportivos y culturales"},
                ],
            },
            {
                "orden": 2,
                "tipo": "opcion_multiple",
                "texto": (
                    "¿En qué acciones de participación te gustaría sumarte "
                    "durante los próximos meses?"
                ),
                "obligatoria": False,
                "max_selecciones": 3,
                "opciones": [
                    {"orden": 1, "texto": "Cabildos barriales"},
                    {"orden": 2, "texto": "Jornadas de voluntariado"},
                    {"orden": 3, "texto": "Consultas públicas digitales"},
                    {"orden": 4, "texto": "Mesas de trabajo temáticas"},
                ],
            },
            {
                "orden": 3,
                "tipo": "abierta",
                "texto": "Dejanos comentarios o propuestas concretas para Junín",
                "obligatoria": False,
            },
        ],
    }

    try:
        encuesta = create_encuesta(payload, user)
        encuesta, link = publicar_encuesta(encuesta.id, user)
        current_app.logger.info(
            "[encuestas] Encuesta demo de Junín publicada automáticamente con slug %s",
            link.slug_publico,
        )
    except EncuestaError:
        current_app.logger.exception(
            "[encuestas] No se pudo crear la encuesta demo de Junín"
        )


def list_encuestas(tenant_id: int, estado: Optional[str] = None) -> List[EncEncuesta]:
    _bootstrap_sample_if_needed(tenant_id)
    query = EncEncuesta.query.filter_by(tenant_id=tenant_id)
    if estado:
        query = query.filter_by(estado=estado)
    return query.order_by(EncEncuesta.created_at.desc()).all()


def list_public_encuestas_for_tenant(
    tenant_id: int,
    limit: int = 5,
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
        slug_publico = None
        for link in sorted(encuesta.links, key=lambda link: (link.id or 0), reverse=True):
            if link.slug_publico:
                slug_publico = link.slug_publico
                break
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


def get_public_encuesta(slug_publico: str) -> EncEncuesta:
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


def serialize_encuesta(encuesta: EncEncuesta) -> Dict[str, Any]:
    return {
        "id": encuesta.id,
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
