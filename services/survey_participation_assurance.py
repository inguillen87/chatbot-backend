"""Opt-in admission assurance; client identifiers never prove a person.

No historical fingerprints or response rows are changed. Restricted grants
remain the existing operator-reviewed contract, not phone ownership evidence.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from flask import current_app


ACCOUNT_POLICIES = frozenset({"por_usuario", "usuario", "user_id", "por_user_id"})
_REVIEWED_MODES = frozenset({"manual_review", "institution_attested"})


class ParticipationAssuranceError(Exception):
    def __init__(self, reason_code: str, *, status_code: int = 409):
        self.reason_code = reason_code
        self.status_code = status_code
        self.message = (
            "La proteccion de participacion requiere configuracion institucional."
            if status_code == 503
            else "Esta consulta requiere una cuenta verificada o una credencial revisada."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": "surveys.participation_assurance.v1",
            "reason_code": self.reason_code,
            "action_hint": "review_participation_configuration",
            "retryable": False,
        }


def strict_participation_enabled(
    tenant_id: Any, *, config: Mapping[str, Any] | None = None
) -> bool:
    config = current_app.config if config is None else config
    enabled = config.get("ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1")
    if enabled is not True and str(enabled or "").strip().lower() not in {"1", "true"}:
        return False
    raw = str(config.get("SURVEY_PARTICIPATION_ASSURANCE_TENANT_IDS") or "").strip()
    tokens = [part.strip() for part in raw.split(",")]
    if not raw or any(not re.fullmatch(r"[1-9][0-9]*", part) for part in tokens):
        raise ParticipationAssuranceError(
            "survey_participation_assurance_misconfigured", status_code=503
        )
    try:
        target = int(tenant_id)
    except (TypeError, ValueError):
        raise ParticipationAssuranceError(
            "survey_participation_assurance_misconfigured", status_code=503
        ) from None
    return target in {int(part) for part in tokens}


def reviewed_grant_fingerprint(encuesta: Any, release: Any, grant: Any) -> str:
    """Called only after lock_eligibility_grant_for_submission has validated.

    Its subject HMAC already belongs to the existing reviewed authority. The
    release scope is deliberate: this does not certify one human across releases.
    No client DNI, phone, IP or cookie participates in this fingerprint.
    """
    if (
        release is None
        or grant is None
        or grant.tenant_id != encuesta.tenant_id
        or grant.survey_id != encuesta.id
        or grant.release_id != release.id
        or grant.eligibility_mode not in _REVIEWED_MODES
        or not re.fullmatch(r"[0-9a-f]{64}", str(grant.subject_hmac or ""))
    ):
        raise ParticipationAssuranceError("survey_authoritative_participation_required")
    material = (
        f"survey-reviewed-subject-v1\x1f{encuesta.tenant_id}\x1f{encuesta.id}"
        f"\x1f{release.id}\x1f{grant.subject_hmac}"
    ).encode("ascii")
    return "reviewed-subject-hmac-v1:" + hashlib.sha256(material).hexdigest()


def participation_assurance_contract(
    encuesta: Any, governance: Mapping[str, Any] | None
) -> dict[str, Any]:
    policy = str(encuesta.politica_unicidad or "libre").strip().lower()
    eligibility = governance.get("eligibility") if isinstance(governance, Mapping) else None
    reviewed = bool(
        isinstance(eligibility, Mapping)
        and eligibility.get("mode") in _REVIEWED_MODES
        and eligibility.get("credential_required") is True
        and eligibility.get("intake_available") is True
        and eligibility.get("gate_status") == "ready"
    )
    account = policy in ACCOUNT_POLICIES
    blocked_reason = None
    try:
        strict = strict_participation_enabled(encuesta.tenant_id)
    except ParticipationAssuranceError as exc:
        strict = True
        blocked_reason = exc.reason_code
    if strict and not account and not reviewed and blocked_reason is None:
        blocked_reason = "survey_authoritative_participation_required"
    warnings = []
    if account:
        warnings.append("account_is_not_unique_person")
    if reviewed:
        warnings.append("reviewed_subject_is_operator_attested_not_phone_verified")
    if not account and not reviewed:
        warnings.append("client_identifiers_do_not_prove_unique_person")
    if policy in {"por_dni_o_phone", "dni_o_phone"} and not strict:
        warnings.append("legacy_combined_fingerprint_is_not_independent_or_match")
    if blocked_reason is not None:
        ui = {
            "title": "Control de participación",
            "label": "Participación bloqueada",
            "description": "La configuración necesita una cuenta con sesión verificada o una credencial institucional revisada.",
            "limitation": "No se admite participación nueva hasta revisar esta configuración.",
        }
    elif account:
        ui = {
            "title": "Control de participación",
            "label": "Una cuenta por encuesta",
            "description": "La sesión autentica una cuenta existente. Cambiar teléfono, IP o cookie no habilita otra participación.",
            "limitation": "Una cuenta no certifica una persona única.",
        }
    elif reviewed:
        ui = {
            "title": "Control de participación",
            "label": "Credencial institucional revisada",
            "description": "Al responder se valida una credencial institucional de uso único para esta versión de la consulta.",
            "limitation": "No acredita propiedad del teléfono ni certifica elecciones o resultados.",
        }
    else:
        ui = {
            "title": "Control de participación",
            "label": "Control básico",
            "description": "La unicidad usa datos declarados o del navegador. Pueden cambiarse y permitir participaciones repetidas.",
            "limitation": "No prueba que cada respuesta provenga de una persona distinta.",
        }
    return {
        "contract_version": "surveys.participation_assurance.v1",
        "strict_mode": strict,
        "configuration_ready": blocked_reason is None,
        "blocked_reason_code": blocked_reason,
        "uniqueness_policy": policy,
        "required_proof": (
            "verified_existing_account" if account
            else "reviewed_opaque_grant" if strict or reviewed
            else "legacy_client_identifier"
        ),
        "uniqueness_scope": (
            "account_per_survey" if account
            else "reviewed_subject_per_release" if strict and reviewed
            else "legacy_policy"
        ),
        "credential_validation": "submission_time" if reviewed else None,
        "phone_ownership_verified": False,
        "cookie_is_person_identity": False,
        "ip_is_person_identity": False,
        "unique_person_certified": False,
        "result_certified": False,
        "historical_rows_reassessed": False,
        "recommended_strict_policy": "por_usuario_or_reviewed_opaque_grant",
        "warnings": warnings,
        "ui": ui,
    }
