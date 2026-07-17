from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import requests

from services.meta_flow_json import canonical_flow_json


META_FLOW_FIELDS = (
    "id,name,categories,preview,status,validation_errors,json_version,"
    "data_api_version,data_channel_uri,health_status,"
    "whatsapp_business_account,application"
)
META_FLOW_ALLOWED_CATEGORIES = frozenset(
    {
        "SIGN_UP",
        "SIGN_IN",
        "APPOINTMENT_BOOKING",
        "LEAD_GENERATION",
        "CONTACT_US",
        "CUSTOMER_SUPPORT",
        "SURVEY",
        "OTHER",
    }
)
_META_ID_PATTERN = re.compile(r"^\d{6,32}$")
_GRAPH_VERSION_PATTERN = re.compile(r"^v\d+\.\d+$")
_SAFE_ENV_SUFFIX = re.compile(r"[^A-Za-z0-9]+")
_META_ASSET_HOST_SUFFIXES = (
    ".fbcdn.net",
    ".facebook.com",
    ".fbsbx.com",
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_ASSET_REDIRECTS = 3


def _public_graph_base_url(value: str) -> str | None:
    parsed = urlsplit(str(value or ""))
    hostname = parsed.hostname
    if parsed.scheme.lower() != "https" or not hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port else host
    path = parsed.path.rstrip("/")
    return f"https://{authority}{path}"


class MetaFlowManagementError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code or "meta_flow_management_error")
        self.message = str(message or "Meta rechazo la operacion")
        self.status_code = int(status_code)
        self.details = dict(details or {})
        super().__init__(self.message)

    def public_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class MetaGraphCredentials:
    waba_id: str
    api_version: str
    base_url: str = field(repr=False)
    timeout_seconds: float
    access_token: str | None = field(default=None, repr=False)
    token_source: str = "not_configured"
    blockers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def graph_base_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.api_version}"

    def public_payload(self) -> dict[str, Any]:
        return {
            "configured": bool(self.access_token),
            "ready": self.ready,
            "waba_id_present": bool(self.waba_id),
            "waba_id_valid": bool(_META_ID_PATTERN.fullmatch(self.waba_id)),
            "api_version": self.api_version,
            "base_url": _public_graph_base_url(self.base_url),
            "token_source": self.token_source,
            "blockers": list(self.blockers),
        }


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _config_or_env(
    app_config: Mapping[str, Any],
    environ: Mapping[str, str],
    key: str,
) -> str:
    return _clean(app_config.get(key)) or _clean(environ.get(key))


def _waba_env_suffix(waba_id: str) -> str:
    return _SAFE_ENV_SUFFIX.sub("_", _clean(waba_id)).strip("_").upper()


def resolve_meta_graph_credentials(
    *,
    waba_id: Any,
    app_config: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> MetaGraphCredentials:
    env = environ if environ is not None else os.environ
    normalized_waba = _clean(waba_id)
    suffix = _waba_env_suffix(normalized_waba)
    per_waba_key = f"META_FLOW_WABA_{suffix}_ACCESS_TOKEN" if suffix else ""
    token = ""
    token_source = "not_configured"
    for key in (
        per_waba_key,
        "META_GRAPH_ACCESS_TOKEN",
        "META_WHATSAPP_ACCESS_TOKEN",
    ):
        if not key:
            continue
        candidate = _config_or_env(app_config, env, key)
        if candidate:
            token = candidate
            token_source = "per_waba" if key == per_waba_key else "platform"
            break

    api_version = _config_or_env(app_config, env, "META_GRAPH_API_VERSION") or "v23.0"
    base_url = (
        _config_or_env(app_config, env, "META_GRAPH_API_BASE_URL")
        or "https://graph.facebook.com"
    ).rstrip("/")
    timeout_invalid = False
    try:
        timeout_seconds = float(
            _config_or_env(app_config, env, "META_GRAPH_API_TIMEOUT_SECONDS") or "20"
        )
    except (TypeError, ValueError):
        timeout_seconds = 20.0
        timeout_invalid = True

    blockers: list[str] = []
    if not normalized_waba:
        blockers.append("waba_id_not_configured")
    elif not _META_ID_PATTERN.fullmatch(normalized_waba):
        blockers.append("waba_id_invalid")
    if not token:
        blockers.append("meta_graph_access_token_not_configured")
    if not _GRAPH_VERSION_PATTERN.fullmatch(api_version):
        blockers.append("meta_graph_api_version_invalid")
    parsed_base = urlsplit(base_url)
    try:
        parsed_port_valid = parsed_base.port is None or parsed_base.port > 0
    except ValueError:
        parsed_port_valid = False
    if (
        parsed_base.scheme.lower() != "https"
        or not parsed_base.hostname
        or parsed_base.username is not None
        or parsed_base.password is not None
        or bool(parsed_base.query)
        or bool(parsed_base.fragment)
        or not parsed_port_valid
    ):
        blockers.append("meta_graph_api_base_url_invalid")
    if timeout_invalid or timeout_seconds <= 0 or timeout_seconds > 120:
        blockers.append("meta_graph_api_timeout_invalid")

    return MetaGraphCredentials(
        waba_id=normalized_waba,
        api_version=api_version,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        access_token=token or None,
        token_source=token_source,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def meta_flow_category(flow_id: Any, blueprint_category: Any = None) -> str:
    haystack = f"{_clean(flow_id)} {_clean(blueprint_category)}".lower()
    if any(term in haystack for term in ("survey", "encuesta", "votacion", "poll")):
        return "SURVEY"
    if any(term in haystack for term in ("appointment", "turno", "reserva", "booking")):
        return "APPOINTMENT_BOOKING"
    if any(term in haystack for term in ("claim", "reclamo", "support", "soporte", "ticket")):
        return "CUSTOMER_SUPPORT"
    if any(term in haystack for term in ("lead", "cotizacion", "presupuesto")):
        return "LEAD_GENERATION"
    if any(term in haystack for term in ("contact", "contacto")):
        return "CONTACT_US"
    return "OTHER"


def build_meta_flow_publication_readiness(
    *,
    tenant_id: Any,
    flow_id: Any,
    flow_name: Any,
    blueprint_category: Any,
    artifact: Mapping[str, Any],
    credentials: MetaGraphCredentials,
    integration_access: Mapping[str, Any],
    data_exchange: Mapping[str, Any],
    registry: Mapping[str, Any] | None = None,
    supplied_meta_flow_id: Any = None,
    language: Any = "es",
) -> dict[str, Any]:
    """Build a tenant-bound publication plan without calling Meta or Twilio."""

    normalized_tenant_id = _clean(tenant_id)
    normalized_flow_id = _clean(flow_id)
    normalized_flow_name = _clean(flow_name) or normalized_flow_id
    normalized_language = _clean(language) or "es"
    registry_state = dict(registry) if isinstance(registry, Mapping) else {}
    registry_present = bool(registry_state.get("configured"))
    registry_tenant_id = _clean(registry_state.get("tenant_id"))
    registry_scope_verified = bool(
        not registry_present
        or (
            normalized_tenant_id
            and registry_tenant_id
            and registry_tenant_id == normalized_tenant_id
        )
    )

    # Never surface or trust identity from a registry row outside the tenant.
    scoped_registry = registry_state if registry_scope_verified else {}
    registered_meta_flow_id = _clean(scoped_registry.get("meta_flow_id"))
    registered_artifact_sha256 = _clean(scoped_registry.get("flow_json_sha256"))
    registered_waba_id = _clean(scoped_registry.get("waba_id"))
    publication_verified = bool(scoped_registry.get("publication_verified"))
    sync_state = _clean(scoped_registry.get("sync_state")).lower()
    supplied_id = _clean(supplied_meta_flow_id)
    target_meta_flow_id = supplied_id or registered_meta_flow_id

    validation = (
        artifact.get("validation")
        if isinstance(artifact.get("validation"), Mapping)
        else {}
    )
    artifact_sha256 = _clean(artifact.get("content_sha256")).lower()
    endpoint_driven = bool(artifact.get("endpoint_driven"))
    endpoint_uri = (
        _clean(data_exchange.get("endpoint_url")) if endpoint_driven else ""
    )
    current_waba_id = _clean(credentials.waba_id)
    artifact_changed = bool(
        registered_artifact_sha256
        and registered_artifact_sha256 != artifact_sha256
    )
    can_clone_published = bool(
        artifact_changed
        and publication_verified
        and registered_meta_flow_id
        and registered_waba_id
        and registered_waba_id == current_waba_id
        and (not supplied_id or supplied_id == registered_meta_flow_id)
    )
    clone_flow_id = registered_meta_flow_id if can_clone_published else None

    blockers: list[str] = []
    if not normalized_tenant_id:
        blockers.append("tenant_scope_required")
    if not registry_scope_verified:
        blockers.append("meta_flow_registry_tenant_scope_mismatch")
    if not normalized_flow_id:
        blockers.append("flow_id_required")
    if not artifact.get("publishable_flow_json"):
        blockers.append("flow_json_artifact_not_publishable")
    if not validation.get("valid"):
        blockers.append("flow_json_artifact_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        blockers.append("flow_json_artifact_identity_invalid")
    if not integration_access.get("enabled"):
        blockers.append(
            _clean(
                integration_access.get("lock_reason_code")
                or integration_access.get("reason_code")
            )
            or "plan_full_required"
        )
    blockers.extend(credentials.blockers)
    if endpoint_driven and not data_exchange.get("ready"):
        blockers.append("data_exchange_not_ready")
    if endpoint_driven and not endpoint_uri:
        blockers.append("data_exchange_endpoint_url_missing")
    if (
        supplied_id
        and registered_meta_flow_id
        and supplied_id != registered_meta_flow_id
    ):
        blockers.append("meta_flow_registry_conflict")
    if artifact_changed and not clone_flow_id:
        blockers.append("meta_flow_registry_artifact_conflict")
    if (
        registered_waba_id
        and current_waba_id
        and registered_waba_id != current_waba_id
    ):
        blockers.append("meta_flow_registry_waba_conflict")
    if sync_state in {"publishing", "uncertain"}:
        blockers.append("meta_flow_sync_reconciliation_required")
    blockers = list(dict.fromkeys(blockers))

    persisted_publication_match = bool(
        registry_scope_verified
        and publication_verified
        and registered_meta_flow_id
        and target_meta_flow_id == registered_meta_flow_id
        and registered_artifact_sha256 == artifact_sha256
        and registered_waba_id
        and registered_waba_id == current_waba_id
    )
    replacement_mode = "clone_published" if clone_flow_id else "reuse_or_create"
    operation_mode = (
        "verify_published"
        if persisted_publication_match
        else "clone_published"
        if clone_flow_id
        else "reuse_draft"
        if target_meta_flow_id
        else "create_and_publish"
    )
    steps = (
        ["download_and_verify_remote_hash"]
        if operation_mode == "verify_published"
        else [
            (
                "clone_published_flow"
                if clone_flow_id
                else "create_or_reuse_draft"
            ),
            "upload_exact_flow_json",
            "validate_meta_schema",
            "publish_immutable_flow",
            "download_and_verify_remote_hash",
        ]
    )
    fingerprint_payload = {
        "tenant_id": normalized_tenant_id,
        "flow_id": normalized_flow_id,
        "flow_json_sha256": artifact_sha256,
        "waba_id": current_waba_id,
        "meta_flow_id": target_meta_flow_id,
        "endpoint_uri": endpoint_uri,
        "language": normalized_language,
        "operation_mode": operation_mode,
    }
    operation_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    dry_run_payload: dict[str, Any] = {
        "flow_id": normalized_flow_id,
        "language": normalized_language,
        "dry_run": True,
        "publish": True,
    }
    if target_meta_flow_id:
        dry_run_payload["meta_flow_id"] = target_meta_flow_id

    ready = not blockers
    status = (
        "blocked"
        if blockers
        else "verification_ready"
        if persisted_publication_match
        else "publish_ready"
    )
    return {
        "contract_version": "whatsapp.meta_flow_publication_readiness.v1",
        "flow_id": normalized_flow_id,
        "flow_name": normalized_flow_name,
        "category": meta_flow_category(normalized_flow_id, blueprint_category),
        "status": status,
        "ready": ready,
        "ready_to_sync": ready,
        "blocked": bool(blockers),
        "blockers": blockers,
        "operation": {
            "mode": operation_mode,
            "replacement_mode": replacement_mode,
            "target_meta_flow_id": target_meta_flow_id or None,
            "clone_flow_id": clone_flow_id,
            "publish_write_expected": not persisted_publication_match,
            "irreversible_publish": not persisted_publication_match,
            "steps": steps,
        },
        "idempotency": {
            "state": (
                "blocked"
                if blockers
                else "verification_only_expected"
                if persisted_publication_match
                else "new_provider_write_expected"
            ),
            "persisted_publication_match": persisted_publication_match,
            "provider_write_expected": not persisted_publication_match,
            "remote_verification_required": True,
            "same_confirmation_replay_allowed": False,
            "retry_requires_fresh_dry_run": True,
            "operation_fingerprint": operation_fingerprint,
        },
        "artifact": {
            "publishable": bool(artifact.get("publishable_flow_json")),
            "validation_valid": bool(validation.get("valid")),
            "flow_json_version": artifact.get("flow_json_version"),
            "data_api_version": artifact.get("data_api_version"),
            "content_sha256": artifact_sha256 or None,
            "byte_size": artifact.get("byte_size"),
            "endpoint_driven": endpoint_driven,
        },
        "registry": {
            "configured": registry_present,
            "id": scoped_registry.get("id") if registry_scope_verified else None,
            "tenant_scope_verified": registry_scope_verified,
            "status": scoped_registry.get("status") if registry_scope_verified else None,
            "meta_flow_id": registered_meta_flow_id or None,
            "waba_scope_verified": bool(
                not registered_waba_id
                or (
                    current_waba_id
                    and registered_waba_id == current_waba_id
                )
            ),
            "artifact_identity_matches": bool(
                registered_artifact_sha256
                and registered_artifact_sha256 == artifact_sha256
            ),
            "publication_verified": publication_verified,
            "sync_state": sync_state or None,
            "last_sync_at": scoped_registry.get("last_sync_at"),
        },
        "dependencies": {
            "integration_access": {
                "enabled": bool(integration_access.get("enabled")),
                "reason_code": integration_access.get("reason_code")
                or integration_access.get("lock_reason_code"),
            },
            "graph": credentials.public_payload(),
            "data_exchange": {
                "ready": bool(data_exchange.get("ready")),
                "endpoint_uri": endpoint_uri or None,
                "tenant_bound": bool(data_exchange.get("tenant_bound")),
                "waba_bound": bool(data_exchange.get("waba_bound")),
                "blockers": list(data_exchange.get("blockers") or []),
            },
        },
        "dry_run": {
            "method": "POST",
            "endpoint": "/api/admin/whatsapp/flows/meta/sync",
            "payload": dry_run_payload,
            "meta_graph_calls_performed": False,
            "twilio_calls_performed": False,
            "provider_writes_performed": False,
            "messages_sent": False,
            "execute_confirmation_issued": False,
        },
        "security": {
            "tenant_scope_verified": registry_scope_verified,
            "graph_access_token_exposed": False,
            "data_exchange_private_key_exposed": False,
            "meta_app_secret_exposed": False,
            "execute_confirmation_exposed": False,
        },
    }


class MetaFlowGraphClient:
    def __init__(
        self,
        credentials: MetaGraphCredentials,
        *,
        http: Any = requests,
    ) -> None:
        if not credentials.ready or not credentials.access_token:
            raise MetaFlowManagementError(
                "meta_graph_not_ready",
                "La integracion Graph API de Meta no esta configurada",
                status_code=503,
                details={"blockers": list(credentials.blockers)},
            )
        self.credentials = credentials
        self._http = http

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.access_token}",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.credentials.graph_base_url}/{str(path).lstrip('/')}"

    @staticmethod
    def _decode_json(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            raise MetaFlowManagementError(
                "meta_graph_invalid_json",
                "Meta devolvio una respuesta que no es JSON",
                status_code=502,
            ) from exc
        if not isinstance(payload, dict):
            raise MetaFlowManagementError(
                "meta_graph_invalid_contract",
                "Meta devolvio un contrato inesperado",
                status_code=502,
            )
        return payload

    @staticmethod
    def _raise_for_meta_error(response: Any, payload: Mapping[str, Any]) -> None:
        status_code = int(getattr(response, "status_code", 0) or 0)
        error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
        if 200 <= status_code < 300 and not error:
            return
        graph_code = _clean(error.get("code")) or "unknown"
        graph_subcode = _clean(error.get("error_subcode"))
        code = f"meta_graph_{graph_code}"
        if graph_subcode:
            code = f"{code}_{graph_subcode}"
        raise MetaFlowManagementError(
            code,
            _clean(error.get("message")) or "Meta rechazo la operacion",
            status_code=502 if status_code >= 500 else 409,
            details={
                "graph_status": status_code,
                "graph_type": _clean(error.get("type")) or None,
                "graph_code": graph_code,
                "graph_subcode": graph_subcode or None,
                "fbtrace_id": _clean(error.get("fbtrace_id")) or None,
            },
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._http.request(
                method,
                self._url(path),
                headers=self._headers,
                params=dict(params or {}),
                data=dict(data or {}),
                files=dict(files or {}),
                timeout=self.credentials.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise MetaFlowManagementError(
                "meta_graph_unreachable",
                "No se pudo conectar con Graph API de Meta",
                status_code=503,
            ) from exc
        payload = self._decode_json(response)
        self._raise_for_meta_error(response, payload)
        return payload

    def create_flow(
        self,
        *,
        name: str,
        category: str,
        endpoint_uri: str | None,
        clone_flow_id: str | None = None,
    ) -> str:
        normalized_category = _clean(category).upper()
        if normalized_category not in META_FLOW_ALLOWED_CATEGORIES:
            raise MetaFlowManagementError(
                "meta_flow_category_invalid",
                "La categoria del Flow no es valida para Meta",
                status_code=422,
            )
        payload = self._request_json(
            "POST",
            f"{self.credentials.waba_id}/flows",
            data={
                "name": _clean(name),
                "categories": json.dumps([normalized_category], separators=(",", ":")),
                "endpoint_uri": _clean(endpoint_uri) or None,
                "clone_flow_id": _clean(clone_flow_id) or None,
            },
        )
        flow_id = _clean(payload.get("id"))
        if not _META_ID_PATTERN.fullmatch(flow_id):
            raise MetaFlowManagementError(
                "meta_flow_id_missing",
                "Meta no devolvio un Flow ID valido",
                status_code=502,
            )
        return flow_id

    def get_flow(self, meta_flow_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            meta_flow_id,
            params={"fields": META_FLOW_FIELDS},
        )

    def upload_flow_json(self, meta_flow_id: str, canonical_json: str) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            f"{meta_flow_id}/assets",
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
            files={
                "file": (
                    "flow.json",
                    canonical_json.encode("utf-8"),
                    "application/json",
                )
            },
        )
        validation_errors = payload.get("validation_errors")
        if isinstance(validation_errors, list) and validation_errors:
            raise MetaFlowManagementError(
                "meta_flow_json_rejected",
                "Meta encontro errores de validacion en flow.json",
                status_code=422,
                details={"validation_errors": validation_errors[:25]},
            )
        if payload.get("success") is not True:
            raise MetaFlowManagementError(
                "meta_flow_json_upload_unconfirmed",
                "Meta no confirmo la carga de flow.json",
                status_code=502,
            )
        return payload

    def publish_flow(self, meta_flow_id: str) -> None:
        payload = self._request_json("POST", f"{meta_flow_id}/publish")
        if payload.get("success") is not True:
            raise MetaFlowManagementError(
                "meta_flow_publish_unconfirmed",
                "Meta no confirmo la publicacion del Flow",
                status_code=502,
            )

    def list_assets(self, meta_flow_id: str) -> list[dict[str, Any]]:
        payload = self._request_json("GET", f"{meta_flow_id}/assets")
        assets = payload.get("data")
        if not isinstance(assets, list):
            raise MetaFlowManagementError(
                "meta_flow_assets_invalid",
                "Meta no devolvio la lista de assets del Flow",
                status_code=502,
            )
        return [dict(item) for item in assets if isinstance(item, Mapping)]

    @staticmethod
    def _validate_asset_url(download_url: str) -> None:
        parsed = urlsplit(download_url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() != "https"
            or not host
            or not any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in _META_ASSET_HOST_SUFFIXES)
        ):
            raise MetaFlowManagementError(
                "meta_flow_asset_url_invalid",
                "Meta devolvio una URL de asset no permitida",
                status_code=502,
            )

    def download_flow_json(self, meta_flow_id: str) -> dict[str, Any]:
        assets = self.list_assets(meta_flow_id)
        asset = next(
            (
                item
                for item in assets
                if _clean(item.get("asset_type")).upper() == "FLOW_JSON"
            ),
            None,
        )
        download_url = _clean((asset or {}).get("download_url"))
        if not download_url:
            raise MetaFlowManagementError(
                "meta_flow_json_asset_missing",
                "El Flow publicado no contiene un asset FLOW_JSON",
                status_code=409,
            )
        response = None
        current_url = download_url
        for redirect_count in range(_MAX_ASSET_REDIRECTS + 1):
            self._validate_asset_url(current_url)
            try:
                response = self._http.get(
                    current_url,
                    headers={"Accept": "application/json"},
                    timeout=self.credentials.timeout_seconds,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise MetaFlowManagementError(
                    "meta_flow_asset_unreachable",
                    "No se pudo descargar el flow.json publicado por Meta",
                    status_code=503,
                ) from exc
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code not in _REDIRECT_STATUSES:
                break
            if redirect_count >= _MAX_ASSET_REDIRECTS:
                raise MetaFlowManagementError(
                    "meta_flow_asset_redirect_limit",
                    "Meta excedio el limite de redirecciones del asset",
                    status_code=502,
                )
            headers = getattr(response, "headers", None)
            location = _clean(headers.get("Location")) if isinstance(headers, Mapping) else ""
            if not location:
                raise MetaFlowManagementError(
                    "meta_flow_asset_redirect_invalid",
                    "Meta devolvio una redireccion de asset sin destino",
                    status_code=502,
                )
            current_url = urljoin(current_url, location)
        if response is None:
            raise MetaFlowManagementError(
                "meta_flow_asset_unreachable",
                "No se pudo descargar el flow.json publicado por Meta",
                status_code=503,
            )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise MetaFlowManagementError(
                "meta_flow_asset_download_failed",
                "Meta no permitio descargar el flow.json publicado",
                status_code=502,
                details={"asset_status": status_code},
            )
        try:
            document = response.json()
        except Exception as exc:
            raise MetaFlowManagementError(
                "meta_flow_asset_invalid_json",
                "El asset FLOW_JSON publicado no contiene JSON valido",
                status_code=502,
            ) from exc
        if not isinstance(document, dict):
            raise MetaFlowManagementError(
                "meta_flow_asset_invalid_contract",
                "El asset FLOW_JSON publicado tiene un formato inesperado",
                status_code=502,
            )
        return document

    def verify_flow(
        self,
        *,
        meta_flow_id: str,
        expected_document: Mapping[str, Any],
        expected_sha256: str,
        expected_endpoint_uri: str | None,
        require_published: bool,
    ) -> dict[str, Any]:
        metadata = self.get_flow(meta_flow_id)
        remote_waba = metadata.get("whatsapp_business_account")
        remote_waba_id = (
            _clean(remote_waba.get("id"))
            if isinstance(remote_waba, Mapping)
            else ""
        )
        status = _clean(metadata.get("status")).upper()
        validation_errors = (
            metadata.get("validation_errors")
            if isinstance(metadata.get("validation_errors"), list)
            else []
        )
        expected_version = _clean(expected_document.get("version"))
        expected_data_api_version = _clean(expected_document.get("data_api_version"))
        remote_endpoint = _clean(metadata.get("data_channel_uri")).rstrip("/")
        expected_endpoint = _clean(expected_endpoint_uri).rstrip("/")

        blockers: list[str] = []
        if _clean(metadata.get("id")) != meta_flow_id:
            blockers.append("meta_flow_identity_mismatch")
        if remote_waba_id != self.credentials.waba_id:
            blockers.append("meta_flow_waba_mismatch")
        if require_published and status != "PUBLISHED":
            blockers.append("meta_flow_not_published")
        if validation_errors:
            blockers.append("meta_flow_validation_errors")
        if _clean(metadata.get("json_version")) != expected_version:
            blockers.append("meta_flow_json_version_mismatch")
        if expected_data_api_version and (
            _clean(metadata.get("data_api_version")) != expected_data_api_version
        ):
            blockers.append("meta_flow_data_api_version_mismatch")
        if expected_endpoint and remote_endpoint != expected_endpoint:
            blockers.append("meta_flow_endpoint_uri_mismatch")

        remote_document: dict[str, Any] | None = None
        remote_sha256 = ""
        try:
            remote_document = self.download_flow_json(meta_flow_id)
            remote_canonical = canonical_flow_json(remote_document)
            remote_sha256 = hashlib.sha256(remote_canonical.encode("utf-8")).hexdigest()
        except MetaFlowManagementError as exc:
            blockers.append(exc.code)
        if remote_sha256 and remote_sha256 != expected_sha256:
            blockers.append("meta_flow_json_hash_mismatch")

        health_status = (
            dict(metadata.get("health_status"))
            if isinstance(metadata.get("health_status"), Mapping)
            else {}
        )
        preview = (
            dict(metadata.get("preview"))
            if isinstance(metadata.get("preview"), Mapping)
            else {}
        )
        return {
            "verified": not blockers,
            "meta_flow_id": meta_flow_id,
            "status": status or "UNKNOWN",
            "waba_id": remote_waba_id or None,
            "flow_name": _clean(metadata.get("name")) or None,
            "categories": metadata.get("categories") if isinstance(metadata.get("categories"), list) else [],
            "json_version": _clean(metadata.get("json_version")) or None,
            "data_api_version": _clean(metadata.get("data_api_version")) or None,
            "data_channel_uri": remote_endpoint or None,
            "validation_errors": validation_errors,
            "flow_json_sha256": remote_sha256 or None,
            "expected_flow_json_sha256": expected_sha256,
            "artifact_identity_verified": bool(
                remote_sha256 and remote_sha256 == expected_sha256
            ),
            "publication_verified": bool(not blockers and status == "PUBLISHED"),
            "health_status": health_status,
            "preview_url": _clean(preview.get("preview_url")) or None,
            "preview_expires_at": _clean(preview.get("expires_at")) or None,
            "blockers": list(dict.fromkeys(blockers)),
        }

    def provision_and_publish(
        self,
        *,
        flow_name: str,
        category: str,
        document: Mapping[str, Any],
        expected_sha256: str,
        endpoint_uri: str | None,
        meta_flow_id: str | None = None,
        publish: bool = True,
        clone_published_on_change: bool = False,
        verification_only: bool = False,
    ) -> dict[str, Any]:
        canonical = canonical_flow_json(document)
        actual_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual_sha256 != expected_sha256:
            raise MetaFlowManagementError(
                "meta_flow_local_artifact_mismatch",
                "El hash local del Flow JSON no coincide con el artefacto compilado",
                status_code=409,
            )

        created = False
        cloned_from_flow_id: str | None = None
        normalized_meta_flow_id = _clean(meta_flow_id)
        if verification_only and (
            not normalized_meta_flow_id or clone_published_on_change
        ):
            raise MetaFlowManagementError(
                "meta_flow_verification_only_invalid",
                "La verificacion idempotente requiere un Flow publicado conocido",
                status_code=409,
            )
        if normalized_meta_flow_id:
            if not _META_ID_PATTERN.fullmatch(normalized_meta_flow_id):
                raise MetaFlowManagementError(
                    "meta_flow_id_invalid",
                    "meta_flow_id debe ser un identificador numerico real",
                    status_code=400,
                )
            current = self.get_flow(normalized_meta_flow_id)
            current_waba = current.get("whatsapp_business_account")
            current_waba_id = (
                _clean(current_waba.get("id"))
                if isinstance(current_waba, Mapping)
                else ""
            )
            if current_waba_id != self.credentials.waba_id:
                raise MetaFlowManagementError(
                    "meta_flow_waba_mismatch",
                    "El Flow indicado no pertenece al WABA del tenant",
                    status_code=409,
                )
            current_status = _clean(current.get("status")).upper()
            if clone_published_on_change and current_status != "PUBLISHED":
                raise MetaFlowManagementError(
                    "meta_flow_clone_source_state_mismatch",
                    "El Flow origen ya no esta publicado; se requiere un nuevo dry-run",
                    status_code=409,
                    details={
                        "meta_flow_id": normalized_meta_flow_id,
                        "status": current_status or "UNKNOWN",
                    },
                )
            if current_status == "PUBLISHED" and clone_published_on_change:
                published_verification = self.verify_flow(
                    meta_flow_id=normalized_meta_flow_id,
                    expected_document=document,
                    expected_sha256=expected_sha256,
                    expected_endpoint_uri=endpoint_uri,
                    require_published=True,
                )
                cloneable_blockers = {
                    "meta_flow_json_hash_mismatch",
                    "meta_flow_json_version_mismatch",
                    "meta_flow_data_api_version_mismatch",
                }
                blockers = set(published_verification.get("blockers") or ())
                remote_sha256 = _clean(
                    published_verification.get("flow_json_sha256")
                )
                can_clone = bool(
                    blockers
                    and blockers.issubset(cloneable_blockers)
                    and remote_sha256
                    and remote_sha256 != expected_sha256
                )
                if can_clone:
                    cloned_from_flow_id = normalized_meta_flow_id
                    normalized_meta_flow_id = self.create_flow(
                        name=flow_name,
                        category=category,
                        endpoint_uri=endpoint_uri,
                        clone_flow_id=cloned_from_flow_id,
                    )
                    created = True
        else:
            normalized_meta_flow_id = self.create_flow(
                name=flow_name,
                category=category,
                endpoint_uri=endpoint_uri,
            )
            created = True

        try:
            current = self.get_flow(normalized_meta_flow_id)
            current_status = _clean(current.get("status")).upper()
            if verification_only and current_status != "PUBLISHED":
                raise MetaFlowManagementError(
                    "meta_flow_verification_only_state_mismatch",
                    "El Flow remoto ya no esta publicado; se requiere un nuevo dry-run",
                    status_code=409,
                    details={"status": current_status or "UNKNOWN"},
                )
            uploaded = False
            published_now = False
            if current_status != "PUBLISHED":
                if current_status not in {"", "DRAFT"}:
                    raise MetaFlowManagementError(
                        "meta_flow_not_editable",
                        "El Flow no esta en estado DRAFT y no puede actualizarse",
                        status_code=409,
                        details={"status": current_status or "UNKNOWN"},
                    )
                current_endpoint = _clean(current.get("data_channel_uri")).rstrip("/")
                expected_endpoint = _clean(endpoint_uri).rstrip("/")
                if expected_endpoint and current_endpoint != expected_endpoint:
                    raise MetaFlowManagementError(
                        "meta_flow_endpoint_uri_mismatch",
                        "El endpoint Data Exchange del borrador no coincide con el tenant",
                        status_code=409,
                        details={
                            "endpoint_configured": bool(current_endpoint),
                            "endpoint_expected": True,
                        },
                    )
                self.upload_flow_json(normalized_meta_flow_id, canonical)
                uploaded = True
                uploaded_state = self.get_flow(normalized_meta_flow_id)
                validation_errors = (
                    uploaded_state.get("validation_errors")
                    if isinstance(uploaded_state.get("validation_errors"), list)
                    else []
                )
                if validation_errors:
                    raise MetaFlowManagementError(
                        "meta_flow_json_rejected",
                        "Meta encontro errores de validacion antes de publicar",
                        status_code=422,
                        details={"validation_errors": validation_errors[:25]},
                    )
                if publish:
                    self.publish_flow(normalized_meta_flow_id)
                    published_now = True

            verification = self.verify_flow(
                meta_flow_id=normalized_meta_flow_id,
                expected_document=document,
                expected_sha256=expected_sha256,
                expected_endpoint_uri=endpoint_uri,
                require_published=publish,
            )
            if not verification["verified"]:
                raise MetaFlowManagementError(
                    "meta_flow_verification_failed",
                    "Meta no pudo verificar la identidad y estado del Flow",
                    status_code=409,
                    details={"blockers": verification["blockers"]},
                )
        except MetaFlowManagementError as exc:
            exc.details.setdefault("meta_flow_id", normalized_meta_flow_id)
            exc.details.setdefault("created", created)
            raise
        return {
            "created": created,
            "cloned_from_flow_id": cloned_from_flow_id,
            "uploaded": uploaded,
            "published_now": published_now,
            "idempotent": bool(current_status == "PUBLISHED"),
            "verification": verification,
        }


__all__ = [
    "META_FLOW_ALLOWED_CATEGORIES",
    "MetaFlowGraphClient",
    "MetaFlowManagementError",
    "MetaGraphCredentials",
    "build_meta_flow_publication_readiness",
    "meta_flow_category",
    "resolve_meta_graph_credentials",
]
