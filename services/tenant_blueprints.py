from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from extensions import db
from models import TenantProfile
from models_tenant_blueprints import TenantBlueprintApplication
from utils.roles import normalize_tenant_type


MANIFEST_CONTRACT_VERSION = "tenant.blueprint.manifest.v1"
DETAIL_CONTRACT_VERSION = "tenant.blueprint.detail.v1"
PREVIEW_CONTRACT_VERSION = "tenant.blueprint.preview.v1"
APPLY_CONTRACT_VERSION = "tenant.blueprint.apply.v1"
_MANIFEST_ROOT = Path(__file__).resolve().parents[1] / "data" / "tenant_blueprints"
_BLUEPRINT_REGISTRY = {
    "government-core": "government-core.v1.json",
    "government-disability-support": "government-disability-support.v1.json",
}
_BLUEPRINT_TOP_LEVEL_KEYS = {
    "contract_version",
    "blueprint_id",
    "version",
    "label",
    "description",
    "supported_tenant_types",
    "modules",
    "configuration_defaults",
}
_CONFIGURATION_KEYS = {
    "operating_model",
    "ticket_categories",
    "employee_routing",
    "channels",
    "accessibility_policy",
    "module_configuration",
    "data_governance",
}
_MODULE_IDS = {
    "service_desk",
    "crm",
    "claims",
    "participation",
    "territory",
    "knowledge",
    "channels",
    "accessibility",
}
_ACTIVATION_STATES = {
    "configuration_required",
    "evidence_required",
    "content_required",
    "provider_required",
    "validation_required",
}
_FORBIDDEN_KEYS = {
    "capabilities",
    "capabilities_json",
    "features",
    "feature_flags",
    "secrets",
    "credentials",
    "token",
    "tokens",
    "api_key",
    "access_token",
    "refresh_token",
    "sender_id",
    "phone",
    "phone_number",
    "domain",
    "dominio",
    "provider_account_id",
    "jurisdiction_ref",
    "jurisdiction_status",
    "jurisdiction_evidence_ref",
    "coordinates",
    "coordenadas",
    "lat",
    "lng",
    "latitude",
    "longitude",
}
_FORBIDDEN_KEY_FRAGMENTS = {
    "secret",
    "credential",
    "password",
    "token",
    "api_key",
    "sender",
    "phone",
    "domain",
    "jurisdiction",
    "coordinate",
    "latitude",
    "longitude",
}
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class TenantBlueprintError(Exception):
    def __init__(
        self,
        reason_code: str,
        message: str,
        status_code: int,
        *,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_keys(value: Mapping[str, Any], expected: set[str], *, location: str) -> None:
    actual = set(value)
    if actual != expected:
        raise TenantBlueprintError(
            "invalid_blueprint_manifest",
            "El manifiesto del blueprint no cumple el contrato esperado.",
            500,
            details={"location": location},
        )


def _walk_forbidden(value: Any, *, location: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            if key in _FORBIDDEN_KEYS or any(
                fragment in key for fragment in _FORBIDDEN_KEY_FRAGMENTS
            ):
                raise TenantBlueprintError(
                    "unsafe_blueprint_manifest",
                    "El manifiesto contiene configuracion reservada.",
                    500,
                    details={"location": f"{location}.{key}"},
                )
            _walk_forbidden(nested, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _walk_forbidden(nested, location=f"{location}[{index}]")
    elif isinstance(value, str) and re.search(r"https?://", value, flags=re.IGNORECASE):
        raise TenantBlueprintError(
            "unsafe_blueprint_manifest",
            "El manifiesto no puede incorporar destinos externos.",
            500,
            details={"location": location},
        )


def _validate_manifest(manifest: Any, *, expected_blueprint_id: str) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "El manifiesto no es valido.", 500
        )
    _require_keys(manifest, _BLUEPRINT_TOP_LEVEL_KEYS, location="manifest")
    _walk_forbidden(manifest)

    if (
        manifest.get("contract_version") != MANIFEST_CONTRACT_VERSION
        or manifest.get("blueprint_id") != expected_blueprint_id
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", expected_blueprint_id)
        or not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version") or ""))
    ):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "El manifiesto no es valido.", 500
        )

    supported_types = manifest.get("supported_tenant_types")
    if not isinstance(supported_types, list) or not supported_types:
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "El manifiesto no declara tipos de tenant.", 500
        )
    if any(not isinstance(item, str) or not item.strip() for item in supported_types):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "El manifiesto declara tipos invalidos.", 500
        )
    if {
        normalize_tenant_type(item, default="") for item in supported_types
    } != {"municipio"}:
        raise TenantBlueprintError(
            "unsafe_blueprint_manifest",
            "El blueprint gubernamental no puede ampliar su alcance de tenant.",
            500,
        )

    modules = manifest.get("modules")
    if not isinstance(modules, list) or len(modules) != len(_MODULE_IDS):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "El manifiesto no declara todos los modulos.", 500
        )
    module_ids: list[str] = []
    for module in modules:
        if not isinstance(module, dict):
            raise TenantBlueprintError(
                "invalid_blueprint_manifest", "El manifiesto declara un modulo invalido.", 500
            )
        _require_keys(
            module,
            {"id", "label", "summary", "activation_state"},
            location="manifest.modules",
        )
        module_id = str(module.get("id") or "")
        if (
            module_id not in _MODULE_IDS
            or module.get("activation_state") not in _ACTIVATION_STATES
            or not str(module.get("label") or "").strip()
            or not str(module.get("summary") or "").strip()
        ):
            raise TenantBlueprintError(
                "invalid_blueprint_manifest", "El manifiesto declara un modulo invalido.", 500
            )
        module_ids.append(module_id)
    if set(module_ids) != _MODULE_IDS or len(module_ids) != len(set(module_ids)):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "El manifiesto repite o omite modulos.", 500
        )

    defaults = manifest.get("configuration_defaults")
    if not isinstance(defaults, dict):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "La configuracion inicial no es valida.", 500
        )
    _require_keys(defaults, _CONFIGURATION_KEYS, location="manifest.configuration_defaults")

    operating_model = defaults.get("operating_model")
    if not isinstance(operating_model, dict):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "El modelo operativo no es valido.", 500
        )
    _require_keys(
        operating_model,
        {"case_intake", "human_handoff", "audit_mode"},
        location="manifest.configuration_defaults.operating_model",
    )
    if operating_model != {
        "case_intake": "single_desk",
        "human_handoff": "supervised",
        "audit_mode": "required",
    }:
        raise TenantBlueprintError(
            "unsafe_blueprint_manifest", "El modelo operativo inicial no es seguro.", 500
        )

    categories = defaults.get("ticket_categories")
    if (
        not isinstance(categories, list)
        or not categories
        or any(not isinstance(item, str) or not item.strip() for item in categories)
        or len({item.strip().casefold() for item in categories}) != len(categories)
    ):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "Las categorias iniciales no son validas.", 500
        )

    routing = defaults.get("employee_routing")
    if not isinstance(routing, dict):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "La asignacion inicial no es valida.", 500
        )
    _require_keys(
        routing,
        {
            "default_ticket_categories",
            "default_channels",
            "default_zones",
            "assignment_mode",
        },
        location="manifest.configuration_defaults.employee_routing",
    )
    if any(
        routing.get(key) != []
        for key in ("default_ticket_categories", "default_channels", "default_zones")
    ) or routing.get("assignment_mode") != "manual":
        raise TenantBlueprintError(
            "unsafe_blueprint_manifest", "La asignacion inicial debe ser neutra.", 500
        )

    channels = defaults.get("channels")
    if not isinstance(channels, dict) or set(channels) != {"web_widget", "whatsapp"}:
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "Los canales iniciales no son validos.", 500
        )
    for channel in channels.values():
        if not isinstance(channel, dict):
            raise TenantBlueprintError(
                "invalid_blueprint_manifest", "Los canales iniciales no son validos.", 500
            )
        _require_keys(
            channel,
            {"desired", "enabled", "activation_state"},
            location="manifest.configuration_defaults.channels",
        )
        if channel.get("desired") is not True or channel.get("enabled") is not False:
            raise TenantBlueprintError(
                "unsafe_blueprint_manifest", "Un blueprint no puede activar proveedores.", 500
            )
        if channel.get("activation_state") not in _ACTIVATION_STATES:
            raise TenantBlueprintError(
                "invalid_blueprint_manifest", "El estado del canal no es valido.", 500
            )

    accessibility = defaults.get("accessibility_policy")
    if not isinstance(accessibility, dict):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "La politica de accesibilidad no es valida.", 500
        )
    _require_keys(
        accessibility,
        {"status", "supports"},
        location="manifest.configuration_defaults.accessibility_policy",
    )
    allowed_accessibility_supports = {
        "keyboard_navigation",
        "screen_reader",
        "high_contrast",
        "reduced_motion",
        "dyslexia_friendly_reading",
        "plain_language",
        "voice_transcription",
    }
    supports = accessibility.get("supports")
    if (
        accessibility.get("status") != "validation_required"
        or not isinstance(supports, list)
        or set(supports) != allowed_accessibility_supports
        or len(supports) != len(set(supports))
    ):
        raise TenantBlueprintError(
            "unsafe_blueprint_manifest",
            "La politica de accesibilidad debe quedar pendiente de validacion.",
            500,
        )

    module_configuration = defaults.get("module_configuration")
    if not isinstance(module_configuration, dict) or set(module_configuration) != _MODULE_IDS:
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "La configuracion modular no es valida.", 500
        )
    for module_id, module_config in module_configuration.items():
        if not isinstance(module_config, dict):
            raise TenantBlueprintError(
                "invalid_blueprint_manifest", "La configuracion modular no es valida.", 500
            )
        _require_keys(
            module_config,
            {"desired", "activation_state"},
            location=f"manifest.configuration_defaults.module_configuration.{module_id}",
        )
        if (
            module_config.get("desired") is not True
            or module_config.get("activation_state") not in _ACTIVATION_STATES
        ):
            raise TenantBlueprintError(
                "invalid_blueprint_manifest", "La configuracion modular no es valida.", 500
            )

    declared_module_states = {
        module["id"]: module["activation_state"] for module in modules
    }
    if any(
        module_configuration[module_id]["activation_state"]
        != declared_module_states[module_id]
        for module_id in _MODULE_IDS
    ):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest",
            "Los estados de modulo no son coherentes.",
            500,
        )

    governance = defaults.get("data_governance")
    expected_governance = {
        "tenant_isolation": "required",
        "consent_tracking": "required",
        "audit_receipts": "required",
        "territorial_evidence": "required",
    }
    if not isinstance(governance, dict):
        raise TenantBlueprintError(
            "invalid_blueprint_manifest", "La gobernanza de datos no es valida.", 500
        )
    _require_keys(
        governance,
        set(expected_governance),
        location="manifest.configuration_defaults.data_governance",
    )
    if governance != expected_governance:
        raise TenantBlueprintError(
            "unsafe_blueprint_manifest", "La gobernanza inicial no es segura.", 500
        )

    return deepcopy(manifest)


def load_blueprint(blueprint_id: str) -> tuple[dict[str, Any], str]:
    filename = _BLUEPRINT_REGISTRY.get(str(blueprint_id or "").strip())
    if not filename:
        raise TenantBlueprintError(
            "blueprint_not_found", "Blueprint no encontrado.", 404
        )
    path = (_MANIFEST_ROOT / filename).resolve()
    if path.parent != _MANIFEST_ROOT.resolve():
        raise TenantBlueprintError(
            "unsafe_blueprint_registry", "El registro de blueprints no es seguro.", 500
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TenantBlueprintError(
            "blueprint_unavailable", "El blueprint no esta disponible.", 503
        ) from exc
    validated = _validate_manifest(manifest, expected_blueprint_id=blueprint_id)
    return validated, _digest(validated)


def _blueprint_metadata(manifest: Mapping[str, Any], manifest_digest: str) -> dict[str, Any]:
    return {
        "id": manifest["blueprint_id"],
        "version": manifest["version"],
        "label": manifest["label"],
        "description": manifest["description"],
        "manifest_digest": manifest_digest,
        "supported_tenant_types": list(manifest["supported_tenant_types"]),
        "modules": deepcopy(manifest["modules"]),
    }


def list_blueprints() -> dict[str, Any]:
    blueprints = []
    for blueprint_id in sorted(_BLUEPRINT_REGISTRY):
        manifest, manifest_digest = load_blueprint(blueprint_id)
        blueprints.append(_blueprint_metadata(manifest, manifest_digest))
    return {
        "contract_version": MANIFEST_CONTRACT_VERSION,
        "blueprints": blueprints,
    }


def get_blueprint(tenant: TenantProfile, blueprint_id: str) -> dict[str, Any]:
    manifest, manifest_digest = load_blueprint(blueprint_id)
    _ensure_tenant_eligible(tenant, manifest)
    receipt = TenantBlueprintApplication.query.filter_by(
        tenant_id=tenant.id,
        blueprint_id=manifest["blueprint_id"],
        blueprint_version=manifest["version"],
    ).one_or_none()
    return {
        "contract_version": DETAIL_CONTRACT_VERSION,
        "tenant": {"id": tenant.id, "slug": tenant.slug, "type": tenant.tipo},
        "blueprint": {
            **_blueprint_metadata(manifest, manifest_digest),
            "configuration_defaults": deepcopy(manifest["configuration_defaults"]),
        },
        "application_receipt": receipt.to_dict() if receipt else None,
        "runtime_activation_performed": False,
        "external_calls_performed": False,
    }


def _ensure_tenant_eligible(
    tenant: TenantProfile | None, manifest: Mapping[str, Any]
) -> None:
    if tenant is None:
        raise TenantBlueprintError("tenant_not_found", "Tenant no encontrado.", 404)
    if getattr(tenant, "is_active", False) is not True:
        raise TenantBlueprintError(
            "tenant_inactive", "El tenant debe estar activo.", 409
        )
    supported = {
        normalize_tenant_type(str(item), default="")
        for item in manifest["supported_tenant_types"]
    }
    tenant_type = normalize_tenant_type(getattr(tenant, "tipo", None), default="")
    if not tenant_type or tenant_type not in supported:
        raise TenantBlueprintError(
            "tenant_type_not_supported",
            "El blueprint no admite este tipo de tenant.",
            409,
        )


def _merge_defaults(
    current: Any,
    defaults: Mapping[str, Any],
    *,
    path: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    target = deepcopy(current) if isinstance(current, dict) else {}
    applied: list[str] = []
    preserved: list[str] = []
    for key, default_value in defaults.items():
        item_path = f"{path}.{key}"
        if key not in target:
            target[key] = deepcopy(default_value)
            applied.append(item_path)
        elif isinstance(default_value, Mapping) and isinstance(target.get(key), dict):
            merged, child_applied, child_preserved = _merge_defaults(
                target[key], default_value, path=item_path
            )
            target[key] = merged
            applied.extend(child_applied)
            preserved.extend(child_preserved)
        else:
            preserved.append(item_path)
    return target, sorted(applied), sorted(preserved)


def _build_projection(
    tenant: TenantProfile, manifest: Mapping[str, Any], manifest_digest: str
) -> dict[str, Any]:
    current_config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    namespace = str(manifest["blueprint_id"]).replace("-", "_")
    current_namespace = current_config.get(namespace)
    if namespace in current_config and not isinstance(current_namespace, dict):
        raise TenantBlueprintError(
            "blueprint_namespace_conflict",
            "La configuracion existente del blueprint no puede preservarse de forma segura.",
            409,
        )
    merged_namespace, applied_paths, preserved_paths = _merge_defaults(
        current_namespace,
        manifest["configuration_defaults"],
        path=f"configuracion.{namespace}",
    )
    merged_config = deepcopy(current_config)
    merged_config[namespace] = merged_namespace
    return {
        "namespace": namespace,
        "manifest_digest": manifest_digest,
        "merged_config": merged_config,
        "applied_paths": applied_paths,
        "preserved_paths": preserved_paths,
        "configuration_digest": _digest(merged_config),
    }


def preview_blueprint(tenant: TenantProfile, blueprint_id: str) -> dict[str, Any]:
    manifest, manifest_digest = load_blueprint(blueprint_id)
    _ensure_tenant_eligible(tenant, manifest)
    projection = _build_projection(tenant, manifest, manifest_digest)
    return {
        "contract_version": PREVIEW_CONTRACT_VERSION,
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "type": tenant.tipo,
        },
        "blueprint": _blueprint_metadata(manifest, manifest_digest),
        "changes": {
            "namespace": projection["namespace"],
            "apply_count": len(projection["applied_paths"]),
            "preserve_count": len(projection["preserved_paths"]),
            "apply_paths": projection["applied_paths"],
            "preserve_paths": projection["preserved_paths"],
        },
        "configuration_digest_after_apply": projection["configuration_digest"],
        "write_performed": False,
        "runtime_activation_performed": False,
        "external_calls_performed": False,
    }


def _validate_apply_inputs(expected_manifest_digest: str, idempotency_key: str) -> None:
    if not _DIGEST_PATTERN.fullmatch(expected_manifest_digest):
        raise TenantBlueprintError(
            "invalid_manifest_digest", "manifest_digest no es valido.", 400
        )
    if not 8 <= len(idempotency_key) <= 128 or any(
        ord(char) < 33 or ord(char) > 126 for char in idempotency_key
    ):
        raise TenantBlueprintError(
            "invalid_idempotency_key", "Idempotency-Key no es valido.", 400
        )


def _serialize_apply(
    receipt: TenantBlueprintApplication, *, replayed: bool
) -> dict[str, Any]:
    snapshot = receipt.application_snapshot if isinstance(receipt.application_snapshot, dict) else {}
    return {
        "contract_version": APPLY_CONTRACT_VERSION,
        "receipt": receipt.to_dict(),
        "changes": deepcopy(snapshot.get("changes") or {}),
        "replayed": replayed,
        "write_performed": not replayed,
        "runtime_activation_performed": False,
        "external_calls_performed": False,
    }


def _existing_receipt_or_conflict(
    receipt: TenantBlueprintApplication | None,
    *,
    request_digest: str,
    reason_code: str,
) -> dict[str, Any] | None:
    if receipt is None:
        return None
    if receipt.request_digest == request_digest:
        return _serialize_apply(receipt, replayed=True)
    raise TenantBlueprintError(reason_code, "La solicitud entra en conflicto con un recibo existente.", 409)


def apply_blueprint(
    tenant: TenantProfile,
    *,
    actor_user_id: int,
    blueprint_id: str,
    expected_manifest_digest: str,
    idempotency_key: str,
) -> dict[str, Any]:
    expected_manifest_digest = str(expected_manifest_digest or "").strip().lower()
    idempotency_key = str(idempotency_key or "").strip()
    _validate_apply_inputs(expected_manifest_digest, idempotency_key)

    manifest, manifest_digest = load_blueprint(blueprint_id)
    _ensure_tenant_eligible(tenant, manifest)
    if expected_manifest_digest != manifest_digest:
        raise TenantBlueprintError(
            "manifest_digest_mismatch",
            "El manifiesto cambio; ejecuta preview nuevamente.",
            409,
        )

    locked_tenant = (
        TenantProfile.query.filter_by(id=tenant.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    _ensure_tenant_eligible(locked_tenant, manifest)
    tenant = locked_tenant

    idempotency_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_digest = _digest(
        {
            "tenant_id": tenant.id,
            "blueprint_id": manifest["blueprint_id"],
            "blueprint_version": manifest["version"],
            "manifest_digest": manifest_digest,
        }
    )

    existing = TenantBlueprintApplication.query.filter_by(
        tenant_id=tenant.id, idempotency_key_hash=idempotency_hash
    ).one_or_none()
    replay = _existing_receipt_or_conflict(
        existing,
        request_digest=request_digest,
        reason_code="idempotency_key_conflict",
    )
    if replay:
        return replay

    existing = TenantBlueprintApplication.query.filter_by(
        tenant_id=tenant.id,
        blueprint_id=manifest["blueprint_id"],
        blueprint_version=manifest["version"],
    ).one_or_none()
    replay = _existing_receipt_or_conflict(
        existing,
        request_digest=request_digest,
        reason_code="blueprint_version_conflict",
    )
    if replay:
        return replay

    projection = _build_projection(tenant, manifest, manifest_digest)
    changes = {
        "namespace": projection["namespace"],
        "apply_count": len(projection["applied_paths"]),
        "preserve_count": len(projection["preserved_paths"]),
        "apply_paths": projection["applied_paths"],
        "preserve_paths": projection["preserved_paths"],
    }
    receipt = TenantBlueprintApplication(
        tenant_id=tenant.id,
        blueprint_id=manifest["blueprint_id"],
        blueprint_version=manifest["version"],
        manifest_digest=manifest_digest,
        request_digest=request_digest,
        idempotency_key_hash=idempotency_hash,
        status="applied",
        application_snapshot={
            "blueprint": {
                "id": manifest["blueprint_id"],
                "version": manifest["version"],
                "manifest_digest": manifest_digest,
            },
            "module_states": {
                module["id"]: module["activation_state"]
                for module in manifest["modules"]
            },
            "changes": changes,
            "configuration_digest": projection["configuration_digest"],
            "runtime_activation_performed": False,
            "external_calls_performed": False,
        },
        applied_by_user_id=actor_user_id,
    )

    try:
        tenant.configuracion = projection["merged_config"]
        db.session.add(receipt)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        collision = TenantBlueprintApplication.query.filter_by(
            tenant_id=tenant.id, idempotency_key_hash=idempotency_hash
        ).one_or_none()
        if collision is None:
            collision = TenantBlueprintApplication.query.filter_by(
                tenant_id=tenant.id,
                blueprint_id=manifest["blueprint_id"],
                blueprint_version=manifest["version"],
            ).one_or_none()
        replay = _existing_receipt_or_conflict(
            collision,
            request_digest=request_digest,
            reason_code="blueprint_apply_conflict",
        )
        if replay:
            return replay
        raise TenantBlueprintError(
            "blueprint_apply_conflict", "No se pudo aplicar el blueprint.", 409
        ) from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise TenantBlueprintError(
            "blueprint_apply_unavailable",
            "No se pudo aplicar el blueprint de forma atomica.",
            503,
        ) from exc
    except Exception:
        db.session.rollback()
        raise

    return _serialize_apply(receipt, replayed=False)
