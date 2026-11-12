from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unicodedata
from typing import Any, Mapping

from database import db
from models import EncLink, TenantProfile
from services.territorial_evidence import (
    coordinate_jurisdiction_status,
    resolve_tenant_jurisdiction,
)


JUNIN_QA_LAT = -33.136
JUNIN_QA_LNG = -68.49
JUNIN_QA_ADDRESS = "Ubicación sintética QA, Junín, Mendoza"

_GEO_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "municipios" / "junin" / "geo.json"
)
_GEO_CONFIG = json.loads(_GEO_CONFIG_PATH.read_text(encoding="utf-8"))
_BOUNDARY = _GEO_CONFIG["boundary"]
_AUTHORITY = _BOUNDARY["authority"]

if (
    _GEO_CONFIG.get("city") != "Junín"
    or _GEO_CONFIG.get("state") != "Mendoza"
    or _GEO_CONFIG.get("country") != "AR"
    or _AUTHORITY.get("department_code") != "09"
):
    raise RuntimeError("El fixture E2E de Junín ya no coincide con geo.json")

JUNIN_JURISDICTION_REF = "ar:mendoza:departamento:09"
JUNIN_BOUNDARY_SHA256 = str(_BOUNDARY["snapshot_sha256"])
JUNIN_JURISDICTION_EVIDENCE_REF = f"geo-snapshot:{JUNIN_BOUNDARY_SHA256}"


def mark_junin_jurisdiction_verified(
    tenant: TenantProfile,
    *,
    reviewer_user_id: int,
) -> None:
    """Install the repository-backed Junín boundary as an already reviewed test precondition."""

    jurisdiction = resolve_tenant_jurisdiction(tenant)
    if (
        jurisdiction.get("containment_verified") is not True
        or (jurisdiction.get("source") or {}).get("ref")
        != "municipios/junin/geo.json"
        or coordinate_jurisdiction_status(
            JUNIN_QA_LAT,
            JUNIN_QA_LNG,
            jurisdiction,
        )
        != "within"
    ):
        raise AssertionError(
            "El tenant de prueba no resolvió el límite oficial versionado de Junín, Mendoza"
        )

    tenant.jurisdiction_status = "verified"
    tenant.jurisdiction_ref = JUNIN_JURISDICTION_REF
    tenant.jurisdiction_evidence_ref = JUNIN_JURISDICTION_EVIDENCE_REF
    tenant.jurisdiction_verified_by_user_id = int(reviewer_user_id)
    tenant.jurisdiction_verified_at = datetime.now(timezone.utc)
    db.session.add(tenant)
    db.session.commit()


def _release_policy() -> dict[str, Any]:
    public_text = (
        "Acepto el tratamiento agregado de mi respuesta para esta consulta "
        "institucional no vinculante."
    )
    normalized_text = unicodedata.normalize(
        "NFC", public_text.replace("\r\n", "\n").replace("\r", "\n")
    ).strip(" \n")
    return {
        "eligibility_policy": {
            "policy_version": "eligibility-junin-qa-2026.1",
            "mode": "open",
            "declarations": [],
            "human_review_required": True,
            "automated_decision": False,
        },
        "consent_policy": {
            "policy_version": "consent-junin-qa-2026.1",
            "public_text": public_text,
            "text_sha256": hashlib.sha256(
                normalized_text.encode("utf-8")
            ).hexdigest(),
            "required": True,
        },
        "decision_rules": {
            "quorum": {"type": "none", "value": None},
            "tie": {"procedure": "human_review"},
            "challenge": {
                "enabled": True,
                "window_hours": 72,
                "procedure": "human_review",
            },
            "human_review_required": True,
            "declarative_only": True,
        },
    }


def _expect_json(response: Any, status_code: int, *, step: str) -> dict[str, Any]:
    payload = response.get_json()
    if response.status_code != status_code:
        raise AssertionError(
            f"{step}: HTTP {response.status_code}, esperado {status_code}: {payload}"
        )
    if not isinstance(payload, dict):
        raise AssertionError(f"{step}: la respuesta no es un objeto JSON")
    return payload


@dataclass(frozen=True)
class GovernedSurveyPublication:
    release_id: int
    snapshot_sha256: str
    public_token: str
    public_payload: dict[str, Any]
    response_contract: dict[str, Any]


def publish_governed_junin_survey(
    client: Any,
    *,
    survey_id: int,
    tenant: TenantProfile,
    reviewer_user_id: int,
    headers: Mapping[str, str],
    idempotency_prefix: str,
) -> GovernedSurveyPublication:
    """Exercise bind -> reload hash -> approve -> release -> publish over HTTP."""

    mark_junin_jurisdiction_verified(
        tenant,
        reviewer_user_id=reviewer_user_id,
    )
    base_headers = dict(headers)

    initial = _expect_json(
        client.get(
            f"/api/v2/surveys/{survey_id}/content-review",
            headers=base_headers,
        ),
        200,
        step="load survey review before bind",
    )
    before_hash = initial["jurisdiction"]["content_sha256"]
    if initial["jurisdiction"].get("survey_jurisdiction_ref") is not None:
        raise AssertionError("La encuesta debía comenzar sin jurisdicción vinculada")

    bound = _expect_json(
        client.post(
            f"/api/v2/surveys/{survey_id}/content-review",
            json={
                "decision": "bind",
                "expected_content_sha256": before_hash,
                "evidence_ref": f"qa-review:{idempotency_prefix}",
            },
            headers={
                **base_headers,
                "Idempotency-Key": f"{idempotency_prefix}:bind:0001",
            },
        ),
        201,
        step="bind verified Junín jurisdiction",
    )
    if bound.get("action_hint") != "reload_jurisdiction_readiness_then_review":
        raise AssertionError("El bind no exigió recargar el hash de revisión")
    if bound["jurisdiction"]["content_sha256"] == before_hash:
        raise AssertionError("El bind debía producir un nuevo hash de contenido")

    reloaded = _expect_json(
        client.get(
            f"/api/v2/surveys/{survey_id}/content-review",
            headers=base_headers,
        ),
        200,
        step="reload survey review after bind",
    )
    reloaded_hash = reloaded["jurisdiction"]["content_sha256"]
    if reloaded_hash != bound["jurisdiction"]["content_sha256"]:
        raise AssertionError("La recarga no devolvió el hash generado por el bind")

    approved = _expect_json(
        client.post(
            f"/api/v2/surveys/{survey_id}/content-review",
            json={
                "decision": "approve",
                "expected_content_sha256": reloaded_hash,
                "evidence_ref": f"qa-review:{idempotency_prefix}",
            },
            headers={
                **base_headers,
                "Idempotency-Key": f"{idempotency_prefix}:approve:0001",
            },
        ),
        201,
        step="approve exact survey content hash",
    )
    if approved.get("review_completed") is not True:
        raise AssertionError("La revisión humana exacta no quedó aprobada")
    if approved["jurisdiction"].get("ready") is not True:
        raise AssertionError("La encuesta revisada no quedó lista para publicar")

    release = _expect_json(
        client.post(
            f"/api/v2/surveys/{survey_id}/releases",
            json=_release_policy(),
            headers={
                **base_headers,
                "Idempotency-Key": f"{idempotency_prefix}:release:0001",
            },
        ),
        201,
        step="create governed survey release",
    )
    release_id = int(release["release_id"])
    snapshot_sha256 = str(release["snapshot_sha256"])

    published = _expect_json(
        client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/publish",
            json={"expected_snapshot_sha256": snapshot_sha256},
            headers={
                **base_headers,
                "Idempotency-Key": f"{idempotency_prefix}:publish:0001",
            },
        ),
        200,
        step="publish governed survey release",
    )
    if published.get("status") != "published":
        raise AssertionError("El release gobernado no quedó publicado")

    link = EncLink.query.filter_by(encuesta_id=int(survey_id)).one()
    if int(link.encuesta.tenant_id) != int(tenant.id):
        raise AssertionError("El enlace público no pertenece al tenant Junín de prueba")
    public_token = str(link.slug_publico)
    public_payload = _expect_json(
        client.get(f"/api/v2/public/surveys/{public_token}"),
        200,
        step="load public governed survey",
    )
    response_contract = {
        "instrument_revision": public_payload["instrument_revision"],
        "governance": {
            "release_id": release_id,
            "snapshot_sha256": snapshot_sha256,
            "eligibility_policy_version": "eligibility-junin-qa-2026.1",
            "consent_policy_version": "consent-junin-qa-2026.1",
            "eligibility_acknowledged": True,
            "consent_accepted": True,
        },
    }
    return GovernedSurveyPublication(
        release_id=release_id,
        snapshot_sha256=snapshot_sha256,
        public_token=public_token,
        public_payload=public_payload,
        response_contract=response_contract,
    )
