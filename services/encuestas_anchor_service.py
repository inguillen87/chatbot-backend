"""Utilities to build cryptographic snapshots for survey responses."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import joinedload

from database import db
from models import EncAnchorSnapshot, EncRespuesta
from services.encuestas_service import EncuestaError, get_encuesta, _parse_datetime
from services.survey_response_provenance import (
    is_legacy_unverified_response,
    is_trusted_demo_seed_response,
    partition_survey_responses_by_origin,
)


ANCHOR_CONTRACT_VERSION = "surveys.anchor.v2"
LOCAL_INTEGRITY_SCOPE = "local_merkle_snapshot"


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


def _normalize_requested_chain(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "polygon").strip().lower())
    return (normalized.strip("-") or "polygon")[:24]


def _anchor_truth(snapshot: EncAnchorSnapshot) -> Dict[str, object]:
    """Describe only assurance that this backend can prove locally.

    Historical versions labelled generated ``SIM-*`` references as published.
    They never represented a provider receipt, so every such record is exposed as
    simulated and unverified even if the stored status still says ``published``.
    Other legacy publication claims are also downgraded to unverified until a
    future provider verifier can validate them.
    """

    stored_status = str(snapshot.anchor_status or "draft").strip().lower()
    tx_id = str(snapshot.tx_id or "").strip()
    chain = str(snapshot.chain or "").strip()
    simulated = (
        stored_status == "simulated"
        or tx_id.upper().startswith("SIM-")
        or chain.lower().startswith("simulation:")
    )
    if simulated:
        effective_status = "simulated"
    elif stored_status in {"draft", "failed"}:
        effective_status = stored_status
    else:
        # There is no blockchain/provider verification implementation in this
        # service. Never promote a stored claim to a verified/public status.
        effective_status = "unverified"

    return {
        "anchor_status": effective_status,
        "stored_anchor_status": stored_status,
        "is_simulated": simulated,
        "published": False,
        "externally_anchored": False,
        "externally_verified": False,
        "verification_status": "unverified",
        "integrity_scope": LOCAL_INTEGRITY_SCOPE,
        "assurance_notice": (
            "Prueba Merkle local. No acredita publicacion ni verificacion en una red externa."
        ),
    }


def serialize_anchor_snapshot(snapshot: EncAnchorSnapshot) -> Dict[str, object]:
    truth = _anchor_truth(snapshot)
    return {
        "contract_version": ANCHOR_CONTRACT_VERSION,
        "id": snapshot.id,
        # Compatibility alias used by the original creation endpoint.
        "snapshot_id": snapshot.id,
        "encuesta_id": snapshot.encuesta_id,
        "tenant_id": snapshot.tenant_id,
        "algo": snapshot.algo,
        "root_hash": snapshot.root_hash,
        "total_respuestas": snapshot.total_respuestas,
        "desde_at": snapshot.desde_at.isoformat(),
        "hasta_at": snapshot.hasta_at.isoformat(),
        "anchor_at": snapshot.anchor_at.isoformat() if snapshot.anchor_at else None,
        "tx_id": snapshot.tx_id,
        "chain": snapshot.chain,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "created_by": snapshot.created_by,
        **truth,
    }


def _get_scoped_snapshot(
    *,
    encuesta_id: int,
    snapshot_id: int,
    user: object,
) -> tuple[object, EncAnchorSnapshot]:
    """Resolve tenant first, then snapshot by tenant + survey + id.

    Keeping this lookup centralized prevents callers from accidentally using a
    globally enumerable ``snapshot_id`` as authorization.
    """

    encuesta = get_encuesta(encuesta_id, user=user)
    snapshot = (
        EncAnchorSnapshot.query.filter_by(
            id=snapshot_id,
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
        )
        .one_or_none()
    )
    if snapshot is None:
        raise EncuestaError("Snapshot no encontrado", status_code=404)
    return encuesta, snapshot


def compute_content_hash(encuesta_id: int, respuesta_id: int, user: object) -> str:
    encuesta = get_encuesta(encuesta_id, user=user)
    respuesta: EncRespuesta | None = (
        EncRespuesta.query.options(joinedload(EncRespuesta.detalles))
        .filter_by(
            id=respuesta_id,
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
        )
        .one_or_none()
    )
    if not respuesta:
        raise EncuestaError("Respuesta no encontrada", status_code=404)
    if is_trusted_demo_seed_response(respuesta, survey_id=encuesta.id):
        raise EncuestaError(
            "Las respuestas sinteticas no forman parte de la auditoria ciudadana",
            status_code=409,
            payload={
                "contract_version": ANCHOR_CONTRACT_VERSION,
                "reason_code": "anchor_synthetic_response_forbidden",
            },
        )
    if is_legacy_unverified_response(respuesta, survey_id=encuesta.id):
        raise EncuestaError(
            "La procedencia de la respuesta no está verificada para auditoría ciudadana",
            status_code=409,
            payload={
                "contract_version": ANCHOR_CONTRACT_VERSION,
                "reason_code": "anchor_unverified_response_forbidden",
            },
        )

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

    query = EncRespuesta.query.options(joinedload(EncRespuesta.detalles)).filter_by(
        encuesta_id=encuesta.id,
        tenant_id=tenant_id,
    )
    query = query.filter(EncRespuesta.submitted_at >= desde_dt, EncRespuesta.submitted_at <= hasta_dt)
    respuestas = query.order_by(EncRespuesta.submitted_at.asc(), EncRespuesta.id.asc()).all()
    if not respuestas:
        raise EncuestaError("No hay respuestas en el rango indicado", status_code=404)
    real_respuestas, synthetic_respuestas, unverified_respuestas = (
        partition_survey_responses_by_origin(
        respuestas,
        survey_id=encuesta.id,
        )
    )
    if synthetic_respuestas or unverified_respuestas:
        raise EncuestaError(
            "El rango contiene respuestas sinteticas y no puede auditarse como participacion ciudadana",
            status_code=409,
            payload={
                "contract_version": ANCHOR_CONTRACT_VERSION,
                "reason_code": "anchor_synthetic_responses_forbidden",
                "real_responses": len(real_respuestas),
                "synthetic_responses": len(synthetic_respuestas),
                "unverified_responses": len(unverified_respuestas),
            },
        )
    respuestas = real_respuestas

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


def simulate_snapshot_anchor(
    *,
    encuesta_id: int,
    snapshot_id: int,
    user: object,
    requested_chain: str = "polygon",
) -> EncAnchorSnapshot:
    """Record a local simulation without claiming external publication."""

    _, snapshot = _get_scoped_snapshot(
        encuesta_id=encuesta_id,
        snapshot_id=snapshot_id,
        user=user,
    )
    existing_tx_id = str(snapshot.tx_id or "").strip()
    if existing_tx_id and not existing_tx_id.upper().startswith("SIM-"):
        raise EncuestaError(
            "El snapshot contiene una referencia externa que requiere revision manual",
            status_code=409,
            payload={
                "contract_version": ANCHOR_CONTRACT_VERSION,
                "reason_code": "anchor_external_state_requires_review",
            },
        )

    chain = _normalize_requested_chain(requested_chain)
    snapshot.chain = f"simulation:{chain}"
    snapshot.anchor_status = "simulated"
    snapshot.anchor_at = snapshot.anchor_at or datetime.now(timezone.utc)
    snapshot.tx_id = existing_tx_id or f"SIM-{uuid.uuid4()}"
    db.session.commit()
    return snapshot


def publish_snapshot(
    encuesta_id: int,
    snapshot_id: int,
    user: object,
    chain: str = "polygon",
) -> EncAnchorSnapshot:
    """Compatibility wrapper; this operation is a local simulation only."""

    return simulate_snapshot_anchor(
        encuesta_id=encuesta_id,
        snapshot_id=snapshot_id,
        user=user,
        requested_chain=chain,
    )


def _verify_merkle_proof(content_hash: str, proof: Sequence[str], root_hash: str) -> bool:
    current_hash = content_hash
    for step in proof:
        side, separator, sibling_hash = str(step).partition(":")
        if not separator or side not in {"L", "R"} or not sibling_hash:
            return False
        combined = sibling_hash + current_hash if side == "L" else current_hash + sibling_hash
        current_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return current_hash == root_hash


def generate_merkle_proof(
    encuesta_id: int,
    snapshot_id: int,
    respuesta_id: int,
    user: object,
) -> Dict[str, object]:
    encuesta, snapshot = _get_scoped_snapshot(
        encuesta_id=encuesta_id,
        snapshot_id=snapshot_id,
        user=user,
    )

    respuestas = (
        EncRespuesta.query.options(joinedload(EncRespuesta.detalles))
        .filter_by(
            snapshot_id=snapshot.id,
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
        )
        .order_by(EncRespuesta.submitted_at.asc(), EncRespuesta.id.asc())
        .all()
    )
    _real_respuestas, synthetic_respuestas, unverified_respuestas = (
        partition_survey_responses_by_origin(
        respuestas,
        survey_id=encuesta.id,
        )
    )
    if synthetic_respuestas or unverified_respuestas:
        raise EncuestaError(
            "El snapshot contiene respuestas sinteticas y requiere revision",
            status_code=409,
            payload={
                "contract_version": ANCHOR_CONTRACT_VERSION,
                "reason_code": "anchor_snapshot_synthetic_responses_forbidden",
                "synthetic_responses": len(synthetic_respuestas),
                "unverified_responses": len(unverified_respuestas),
            },
        )
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
    local_proof_valid = _verify_merkle_proof(
        hashes[target_index],
        proof,
        snapshot.root_hash,
    )
    db.session.commit()
    return {
        "contract_version": ANCHOR_CONTRACT_VERSION,
        "respuesta_id": respuesta_id,
        "snapshot_id": snapshot_id,
        "encuesta_id": encuesta.id,
        "root_hash": snapshot.root_hash,
        "content_hash": hashes[target_index],
        "proof": proof,
        "included": True,
        "local_proof_valid": local_proof_valid,
        # Compatibility key deliberately remains false: local inclusion is not
        # external publication/verification.
        "valido": False,
        "verified": False,
        "externally_verified": False,
        "verification_status": "local_only" if local_proof_valid else "invalid",
        "integrity_scope": LOCAL_INTEGRITY_SCOPE,
        "assurance_notice": (
            "La respuesta esta incluida en este corte local; no se verifico una publicacion externa."
        ),
    }


def list_snapshots(encuesta_id: int, user: object) -> Dict[str, object]:
    """Return the anchor snapshots associated with a survey ordered by recency."""

    encuesta = get_encuesta(encuesta_id, user=user)
    snapshots = (
        EncAnchorSnapshot.query.filter_by(
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
        )
        .order_by(EncAnchorSnapshot.created_at.desc())
        .all()
    )

    return {
        "contract_version": ANCHOR_CONTRACT_VERSION,
        "encuesta_id": encuesta.id,
        "snapshots": [serialize_anchor_snapshot(snapshot) for snapshot in snapshots],
    }
