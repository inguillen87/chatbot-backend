"""Fixed Junin/TDF Graph inspection inside the authenticated private runtime.

Only GETs, read-only database transactions and public metadata projections.
Webhook configuration is discovery evidence, never authority to mutate Meta.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import lazyload

from models import ProviderConnection, ProviderSender, TenantProfile, db
from services import meta_credential_admin_runtime as admin
from services import meta_credential_operations as ops
from services import meta_whatsapp_credentials as vault
from services.meta_whatsapp_cloud import MetaSenderSnapshot
from services.meta_whatsapp_runtime import (
    GRAPH_VERSION_KEY, MetaOutboundRuntime, MetaRuntimeError, _object_response,
    native_graph_request,
)
from services.outbox_execution_budget import (
    activate_outbox_execution_budget, install_outbox_database_timeout_hook,
    require_outbox_time_remaining,
)

CONTRACT = "chatboc.meta_phone_webhook_inspection.v1"
INMOVAR_PHONE = "+18564858583"
_CONFIG_FIELDS = ("application", "whatsapp_business_account", "phone_number")
_PUBLIC_CALLBACKS = frozenset(target.callback for target in ops.TARGETS.values())
_DISCOVERY_FIELDS = "id,display_phone_number,webhook_configuration"


class MetaPhoneInspectionError(RuntimeError):
    """Only finite application-owned reason codes; no provider exception text."""


def _require(condition, code):
    if not condition:
        raise MetaPhoneInspectionError(code)


def _digest(value):
    return hashlib.sha256(ops._canonical(value)).hexdigest()


def _phone(value):
    _require(isinstance(value, str) and bool(re.fullmatch(r"[+()0-9 -]{1,64}", value)),
             "meta_inspection_phone_shape_invalid")
    result = "+" + re.sub(r"[^0-9]", "", value)
    _require(bool(re.fullmatch(r"\+[1-9][0-9]{7,14}", result)),
             "meta_inspection_phone_shape_invalid")
    return result


@dataclass(frozen=True)
class _LocalBinding:
    target: ops.Target
    sender: MetaSenderSnapshot
    credential: vault.MetaCredential = field(repr=False)
    state_sha256: str = field(repr=False)


def _load_local(session, target, config, now):
    """Inspect the installed binding; installer pre-state 0/6 is not reused."""
    tenant = session.execute(select(TenantProfile).options(lazyload("*")).where(
        TenantProfile.id == target.tenant_id)).scalar_one_or_none()
    _require(tenant is not None and tenant.is_active is True and tenant.slug == target.slug
             and tenant.tipo == "municipio" and tenant.municipio_id is not None,
             "meta_inspection_tenant_mismatch")
    connections = session.scalars(select(ProviderConnection).options(lazyload("*")).where(
        ProviderConnection.tenant_id == target.tenant_id, ProviderConnection.provider == "meta",
        ProviderConnection.channel == "whatsapp", ProviderConnection.environment == target.environment)
        .limit(2)).all()
    senders = session.scalars(select(ProviderSender).options(lazyload("*")).where(
        ProviderSender.channel == "whatsapp", ProviderSender.phone_number_id == target.phone_id)
        .limit(2)).all()
    _require(len(connections) == 1 and len(senders) == 1, "meta_inspection_binding_not_unique")
    connection, sender = connections[0], senders[0]
    _require(connection.external_app_id == ops.APP_ID and connection.external_account_id == target.waba_id
             and connection.credentials_ref == vault.VAULT_REF and connection.status == "connected"
             and sender.tenant_id == target.tenant_id and sender.provider_connection_id == connection.id
             and sender.waba_id == target.waba_id and sender.phone_number == target.phone
             and sender.status == "connected"
             and (target.connection_id is None or connection.id == target.connection_id)
             and (target.sender_id is None or sender.id == target.sender_id),
             "meta_inspection_binding_mismatch")
    owned = session.scalars(select(ProviderSender.id).where(
        ProviderSender.provider_connection_id == connection.id).limit(2)).all()
    _require(owned == [sender.id], "meta_inspection_connection_has_other_senders")
    credential = vault.open_token(connection=connection, tenant_id=target.tenant_id,
        now=now, app_config=config)
    _require(credential.revision >= target.expected_revision + 1, "meta_inspection_credential_not_installed")
    snapshot = MetaSenderSnapshot(target.tenant_id, connection.id, sender.id, ops.APP_ID,
        target.waba_id, target.phone_id, target.environment)
    fingerprint = _digest({"tenant": {"id": tenant.id, "slug": tenant.slug, "active": tenant.is_active,
        "kind": tenant.tipo, "owner": tenant.municipio_id},
        "connection": ops._columns(connection), "sender": ops._columns(sender)})
    return _LocalBinding(target, snapshot, credential, fingerprint)


class _GraphReads:
    """No generic endpoint, pagination URL, caller params, POST or retry."""
    def __init__(self, binding, config, getter):
        self.binding, self.getter = binding, getter
        self.config = ops._authority_config(binding.target, config)
        version = self.config.get(GRAPH_VERSION_KEY, "v25.0")
        _require(isinstance(version, str) and bool(re.fullmatch(r"v[1-9][0-9]{0,2}\.0", version)),
                 "meta_inspection_graph_version_invalid")
        self.version = version
        self.base = "https://graph.facebook.com/" + version + "/"
        self.allowed = {self.base + suffix for suffix in (
            "debug_token", binding.target.waba_id + "/phone_numbers",
            binding.target.waba_id + "/subscribed_apps")}
        self.observed = {}
        self.calls = 0

    def get(self, method, url, **kwargs):
        _require(method == "GET" and url in self.allowed and self.calls < 5
                 and kwargs.get("allow_redirects") is False and kwargs.get("verify") is True,
                 "meta_inspection_get_only")
        require_outbox_time_remaining()
        self.calls += 1
        if url.endswith("/phone_numbers"):
            kwargs["params"] = {"fields": "id,display_phone_number", "limit": "100"}
        try:
            response = self.getter("GET", url, **kwargs)
            payload = _object_response(response)
        except MetaRuntimeError:
            raise
        except Exception:
            raise MetaPhoneInspectionError("meta_inspection_provider_unavailable") from None
        # debug_token is consumed by the authority verifier but never retained.
        if not url.endswith("/debug_token"):
            self.observed[url] = payload
        require_outbox_time_remaining()
        return response

    def authority(self, clock):
        return MetaOutboundRuntime(self.config, get=self.get, clock=clock).authority(
            self.binding.sender, self.binding.credential)

    def discovery(self, phone_id):
        vault.meta_id(phone_id)
        url = self.base + phone_id
        self.allowed.add(url)
        self.get("GET", url, headers={"Authorization": "Bearer " + self.binding.credential.access_token},
            params={"fields": _DISCOVERY_FIELDS}, allow_redirects=False, verify=True)
        return self.observed.pop(url)


def _inventory(reader):
    payload = reader.observed[reader.base + reader.binding.target.waba_id + "/phone_numbers"]
    rows = payload.get("data")
    _require(isinstance(rows, list) and len(rows) <= 100, "meta_inspection_inventory_invalid")
    _require(isinstance(payload.get("paging", {}), dict) and not payload.get("paging", {}).get("next"),
             "meta_inspection_inventory_incomplete")
    result, seen = [], set()
    for row in rows:
        _require(isinstance(row, dict), "meta_inspection_inventory_invalid")
        identifier, phone = vault.meta_id(row.get("id")), _phone(row.get("display_phone_number"))
        _require(identifier not in seen, "meta_inspection_inventory_ambiguous")
        seen.add(identifier)
        result.append({"id": identifier, "phone": phone})
    target = reader.binding.target
    _require(sum(row == {"id": target.phone_id, "phone": target.phone} for row in result) == 1
             and sum(row["phone"] == target.phone for row in result) == 1,
             "meta_inspection_inventory_binding_mismatch")
    return result


def _kind(value):
    if value is None:
        return "null"
    return {str: "string", dict: "object", list: "array", bool: "boolean", int: "number", float: "number"}.get(type(value), "unknown")


def _configuration(payload, *, expected_id, expected_phone, expected_callback):
    _require(payload.get("id") == expected_id and _phone(payload.get("display_phone_number")) == expected_phone,
             "meta_inspection_discovery_identity_mismatch")
    if "webhook_configuration" not in payload:
        return {"state": "missing", "reason_code": "meta_inspection_configuration_missing",
                "schema_verified": False, "configuration_sha256": None, "known_fields": {}}
    value = payload["webhook_configuration"]
    projected = {"state": "unknown_shape", "schema_verified": False,
        "reason_code": "meta_inspection_configuration_schema_unverified",
        "configuration_sha256": _digest(value), "value_type": _kind(value), "known_fields": {}}
    if not isinstance(value, dict):
        return projected
    projected["unknown_field_count"] = len(set(value) - set(_CONFIG_FIELDS))
    for name in _CONFIG_FIELDS:
        if name not in value:
            continue
        item = value[name]
        projected["known_fields"][name] = {
            "value_type": _kind(item), "value_sha256": _digest(item),
            "matches_expected_callback": isinstance(item, str) and item == expected_callback,
            # No unknown origin, userinfo, path, query, fragment or raw value.
            "public_callback": item if isinstance(item, str) and item in _PUBLIC_CALLBACKS else None,
        }
    if value and not projected["unknown_field_count"] and all(isinstance(v, str) for v in value.values()):
        projected["state"] = "candidate_shape"
    return projected


def _discover(reader, *, label, phone_id, phone, expected_callback):
    result = {"resource": label, "phone_number_id": phone_id, "phone_number": phone,
              "waba_id": reader.binding.target.waba_id, "graph_version": reader.version}
    try:
        result["configuration"] = _configuration(reader.discovery(phone_id), expected_id=phone_id,
            expected_phone=phone, expected_callback=expected_callback)
    except (MetaPhoneInspectionError, MetaRuntimeError):
        # Provider details and arbitrary keys never cross this projection.
        result["configuration"] = {"state": "unavailable", "schema_verified": False,
            "reason_code": "meta_inspection_configuration_unavailable", "configuration_sha256": None,
            "known_fields": {}}
    return result


def inspect(body, actor, config, *, engine=None, graph_get=None, clock=None):
    _require(isinstance(body, dict) and body == {"target": "junin-production"}, "meta_inspection_target_invalid")
    engine = db.engine if engine is None else engine
    graph_get = native_graph_request if graph_get is None else graph_get
    clock = (lambda: int(time.time())) if clock is None else clock
    now = clock()
    _require(type(now) is int and now > 0, "meta_inspection_clock_invalid")
    fingerprint = admin.database_identity(engine, config)
    install_outbox_database_timeout_hook(engine)
    with activate_outbox_execution_budget(deadline_monotonic=time.monotonic() + 35,
            io_timeout_cap_seconds=4, cleanup_reserve_seconds=3):
        with admin._transaction(engine, read_only=True) as session:
            admin._validate_actor(session, actor, now=now)
            bindings = tuple(_load_local(session, target, config, now) for target in ops.TARGETS.values())
        # Both tokens are loaded and the transaction is closed before any Graph GET.
        readers, proofs, inventories = [], [], []
        for binding in bindings:
            reader = _GraphReads(binding, config, graph_get)
            proofs.append(reader.authority(clock))
            inventories.append(_inventory(reader))
            readers.append(reader)
        junin, tdf = readers
        matches = [row for row in inventories[0] if row["phone"] == INMOVAR_PHONE]
        blockers = ["meta_inspection_readback_contract_unverified", "meta_inspection_rollback_contract_unverified",
                    "meta_inspection_mutation_not_implemented"]
        observations = [_discover(junin, label="junin", phone_id=bindings[0].target.phone_id,
            phone=bindings[0].target.phone, expected_callback=bindings[0].target.callback)]
        if len(matches) == 1:
            observations.append(_discover(junin, label="inmovar", phone_id=matches[0]["id"],
                phone=INMOVAR_PHONE, expected_callback=None))
        else:
            blockers.append("meta_inspection_inmovar_not_unique")
            observations.append({"resource": "inmovar", "phone_number_id": None,
                "phone_number": INMOVAR_PHONE, "waba_id": bindings[0].target.waba_id,
                "graph_version": junin.version, "configuration": {"state": "not_requested",
                    "schema_verified": False, "configuration_sha256": None, "known_fields": {},
                    "reason_code": "meta_inspection_inmovar_not_unique"}})
        observations.append(_discover(tdf, label="tdf", phone_id=bindings[1].target.phone_id,
            phone=bindings[1].target.phone, expected_callback=bindings[1].target.callback))
        final_now = clock()
        _require(type(final_now) is int and now <= final_now < min(proof.valid_until for proof in proofs),
                 "meta_inspection_observation_expired")
        with admin._transaction(engine, read_only=True) as session:
            admin._validate_actor(session, actor, now=final_now)
            for original in bindings:
                current = _load_local(session, original.target, config, final_now)
                _require(current.sender == original.sender
                         and current.credential.revision == original.credential.revision
                         and hmac.compare_digest(current.state_sha256, original.state_sha256),
                         "meta_inspection_binding_changed")
        # Closing the database transaction can consume the remaining proof or
        # session lifetime; evidence is checked at response time, not before I/O.
        require_outbox_time_remaining()
        checked_at = clock()
        valid_until = min(actor.expires_at, *(proof.valid_until for proof in proofs))
        _require(type(checked_at) is int and final_now <= checked_at < valid_until,
                 "meta_inspection_observation_expired")
        return {"contract": CONTRACT, "target": "junin-production", "observed_at": now,
            "checked_at": checked_at, "valid_until": valid_until,
            "database_identity_sha256": fingerprint, "inspection_complete": False,
            "write_ready": False, "blockers": blockers, "resources": observations,
            "credentials": [{"target": binding.target.name, "tenant_id": binding.target.tenant_id,
                "environment": binding.target.environment, "connection_id": binding.sender.connection_id,
                "sender_id": binding.sender.sender_id, "revision": binding.credential.revision,
                "expires_at": binding.credential.expires_at, "authority_verified": True,
                "graph_version": reader.version, "inventory_complete": True,
                "inventory_sha256": _digest(sorted(inventory, key=lambda row: row["id"])),
                "subscriptions_sha256": _digest(reader.observed[reader.base + binding.target.waba_id + "/subscribed_apps"])}
                for binding, reader, inventory in zip(bindings, readers, inventories)],
            "writes_performed": False, "provider_mutations": False, "messages_sent": False}
