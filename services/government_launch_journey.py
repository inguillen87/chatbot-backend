"""Safe operational materialization for reusable government blueprints.

The first launch slice deliberately materializes only tenant-scoped ticket
categories.  It never provisions providers, people, tickets, coordinates, or
demo records.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from extensions import db
from models import CategoriaTicket, TenantProfile
from models_government_launch import TenantBlueprintLaunchReceipt
from models_tenant_blueprints import TenantBlueprintApplication
from services.tenant_blueprints import (
    TenantBlueprintError,
    get_blueprint,
    load_blueprint,
)


BLUEPRINT_ID = "government-core"
LAUNCH_ID = "mesa-unica"
PREVIEW_CONTRACT_VERSION = "tenant.blueprint.launch.preview.v1"
APPLY_CONTRACT_VERSION = "tenant.blueprint.launch.apply.v1"

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LAUNCH_REGISTRY: dict[tuple[str, str], dict[str, Any]] = {
    (BLUEPRINT_ID, LAUNCH_ID): {
        "label": "Mesa Unica",
        "description": "Materializa categorias operativas sin crear personas, casos ni canales.",
        "source_path": "configuration_defaults.ticket_categories",
        "runtime_scope": ["ticket_categories"],
    }
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def canonicalize_category_name(value: Any) -> str:
    """Normalize only for comparison; persisted existing labels stay untouched."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split()).casefold()


def _display_category_name(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _launch_definition(blueprint_id: str, launch_id: str) -> dict[str, Any]:
    definition = _LAUNCH_REGISTRY.get((blueprint_id, launch_id))
    if definition is None:
        raise TenantBlueprintError(
            "blueprint_launch_not_found",
            "El recorrido operativo solicitado no existe.",
            404,
        )
    return deepcopy(definition)


def _require_applied_blueprint(
    tenant: TenantProfile,
    *,
    blueprint_id: str,
) -> tuple[dict[str, Any], str, TenantBlueprintApplication]:
    manifest, manifest_digest = load_blueprint(blueprint_id)
    # Reuse the authoritative eligibility checks from the blueprint service.
    get_blueprint(tenant, blueprint_id)
    receipt = TenantBlueprintApplication.query.filter_by(
        tenant_id=tenant.id,
        blueprint_id=manifest["blueprint_id"],
        blueprint_version=manifest["version"],
    ).one_or_none()
    if receipt is None:
        raise TenantBlueprintError(
            "blueprint_not_applied",
            "Aplica el blueprint antes de preparar la Mesa Unica.",
            409,
        )
    if receipt.manifest_digest != manifest_digest:
        raise TenantBlueprintError(
            "blueprint_application_stale",
            "La aplicacion del blueprint no coincide con el manifiesto vigente.",
            409,
        )
    return manifest, manifest_digest, receipt


def _category_ref(category: CategoriaTicket) -> dict[str, Any]:
    return {
        "id": category.id,
        "name": category.nombre,
        "type": category.tipo,
    }


def _build_category_projection(
    tenant: TenantProfile,
    *,
    manifest: Mapping[str, Any],
    manifest_digest: str,
    blueprint_application: TenantBlueprintApplication,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    desired_names = [
        _display_category_name(item)
        for item in manifest["configuration_defaults"]["ticket_categories"]
    ]
    desired_keys = [canonicalize_category_name(item) for item in desired_names]
    if not all(desired_keys) or len(desired_keys) != len(set(desired_keys)):
        raise TenantBlueprintError(
            "invalid_blueprint_launch_categories",
            "El blueprint no contiene categorias operativas univocas.",
            500,
        )

    existing = (
        CategoriaTicket.query.filter_by(tenant_id=tenant.id)
        .order_by(CategoriaTicket.id.asc())
        .all()
    )
    existing_by_key: dict[str, list[CategoriaTicket]] = {}
    for category in existing:
        key = canonicalize_category_name(category.nombre)
        if key:
            existing_by_key.setdefault(key, []).append(category)

    to_create: list[dict[str, Any]] = []
    already_present: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for desired_name, desired_key in zip(desired_names, desired_keys):
        matches = existing_by_key.get(desired_key, [])
        if not matches:
            to_create.append(
                {"name": desired_name, "canonical_name": desired_key, "type": "ticket"}
            )
            continue
        if len(matches) > 1:
            conflicts.append(
                {
                    "desired_name": desired_name,
                    "canonical_name": desired_key,
                    "reason_code": "ambiguous_existing_categories",
                    "matches": [_category_ref(item) for item in matches],
                }
            )
            continue
        match = matches[0]
        if canonicalize_category_name(match.tipo) != "ticket":
            conflicts.append(
                {
                    "desired_name": desired_name,
                    "canonical_name": desired_key,
                    "reason_code": "category_type_conflict",
                    "matches": [_category_ref(match)],
                }
            )
            continue
        already_present.append(
            {
                "desired_name": desired_name,
                "canonical_name": desired_key,
                "category": _category_ref(match),
                "preserved": True,
            }
        )

    existing_fingerprint = [
        {
            "id": category.id,
            "name": _display_category_name(category.nombre),
            "canonical_name": canonicalize_category_name(category.nombre),
            "type": canonicalize_category_name(category.tipo),
        }
        for category in existing
    ]
    digest_input = {
        "contract_version": PREVIEW_CONTRACT_VERSION,
        "tenant_id": tenant.id,
        "blueprint_application_id": blueprint_application.id,
        "blueprint_id": manifest["blueprint_id"],
        "blueprint_version": manifest["version"],
        "manifest_digest": manifest_digest,
        "launch_id": LAUNCH_ID,
        "source_path": definition["source_path"],
        "desired_categories": [
            {"name": name, "canonical_name": key}
            for name, key in zip(desired_names, desired_keys)
        ],
        "existing_categories": existing_fingerprint,
    }
    return {
        "launch_digest": _digest(digest_input),
        "to_create": to_create,
        "already_present": already_present,
        "conflicts": conflicts,
        "existing_count": len(existing),
        "desired_count": len(desired_names),
    }


def _preview_payload(
    tenant: TenantProfile,
    *,
    manifest: Mapping[str, Any],
    manifest_digest: str,
    blueprint_application: TenantBlueprintApplication,
    definition: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": PREVIEW_CONTRACT_VERSION,
        "tenant": {"id": tenant.id, "slug": tenant.slug, "type": tenant.tipo},
        "blueprint": {
            "id": manifest["blueprint_id"],
            "version": manifest["version"],
            "manifest_digest": manifest_digest,
            "application_receipt_id": blueprint_application.id,
        },
        "launch": {
            "id": LAUNCH_ID,
            "label": definition["label"],
            "description": definition["description"],
            "source_path": definition["source_path"],
            "runtime_scope": list(definition["runtime_scope"]),
        },
        "launch_digest": projection["launch_digest"],
        "changes": {
            "to_create": deepcopy(projection["to_create"]),
            "already_present": deepcopy(projection["already_present"]),
            "conflicts": deepcopy(projection["conflicts"]),
            "summary": {
                "desired": projection["desired_count"],
                "existing": projection["existing_count"],
                "create": len(projection["to_create"]),
                "preserve": len(projection["already_present"]),
                "conflict": len(projection["conflicts"]),
            },
        },
        "write_performed": False,
        "runtime_activation_performed": False,
        "operational_defaults_materialized": False,
        "provider_activation_performed": False,
        "external_calls_performed": False,
        "demo_data_created": False,
    }


def preview_mesa_unica_launch(tenant: TenantProfile) -> dict[str, Any]:
    definition = _launch_definition(BLUEPRINT_ID, LAUNCH_ID)
    manifest, manifest_digest, application = _require_applied_blueprint(
        tenant, blueprint_id=BLUEPRINT_ID
    )
    projection = _build_category_projection(
        tenant,
        manifest=manifest,
        manifest_digest=manifest_digest,
        blueprint_application=application,
        definition=definition,
    )
    return _preview_payload(
        tenant,
        manifest=manifest,
        manifest_digest=manifest_digest,
        blueprint_application=application,
        definition=definition,
        projection=projection,
    )


def _validate_apply_inputs(expected_launch_digest: str, idempotency_key: str) -> None:
    if not _DIGEST_PATTERN.fullmatch(expected_launch_digest):
        raise TenantBlueprintError(
            "invalid_launch_digest", "launch_digest no es valido.", 400
        )
    if not 8 <= len(idempotency_key) <= 128 or any(
        ord(char) < 33 or ord(char) > 126 for char in idempotency_key
    ):
        raise TenantBlueprintError(
            "invalid_idempotency_key", "Idempotency-Key no es valido.", 400
        )


def _serialize_apply(
    receipt: TenantBlueprintLaunchReceipt,
    *,
    replayed: bool,
) -> dict[str, Any]:
    snapshot = (
        receipt.application_snapshot
        if isinstance(receipt.application_snapshot, dict)
        else {}
    )
    return {
        "contract_version": APPLY_CONTRACT_VERSION,
        "receipt": receipt.to_dict(),
        "launch_digest": receipt.launch_digest,
        "changes": deepcopy(snapshot.get("changes") or {}),
        "replayed": replayed,
        "write_performed": not replayed,
        # Categories prepare the service desk contract, but no live channel,
        # provider, routing rule, or worker is activated by this slice.
        "runtime_activation_performed": False,
        "runtime_activation_scope": ["ticket_categories"],
        "operational_defaults_materialized": True,
        "provider_activation_performed": False,
        "external_calls_performed": False,
        "demo_data_created": False,
    }


def _existing_receipt_or_conflict(
    receipt: TenantBlueprintLaunchReceipt | None,
    *,
    request_digest: str,
    reason_code: str,
) -> dict[str, Any] | None:
    if receipt is None:
        return None
    if receipt.request_digest == request_digest:
        return _serialize_apply(receipt, replayed=True)
    raise TenantBlueprintError(
        reason_code,
        "La solicitud entra en conflicto con un recibo operativo existente.",
        409,
    )


def apply_mesa_unica_launch(
    tenant: TenantProfile,
    *,
    actor_user_id: int,
    expected_launch_digest: str,
    idempotency_key: str,
) -> dict[str, Any]:
    expected_launch_digest = str(expected_launch_digest or "").strip().lower()
    idempotency_key = str(idempotency_key or "").strip()
    _validate_apply_inputs(expected_launch_digest, idempotency_key)
    definition = _launch_definition(BLUEPRINT_ID, LAUNCH_ID)

    # This row lock serializes category discovery and materialization without a
    # legacy-breaking global/category unique constraint.
    locked_tenant = (
        TenantProfile.query.filter_by(id=tenant.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    manifest, manifest_digest, application = _require_applied_blueprint(
        locked_tenant, blueprint_id=BLUEPRINT_ID
    )
    tenant = locked_tenant

    idempotency_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_digest = _digest(
        {
            "tenant_id": tenant.id,
            "blueprint_application_id": application.id,
            "blueprint_id": manifest["blueprint_id"],
            "blueprint_version": manifest["version"],
            "manifest_digest": manifest_digest,
            "launch_id": LAUNCH_ID,
            "launch_digest": expected_launch_digest,
        }
    )

    existing = TenantBlueprintLaunchReceipt.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key_hash=idempotency_hash,
    ).one_or_none()
    replay = _existing_receipt_or_conflict(
        existing,
        request_digest=request_digest,
        reason_code="launch_idempotency_key_conflict",
    )
    if replay:
        return replay

    existing = TenantBlueprintLaunchReceipt.query.filter_by(
        tenant_id=tenant.id,
        blueprint_id=manifest["blueprint_id"],
        blueprint_version=manifest["version"],
        launch_id=LAUNCH_ID,
    ).one_or_none()
    replay = _existing_receipt_or_conflict(
        existing,
        request_digest=request_digest,
        reason_code="blueprint_launch_version_conflict",
    )
    if replay:
        return replay

    projection = _build_category_projection(
        tenant,
        manifest=manifest,
        manifest_digest=manifest_digest,
        blueprint_application=application,
        definition=definition,
    )
    if projection["launch_digest"] != expected_launch_digest:
        raise TenantBlueprintError(
            "launch_digest_mismatch",
            "La preparacion cambio; ejecuta preview nuevamente.",
            409,
        )
    if projection["conflicts"]:
        raise TenantBlueprintError(
            "launch_category_conflict",
            "Existen categorias ambiguas o incompatibles; revisalas antes de aplicar.",
            409,
            details={"conflict_count": len(projection["conflicts"])},
        )

    created_models: list[CategoriaTicket] = []
    for item in projection["to_create"]:
        category = CategoriaTicket(
            tenant_id=tenant.id,
            nombre=item["name"],
            tipo="ticket",
        )
        db.session.add(category)
        created_models.append(category)

    try:
        db.session.flush()
        changes = {
            "created": [_category_ref(item) for item in created_models],
            "already_present": deepcopy(projection["already_present"]),
            "conflicts": [],
            "summary": {
                "desired": projection["desired_count"],
                "created": len(created_models),
                "preserved": len(projection["already_present"]),
                "conflict": 0,
            },
        }
        receipt = TenantBlueprintLaunchReceipt(
            tenant_id=tenant.id,
            blueprint_application_id=application.id,
            blueprint_id=manifest["blueprint_id"],
            blueprint_version=manifest["version"],
            launch_id=LAUNCH_ID,
            manifest_digest=manifest_digest,
            launch_digest=expected_launch_digest,
            request_digest=request_digest,
            idempotency_key_hash=idempotency_hash,
            status="applied",
            application_snapshot={
                "launch": {
                    "id": LAUNCH_ID,
                    "source_path": definition["source_path"],
                    "runtime_scope": list(definition["runtime_scope"]),
                },
                "changes": changes,
                "operational_defaults_materialized": True,
                "provider_activation_performed": False,
                "external_calls_performed": False,
                "demo_data_created": False,
            },
            applied_by_user_id=actor_user_id,
        )
        db.session.add(receipt)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        collision = TenantBlueprintLaunchReceipt.query.filter_by(
            tenant_id=tenant.id,
            idempotency_key_hash=idempotency_hash,
        ).one_or_none()
        if collision is None:
            collision = TenantBlueprintLaunchReceipt.query.filter_by(
                tenant_id=tenant.id,
                blueprint_id=manifest["blueprint_id"],
                blueprint_version=manifest["version"],
                launch_id=LAUNCH_ID,
            ).one_or_none()
        replay = _existing_receipt_or_conflict(
            collision,
            request_digest=request_digest,
            reason_code="blueprint_launch_apply_conflict",
        )
        if replay:
            return replay
        raise TenantBlueprintError(
            "blueprint_launch_apply_conflict",
            "No se pudo aplicar la preparacion operativa.",
            409,
        ) from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise TenantBlueprintError(
            "blueprint_launch_unavailable",
            "No se pudo preparar la Mesa Unica de forma atomica.",
            503,
        ) from exc
    except Exception:
        db.session.rollback()
        raise

    return _serialize_apply(receipt, replayed=False)


__all__ = [
    "APPLY_CONTRACT_VERSION",
    "BLUEPRINT_ID",
    "LAUNCH_ID",
    "PREVIEW_CONTRACT_VERSION",
    "apply_mesa_unica_launch",
    "canonicalize_category_name",
    "preview_mesa_unica_launch",
]
