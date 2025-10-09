"""Utilities to build cryptographic snapshots for survey responses."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import joinedload

from database import db
from models import EncAnchorSnapshot, EncRespuesta
from services.encuestas_service import EncuestaError, get_encuesta, _parse_datetime


def _canonical_response_payload(respuesta: EncRespuesta) -> Dict[str, object]:
    detalles = [
        {
            "pregunta_id": detalle.pregunta_id,
            "opcion_id": detalle.opcion_id,
            "texto_libre": detalle.texto_libre,
        }
        for detalle in sorted(
            respuesta.detalles,
            key=lambda d: (d.pregunta_id or 0, d.opcion_id or 0, d.id),
        )
    ]
    return {
        "respuesta_id": respuesta.id,
        "encuesta_id": respuesta.encuesta_id,
        "tenant_id": respuesta.tenant_id,
        "canal": respuesta.canal,
        "utm_source": respuesta.utm_source,
        "utm_campaign": respuesta.utm_campaign,
        "submitted_at": respuesta.submitted_at.isoformat() if respuesta.submitted_at else None,
        "detalles": detalles,
    }


def _hash_payload(payload: Dict[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_content_hash(respuesta_id: int) -> str:
    respuesta: EncRespuesta | None = (
        EncRespuesta.query.options(joinedload(EncRespuesta.detalles)).filter_by(id=respuesta_id).one_or_none()
    )
    if not respuesta:
        raise EncuestaError("Respuesta no encontrada", status_code=404)

    payload = _canonical_response_payload(respuesta)
    content_hash = _hash_payload(payload)
    respuesta.content_hash = content_hash
    db.session.commit()
    return content_hash


def _merkle_root(hashes: Sequence[str]) -> Optional[str]:
    if not hashes:
        return None
    level = list(hashes)
    while len(level) > 1:
        next_level: List[str] = []
        for idx in range(0, len(level), 2):
            left = level[idx]
            right = level[idx + 1] if idx + 1 < len(level) else level[idx]
            combined = hashlib.sha256((left + right).encode("utf-8")).hexdigest()
            next_level.append(combined)
        level = next_level
    return level[0]


def _merkle_proof(hashes: Sequence[str], index: int) -> List[str]:
    proof: List[str] = []
    idx = index
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        next_level: List[str] = []
        for pair_index in range(0, len(level), 2):
            left = level[pair_index]
            right = level[pair_index + 1]
            combined = hashlib.sha256((left + right).encode("utf-8")).hexdigest()
            next_level.append(combined)
            if pair_index == idx or pair_index + 1 == idx:
                if pair_index == idx:
                    proof.append(f"R:{right}")
                else:
                    proof.append(f"L:{left}")
                idx = pair_index // 2
        level = next_level
    return proof


def build_snapshot(encuesta_id: int, desde: str, hasta: str, user: object) -> EncAnchorSnapshot:
    encuesta = get_encuesta(encuesta_id, user=user)
    tenant_id = encuesta.tenant_id
    desde_dt = _parse_datetime(desde)
    hasta_dt = _parse_datetime(hasta)
    if not desde_dt or not hasta_dt:
        raise EncuestaError("Debe indicar rango completo de fechas")

    query = EncRespuesta.query.options(joinedload(EncRespuesta.detalles)).filter_by(encuesta_id=encuesta.id)
    query = query.filter(EncRespuesta.submitted_at >= desde_dt, EncRespuesta.submitted_at <= hasta_dt)
    respuestas = query.order_by(EncRespuesta.submitted_at.asc()).all()
    if not respuestas:
        raise EncuestaError("No hay respuestas en el rango indicado", status_code=404)

    hashes: List[str] = []
    for respuesta in respuestas:
        if not respuesta.content_hash:
            payload = _canonical_response_payload(respuesta)
            respuesta.content_hash = _hash_payload(payload)
        hashes.append(respuesta.content_hash)

    root_hash = _merkle_root(hashes)
    if not root_hash:
        raise EncuestaError("No se pudo construir el Merkle root")

    snapshot = EncAnchorSnapshot(
        encuesta_id=encuesta.id,
        tenant_id=tenant_id,
        algo="sha256",
        root_hash=root_hash,
        total_respuestas=len(respuestas),
        desde_at=desde_dt,
        hasta_at=hasta_dt,
        created_by=getattr(user, "id", None),
    )
    db.session.add(snapshot)
    db.session.flush()

    for respuesta in respuestas:
        respuesta.snapshot_id = snapshot.id

    db.session.commit()
    return snapshot


def publish_snapshot(snapshot_id: int, chain: str = "polygon") -> EncAnchorSnapshot:
    snapshot = db.session.get(EncAnchorSnapshot, snapshot_id)
    if not snapshot:
        raise EncuestaError("Snapshot no encontrado", status_code=404)
    snapshot.chain = chain
    snapshot.anchor_status = "published"
    snapshot.anchor_at = datetime.now(timezone.utc)
    snapshot.tx_id = snapshot.tx_id or f"SIM-{uuid.uuid4()}"
    db.session.commit()
    return snapshot


def generate_merkle_proof(snapshot_id: int, respuesta_id: int) -> Dict[str, object]:
    snapshot: EncAnchorSnapshot | None = (
        EncAnchorSnapshot.query.options(joinedload(EncAnchorSnapshot.respuestas).joinedload(EncRespuesta.detalles))
        .filter_by(id=snapshot_id)
        .one_or_none()
    )
    if not snapshot:
        raise EncuestaError("Snapshot no encontrado", status_code=404)

    respuestas = sorted(snapshot.respuestas, key=lambda r: r.submitted_at or datetime.now(timezone.utc))
    hashes = []
    target_index = None
    for idx, respuesta in enumerate(respuestas):
        if not respuesta.content_hash:
            payload = _canonical_response_payload(respuesta)
            respuesta.content_hash = _hash_payload(payload)
        hashes.append(respuesta.content_hash)
        if respuesta.id == respuesta_id:
            target_index = idx

    if target_index is None:
        raise EncuestaError("La respuesta no pertenece al snapshot", status_code=404)

    proof = _merkle_proof(hashes, target_index)
    db.session.commit()
    return {
        "respuesta_id": respuesta_id,
        "snapshot_id": snapshot_id,
        "root_hash": snapshot.root_hash,
        "content_hash": hashes[target_index],
        "proof": proof,
    }
