"""Canonical tenant resolution for the surveys subsystem.

Every persisted survey row is scoped by ``TenantProfile.id``.  Historical
owner/user identifiers may remain in ``TenantProfile.encuestas_tenant_id``
only as inbound compatibility aliases after the migration has physically
rewritten legacy rows.  An alias is never a storage namespace and is never
returned by :func:`resolve_survey_tenant_scope_id`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_

from models import TenantProfile


SURVEY_TENANT_SCOPE_CONTRACT_VERSION = "surveys.tenant_scope.v2"


class SurveyTenantScopeError(RuntimeError):
    """Raised when a tenant reference cannot be resolved without ambiguity."""

    def __init__(
        self,
        reason_code: str,
        *,
        tenant_id: int | None = None,
        candidate_scope_id: int | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.tenant_id = tenant_id
        self.candidate_scope_id = candidate_scope_id


def _positive_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized > 0 else None


def resolve_survey_tenant_scope_id(tenant: TenantProfile) -> int:
    """Return the one authoritative storage namespace for ``tenant``.

    Runtime reads and writes always use the canonical ``TenantProfile.id``.
    Legacy aliases are intentionally ignored here; the release migration owns
    the one-time physical data rewrite.
    """

    canonical_tenant_id = _positive_int(getattr(tenant, "id", None))
    if canonical_tenant_id is None:
        raise SurveyTenantScopeError("survey_tenant_scope_missing")
    return canonical_tenant_id


def resolve_survey_tenant_profile_reference(
    reference: Any,
    *,
    allow_legacy_owner: bool = False,
) -> TenantProfile:
    """Resolve a numeric tenant reference to exactly one profile.

    Canonical ids and explicit ``encuestas_tenant_id`` compatibility aliases
    are accepted.  Owner ids are supported only for explicitly labelled
    legacy transports and are resolved through ``TenantProfile`` rather than
    being used as storage ids.  Any collision fails closed.
    """

    candidate = _positive_int(reference)
    if candidate is None:
        raise SurveyTenantScopeError("survey_tenant_scope_invalid")

    predicate = or_(
        TenantProfile.id == candidate,
        TenantProfile.encuestas_tenant_id == candidate,
    )
    if allow_legacy_owner:
        predicate = or_(
            predicate,
            TenantProfile.municipio_id == candidate,
            TenantProfile.pyme_id == candidate,
        )

    matches = TenantProfile.query.filter(predicate).order_by(TenantProfile.id).limit(3).all()
    unique_matches = {int(row.id): row for row in matches}
    if not unique_matches:
        raise SurveyTenantScopeError(
            "survey_tenant_scope_missing",
            candidate_scope_id=candidate,
        )
    if len(unique_matches) != 1:
        raise SurveyTenantScopeError(
            "survey_tenant_scope_ambiguous",
            candidate_scope_id=candidate,
        )
    return next(iter(unique_matches.values()))


def resolve_survey_tenant_profile_slug(slug: Any) -> TenantProfile:
    """Resolve a public tenant slug to exactly one canonical profile."""

    normalized = str(slug or "").strip().lower()
    if not normalized or len(normalized) > 80:
        raise SurveyTenantScopeError("survey_tenant_scope_invalid")
    matches = TenantProfile.query.filter_by(slug=normalized).limit(2).all()
    if not matches:
        raise SurveyTenantScopeError("survey_tenant_scope_missing")
    if len(matches) != 1:
        raise SurveyTenantScopeError("survey_tenant_scope_ambiguous")
    return matches[0]


def resolve_survey_storage_tenant_profile(scope_id: Any) -> TenantProfile:
    """Resolve a persisted survey scope and prove it is canonical.

    Realtime workers and socket rooms consume identifiers read from survey
    rows, not inbound compatibility references.  A canonical primary-key match
    is therefore authoritative and must not be made ambiguous by an unrelated
    profile whose legacy owner id happens to use the same integer.  Alias/owner
    lookup is used only to classify a non-canonical stored value and fail it
    closed.
    """

    candidate = _positive_int(scope_id)
    if candidate is None:
        raise SurveyTenantScopeError("survey_tenant_scope_invalid")

    canonical_profile = TenantProfile.query.filter(
        TenantProfile.id == candidate
    ).first()
    if canonical_profile is not None:
        return canonical_profile

    profile = resolve_survey_tenant_profile_reference(
        candidate,
        allow_legacy_owner=True,
    )
    if int(profile.id) != candidate:
        raise SurveyTenantScopeError(
            "survey_tenant_storage_scope_not_canonical",
            tenant_id=int(profile.id),
            candidate_scope_id=candidate,
        )
    return profile
