"""Public endpoints for answering surveys."""

import io
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Optional

from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from config.feature_flags import FEATURE_ENCUESTAS
from models import EncEncuesta
from services.encuestas_service import (
    EncuestaError,
    get_public_encuesta,
    save_respuesta,
    serialize_public_encuesta,
)
from services.encuestas_qr_service import build_qr_png


encuestas_public_bp = Blueprint("encuestas_public_bp", __name__, url_prefix="/api/public/encuestas")
encuestas_public_legacy_bp = Blueprint(
    "encuestas_public_legacy_bp", __name__, url_prefix="/public/encuestas"
)

_RATE_LIMIT = 30
_RATE_PERIOD = 60
_rate_buckets: defaultdict[str, deque] = defaultdict(deque)
_rate_lock = Lock()


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


@encuestas_public_bp.before_request
@encuestas_public_legacy_bp.before_request
def _check_feature():
    guard = _feature_guard()
    if guard:
        return guard
    return None


def _serialize_public_list(encuestas: List[EncEncuesta]) -> List[Dict[str, Optional[str]]]:
    resultados: List[Dict[str, Optional[str]]] = []
    for encuesta in encuestas:
        slug_publico = None
        for link in sorted(encuesta.links, key=lambda link: (link.id or 0), reverse=True):
            if link.slug_publico:
                slug_publico = link.slug_publico
                break
        if not slug_publico:
            continue

        resultados.append(
            {
                "slug": slug_publico,
                "titulo": encuesta.titulo,
                "inicio_at": encuesta.inicio_at.isoformat() if encuesta.inicio_at else None,
                "fin_at": encuesta.fin_at.isoformat() if encuesta.fin_at else None,
            }
        )
    return resultados


def _list_public_surveys():
    now = datetime.now(timezone.utc)
    query = (
        EncEncuesta.query.options(joinedload(EncEncuesta.links))
        .filter(EncEncuesta.estado == "publicada")
        .filter(or_(EncEncuesta.inicio_at.is_(None), EncEncuesta.inicio_at <= now))
        .filter(or_(EncEncuesta.fin_at.is_(None), EncEncuesta.fin_at >= now))
        .order_by(EncEncuesta.created_at.desc())
    )
    encuestas = query.all()
    data = _serialize_public_list(encuestas)
    return jsonify(data), 200


@encuestas_public_bp.route("", methods=["GET"])
@encuestas_public_legacy_bp.route("", methods=["GET"])
def listar_publicas():
    return _list_public_surveys()


@encuestas_public_bp.route("/<slug>", methods=["GET"])
@encuestas_public_legacy_bp.route("/<slug>", methods=["GET"])
def obtener_encuesta(slug: str):
    try:
        encuesta = get_public_encuesta(slug)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify(serialize_public_encuesta(encuesta, slug_publico=slug))


@encuestas_public_bp.route("/<slug>/responder", methods=["POST"])
@encuestas_public_legacy_bp.route("/<slug>/responder", methods=["POST"])
def responder(slug: str):
    ip = _extract_ip()
    if not _rate_limit(ip):
        return jsonify({"error": "Demasiadas respuestas desde esta IP. Intenta más tarde."}), 429

    request_ctx = {
        "ip": ip,
        "user_agent": request.headers.get("User-Agent"),
        "anon_id": request.cookies.get("Anon-Id") or request.headers.get("X-Anon-Id"),
        "canal": request.args.get("canal"),
    }
    try:
        respuesta = save_respuesta(slug, request.get_json(force=True), request_ctx)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({"ok": True, "respuesta_id": respuesta.id}), 201


@encuestas_public_bp.route("/<slug>/qr")
@encuestas_public_legacy_bp.route("/<slug>/qr")
def qr(slug: str):
    try:
        encuesta = get_public_encuesta(slug)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code

    size = request.args.get("size", default=320, type=int)
    base_url = request.host_url.rstrip("/")
    url = f"{base_url}/e/{slug}"
    try:
        png = build_qr_png(url, size=size)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return send_file(io.BytesIO(png), mimetype="image/png", download_name=f"encuesta-{slug}.png")


def _extract_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def _rate_limit(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        while bucket and now - bucket[0] > _RATE_PERIOD:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT:
            return False
        bucket.append(now)
        return True
