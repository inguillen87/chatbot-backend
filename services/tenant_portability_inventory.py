"""Internal case-graph inventory, not a complete AIF return or restore.

No HTTP route, storage client, URL fetch, filesystem reader or writer. Object
adapters are server code receiving only persisted attachment/tenant IDs. The
registry deliberately retains all unimplemented AIF domains as incomplete.
"""
from __future__ import annotations

import base64
from datetime import date, datetime
import hashlib
import json
import math
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import and_, exists, inspect, not_, or_

from models import (
    db, ArchivoAdjunto, AuditEvent, InboxTicketArtifact, MunicipioTicket,
    MunicipioTicketReplyEvent, PymeTicket, Role, TenantProfile, TenantTicket,
    TenantTicketReplyEvent, TicketComentario, User, UserRole,
)
from utils.auth_helpers import is_user_auth_disabled
from utils.tenant_admin_access import can_manage_tenant_control_plane
from services.tenant_ticket_scope import _is_structurally_municipal_tenant, municipio_ticket_scope_filter

CONTRACT = "tenant.portability.inventory.v1"
MAX_ROWS_PER_MODEL = 10000
MAX_OBJECT_BYTES = 15 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 16 * 1024 * 1024
DOMAINS = ("data", "objects", "audit", "config_prompts", "vectors", "backup", "restore")
_SECRET_KEY = re.compile(r"password|secret|credential|authorization|api.?key|token|signed.?url|(?:^|[_-])(?:pin|otp)(?:$|[_-])", re.I)
_URL = re.compile(r"(?:https?://|r2-private://)[^\s<>\"']+", re.I)
_CASES = {"TenantTicket": TenantTicket, "MunicipioTicket": MunicipioTicket, "PymeTicket": PymeTicket}
_AUDIT_CASES = {"tenant_ticket": "TenantTicket", "municipio_ticket": "MunicipioTicket", "pyme_ticket": "PymeTicket"}
_FIELDS = {
    "TenantTicket": ("user_id", "categoria", "descripcion", "estado", "origen", "latitud", "longitud", "datos_extra", "created_at", "updated_at"),
    "MunicipioTicket": ("asunto", "categoria", "pregunta", "detalles", "estado", "asignado_a_id", "asignado_en", "direccion", "latitud", "longitud", "fecha", "nombre_vecino", "telefono_vecino", "email_vecino", "datos_extra"),
    "PymeTicket": ("asunto", "categoria", "pregunta", "estado", "asignado_a_id", "asignado_en", "direccion", "latitud", "longitud", "telefono", "email", "fecha", "datos_extra"),
    "TenantTicketReplyEvent": ("ticket_id", "event_id", "body", "visibility", "actor_user_id", "actor_name", "actor_role", "recipient_email", "recipient_phone", "whatsapp_delivery_status", "created_at"),
    "MunicipioTicketReplyEvent": ("source_model", "ticket_id", "comment_id", "event_id", "body", "visibility", "actor_user_id", "actor_name", "actor_role", "recipient_phone", "whatsapp_delivery_status", "created_at"),
    "TicketComentario": ("municipio_ticket_id", "pyme_ticket_id", "comentario", "fecha", "user_id", "es_admin", "origen", "estado_ticket", "archivo_adjunto_id"),
    "InboxTicketArtifact": ("source_model", "ticket_id", "action", "payload_json", "actor_user_id", "created_at"),
    "AuditEvent": ("actor_user_id", "event_type", "resource_type", "resource_id", "details", "created_at"),
    "ArchivoAdjunto": ("municipio_ticket_id", "pyme_ticket_id", "nombre_original", "mime", "tamano", "tipo", "fecha"),
}
DICTIONARY = {
    model: {field: {"encoding": "json", "source": field} for field in fields}
    for model, fields in _FIELDS.items()
}
DICTIONARY["TenantTicket"]["presentation_comments"] = {
    "encoding": "json", "source": "datos_extra.comments", "meaning": "bounded presentation, not immutable history",
}
DICTIONARY["_identity"] = {"key": "model:id", "tenant_id": "persisted or proven case relationship",
    "object_path": "objects/<sha256>; bytes are base64 in this open JSON inventory"}
DICTIONARY["_tenant_scope"] = {"id": "persisted tenant primary key", "slug": "persisted tenant slug",
    "type": "persisted organization domain", "municipio_id": "municipal owner reference, nullable",
    "pyme_id": "business owner reference, nullable"}


class PortabilityError(ValueError):
    """Value-free internal error; no request or provider body is exposed."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _key(model, row_id):
    return f"{model}:{row_id}"


def _issue(issues, code, key=None):
    value = {"code": code, "key": key}
    if value not in issues:
        issues.append(value)


def _safe(value, issues, owner):
    """Non-finite numbers degrade to null plus an issue, never coordinates."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        out = {}
        for name, item in value.items():
            if not isinstance(name, str) or _URL.search(name):
                _issue(issues, "metadata_key_unsupported", owner)
                continue
            if _SECRET_KEY.search(name):
                out[name] = {"redacted": True}
                _issue(issues, "security_field_redacted", owner)
            else:
                out[name] = _safe(item, issues, owner)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe(item, issues, owner) for item in value]
    if isinstance(value, str):
        def replace(match):
            try:
                url = urlsplit(match.group())
            except ValueError:
                _issue(issues, "url_value_unsupported", owner)
                return "[URL omitted]"
            if url.scheme == "r2-private" or url.query or url.fragment or url.username or url.password:
                _issue(issues, "private_or_parameterized_url_redacted", owner)
                return "[URL omitted]"
            return match.group()
        return _URL.sub(replace, value)
    if type(value) is float and not math.isfinite(value):
        _issue(issues, "non_finite_number_omitted", owner)
        return None
    if value is None or type(value) in (bool, int, float):
        return value
    _issue(issues, "value_type_unsupported", owner)
    return {"unsupported": True}


def _bounded(query, model, issues):
    rows = query.order_by(model.id.asc()).limit(MAX_ROWS_PER_MODEL + 1).all()
    if len(rows) > MAX_ROWS_PER_MODEL:
        _issue(issues, "row_budget_exceeded", model.__name__)
    return rows[:MAX_ROWS_PER_MODEL]


def _authorize(actor, tenant):
    # Fail conservatively if any control-plane identity in this Session has
    # pending changes. Never flush, reset WIP or promote identity-map values.
    if not isinstance(actor, User) or not isinstance(tenant, TenantProfile):
        raise PortabilityError("portability_admin_required")
    session = db.session()
    pending = (*session.new, *session.dirty, *session.deleted)
    if any(isinstance(row, (User, TenantProfile, Role, UserRole)) for row in pending):
        raise PortabilityError("portability_pending_state")
    identities = []
    for row in (actor, tenant):
        state = inspect(row)
        if (not state.persistent or state.session is not session or not state.identity
            or len(state.identity) != 1 or type(state.identity[0]) is not int or state.identity[0] <= 0
            or session.identity_map.get(state.key) is not row):
            raise PortabilityError("portability_persistent_identity_required")
        identities.append(state.identity[0])
    with db.session.no_autoflush:
        stored_actor = db.session.get(User, identities[0], populate_existing=True)
        stored_tenant = db.session.get(TenantProfile, identities[1], populate_existing=True)
        if (stored_actor is None or stored_tenant is None or is_user_auth_disabled(stored_actor)
            or not can_manage_tenant_control_plane(stored_actor, stored_tenant)):
            raise PortabilityError("portability_admin_required")
    return stored_actor, stored_tenant


def _private_reference_matches(reference, slug):
    if not isinstance(reference, str):
        return False
    try:
        value = urlsplit(reference)
    except ValueError:
        return False
    return (value.scheme == "r2-private" and bool(value.netloc) and not value.query
        and not value.fragment and not value.username and not value.password
        and re.fullmatch(r"/private-attachments/[a-f0-9]{32}/tenants/" + re.escape(slug)
                         + r"/attachments/[a-f0-9]{32}(?:\.[a-z0-9]{1,12})?", value.path) is not None)


def build_case_inventory(actor, tenant, *, object_reader=None):
    """Return a bounded internal inventory using the existing control-plane gate.

    object_reader(attachment_id, tenant_id) is supplied only by server code.
    Absence of that adapter records missing bytes, never zero objects or success.
    This service never commits, writes an audit/grant, creates a DB or restores.
    """
    if object_reader is not None and not callable(object_reader):
        raise PortabilityError("portability_server_adapter_required")
    with db.session.no_autoflush:
        _, tenant = _authorize(actor, tenant)
        return _build_case_inventory(tenant, object_reader)


def _build_case_inventory(tenant, object_reader):
    records, edges, objects, issues, case_ids = [], [], [], [], {}
    comment_attachments, object_bytes = [], 0

    def add(model, row, *, derived=False):
        key = _key(model, row.id)
        if not derived and getattr(row, "tenant_id", None) != tenant.id:
            raise PortabilityError("portability_foreign_record")
        data = {name: _safe(getattr(row, name), issues, key) for name in _FIELDS[model]}
        if model == "TenantTicket":
            extra = row.datos_extra if isinstance(row.datos_extra, dict) else {}
            data["presentation_comments"] = _safe(extra.get("comments", []), issues, key)
            # Canonical attachment snapshots need their own proven storage adapter.
            if any(extra.get(name) for name in ("attachments", "archivos_adjuntos", "attachmentInfo", "attachment_info", "source_attachment")):
                _issue(issues, "canonical_attachment_binding_unsupported", key)
        record = {"key": key, "model": model, "id": row.id, "tenant_id": tenant.id, "data": data}
        records.append(record)
        return key

    def excluded(query, model, predicate, code):
        # Existence/ID only: quarantined bodies, payloads and objects are not
        # materialized merely to report why they were excluded.
        row = query.with_entities(model.id).filter(or_(not_(predicate), predicate.is_(None))).order_by(model.id.asc()).first()
        if row is not None:
            _issue(issues, code, _key(model.__name__, row[0]))

    for name, model in _CASES.items():
        query = model.query.filter(model.tenant_id == tenant.id)
        if name == "MunicipioTicket":
            predicate = municipio_ticket_scope_filter(tenant)
            excluded(query, model, predicate, "municipal_domain_quarantined")
            query = query.filter(predicate)
        rows = _bounded(query, model, issues)
        case_ids[name] = {row.id for row in rows}
        for row in rows:
            add(name, row)

    municipal_comment = TicketComentario.municipio_ticket_id.in_(case_ids["MunicipioTicket"])
    business_comment = TicketComentario.pyme_ticket_id.in_(case_ids["PymeTicket"])
    comment_query = TicketComentario.query.filter(or_(municipal_comment, business_comment))
    comment_scope = or_(and_(municipal_comment, TicketComentario.pyme_ticket_id.is_(None)),
                        and_(business_comment, TicketComentario.municipio_ticket_id.is_(None)))
    excluded(comment_query, TicketComentario, comment_scope, "legacy_relation_ambiguous")
    legacy_comments = _bounded(comment_query.filter(comment_scope), TicketComentario, issues)
    proven_municipal_comments = {row.id: row.municipio_ticket_id for row in legacy_comments
                                if row.municipio_ticket_id in case_ids["MunicipioTicket"] and row.pyme_ticket_id is None}

    def link(row, model_name, target_model, target_id, relation, *, derived=False):
        source = _key(model_name, row.id)
        if target_model not in case_ids or target_id not in case_ids[target_model]:
            _issue(issues, "case_relation_unresolved", source)
            return False
        add(model_name, row, derived=derived)
        edges.append({"from": source, "to": _key(target_model, target_id), "relation": relation})
        return True

    for name, model, target in (
        ("TenantTicketReplyEvent", TenantTicketReplyEvent, "TenantTicket"),
        ("MunicipioTicketReplyEvent", MunicipioTicketReplyEvent, "MunicipioTicket"),
    ):
        query = model.query.filter(model.tenant_id == tenant.id)
        predicate = model.ticket_id.in_(case_ids[target])
        excluded(query, model, predicate, "case_relation_unresolved")
        query = query.filter(predicate)
        if name == "MunicipioTicketReplyEvent":
            comment_scope = exists().where(TicketComentario.id == model.comment_id,
                TicketComentario.municipio_ticket_id == model.ticket_id, TicketComentario.pyme_ticket_id.is_(None))
            excluded(query, model, comment_scope, "municipal_reply_comment_unresolved")
            query = query.filter(comment_scope)
        for row in _bounded(query, model, issues):
            if name == "MunicipioTicketReplyEvent" and proven_municipal_comments.get(row.comment_id) != row.ticket_id:
                _issue(issues, "municipal_reply_comment_unresolved", _key(name, row.id))
                continue
            link(row, name, target, row.ticket_id, "immutable_reply")

    artifact_query = InboxTicketArtifact.query.filter_by(tenant_id=tenant.id)
    artifact_scope = or_(*(and_(InboxTicketArtifact.source_model == model,
        InboxTicketArtifact.ticket_id.in_(case_ids[model])) for model in ("TenantTicket", "MunicipioTicket")))
    excluded(artifact_query, InboxTicketArtifact, artifact_scope, "case_relation_unresolved")
    for row in _bounded(artifact_query.filter(artifact_scope), InboxTicketArtifact, issues):
        link(row, "InboxTicketArtifact", row.source_model, row.ticket_id, "artifact")
        if row.action == "attach_file":
            _issue(issues, "artifact_object_adapter_unsupported", _key("InboxTicketArtifact", row.id))

    audit_query = AuditEvent.query.filter_by(tenant_id=tenant.id)
    audit_scope = or_(*(and_(AuditEvent.resource_type == resource,
        AuditEvent.resource_id.in_([str(identifier) for identifier in case_ids[model]])) for resource, model in _AUDIT_CASES.items()))
    excluded(audit_query, AuditEvent, audit_scope, "audit_relation_unsupported")
    for row in _bounded(audit_query.filter(audit_scope), AuditEvent, issues):
        target = _AUDIT_CASES.get(row.resource_type)
        reference = row.resource_id
        if target and isinstance(reference, str) and re.fullmatch(r"[1-9][0-9]*", reference):
            link(row, "AuditEvent", target, int(reference), "audit")
        else:
            _issue(issues, "audit_relation_unsupported", _key("AuditEvent", row.id))

    for row in legacy_comments:
        associations = [("MunicipioTicket", row.municipio_ticket_id), ("PymeTicket", row.pyme_ticket_id)]
        present = [(name, identifier) for name, identifier in associations if identifier is not None]
        if len(present) != 1:
            _issue(issues, "legacy_relation_ambiguous", _key("TicketComentario", row.id))
        else:
            if link(row, "TicketComentario", *present[0], "legacy_comment", derived=True) and row.archivo_adjunto_id is not None:
                comment_attachments.append((_key("TicketComentario", row.id), _key("ArchivoAdjunto", row.archivo_adjunto_id), _key(*present[0])))

    municipal_attachment = ArchivoAdjunto.municipio_ticket_id.in_(case_ids["MunicipioTicket"])
    business_attachment = ArchivoAdjunto.pyme_ticket_id.in_(case_ids["PymeTicket"])
    attachments = ArchivoAdjunto.query.filter(or_(municipal_attachment, business_attachment))
    attachment_scope = or_(and_(municipal_attachment, ArchivoAdjunto.pyme_ticket_id.is_(None)),
                           and_(business_attachment, ArchivoAdjunto.municipio_ticket_id.is_(None)))
    excluded(attachments, ArchivoAdjunto, attachment_scope, "attachment_relation_ambiguous")
    for row in _bounded(attachments.filter(attachment_scope), ArchivoAdjunto, issues):
        key = _key("ArchivoAdjunto", row.id)
        present = [(name, identifier) for name, identifier in (
            ("MunicipioTicket", row.municipio_ticket_id), ("PymeTicket", row.pyme_ticket_id)) if identifier is not None]
        if len(present) != 1 or not link(row, "ArchivoAdjunto", *present[0], "attachment", derived=True):
            _issue(issues, "attachment_relation_ambiguous", key)
            continue
        if not _private_reference_matches(row.url, tenant.slug):
            _issue(issues, "attachment_private_binding_unsupported", key)
            continue
        if object_reader is None:
            _issue(issues, "object_reader_missing", key)
            continue
        if object_bytes >= MAX_TOTAL_OBJECT_BYTES:
            _issue(issues, "object_byte_budget_exceeded", key)
            continue
        try:
            content = object_reader(row.id, tenant.id)
        except Exception:
            _issue(issues, "object_unavailable", key)
            continue
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_OBJECT_BYTES or row.tamano != len(content):
            _issue(issues, "object_content_invalid", key)
            continue
        if object_bytes + len(content) > MAX_TOTAL_OBJECT_BYTES:
            _issue(issues, "object_byte_budget_exceeded", key)
            continue
        object_bytes += len(content)
        digest = hashlib.sha256(content).hexdigest()
        objects.append({"owner": key, "path": "objects/" + digest, "byte_size": len(content),
                        "sha256": digest, "base64": base64.b64encode(content).decode("ascii")})

    attachment_cases = {edge["from"]: edge["to"] for edge in edges if edge["relation"] == "attachment"}
    for comment_key, attachment_key, case_key in comment_attachments:
        if attachment_cases.get(attachment_key) != case_key:
            _issue(issues, "comment_attachment_relation_unresolved", comment_key)

    records.sort(key=lambda row: row["key"])
    edges.sort(key=lambda row: (row["from"], row["to"], row["relation"]))
    objects.sort(key=lambda row: row["owner"])
    registry = {
        "data": {"state": "partial", "reason": "case_graph_slice_not_all_tables_unbound_legacy_and_security_fields_not_included"},
        "objects": {"state": "partial", "reason": "legacy_case_objects_only_canonical_and_other_domains_pending"},
        "audit": {"state": "partial", "reason": "case_linked_audit_only_not_all_access_or_ai_audit"},
        "config_prompts": {"state": "unsupported", "reason": "editable_configuration_and_prompt_adapter_pending"},
        "vectors": {"state": "unsupported", "reason": "complete_tenant_vector_enumeration_not_implemented"},
        "backup": {"state": "unavailable", "reason": "backup_catalog_not_observed"},
        "restore": {"state": "unverified", "reason": "no_postgresql_restore_or_reimport_performed"},
    }
    result = {"contract_version": CONTRACT, "scope": "case_graph_slice", "tenant": {"id": tenant.id, "slug": tenant.slug,
              "type": tenant.tipo, "municipio_id": tenant.municipio_id, "pyme_id": tenant.pyme_id},
              "snapshot_consistency": "caller_transaction_not_certified_point_in_time", "records": records,
              "relations": edges, "objects": objects, "dictionary": json.loads(_canonical(DICTIONARY)), "registry": registry,
              "issues": issues, "aif_complete": False}
    result["checksums"] = {name: _digest(result[name]) for name in ("records", "relations", "objects", "dictionary", "registry", "issues")}
    result["checksums"]["payload_sha256"] = _digest(result)
    return result


def verify_case_inventory(bundle):
    """Pure offline integrity/edge verifier; not authenticity or DB restoration."""
    try:
        if set(bundle) != {"contract_version", "scope", "tenant", "snapshot_consistency", "records", "relations", "objects", "dictionary", "registry", "issues", "aif_complete", "checksums"}:
            raise PortabilityError("portability_contract_invalid")
        if bundle["contract_version"] != CONTRACT or bundle["scope"] != "case_graph_slice" or bundle["aif_complete"] is not False:
            raise PortabilityError("portability_contract_invalid")
        if bundle["dictionary"] != DICTIONARY or set(bundle["registry"]) != set(DOMAINS):
            raise PortabilityError("portability_registry_invalid")
        if any(row["state"] not in {"partial", "unsupported", "unavailable", "unverified"} for row in bundle["registry"].values()):
            raise PortabilityError("portability_complete_claim_invalid")
        tenant_id = bundle["tenant"]["id"]
        if type(tenant_id) is not int or tenant_id <= 0 or not isinstance(bundle["tenant"]["slug"], str):
            raise PortabilityError("portability_tenant_invalid")
        check = dict(bundle["checksums"])
        expected = check.pop("payload_sha256")
        unsigned = {**bundle, "checksums": check}
        if _digest(unsigned) != expected or set(check) != {"records", "relations", "objects", "dictionary", "registry", "issues"}:
            raise PortabilityError("portability_checksum_invalid")
        if any(_digest(bundle[name]) != digest for name, digest in check.items()):
            raise PortabilityError("portability_checksum_invalid")
        keys, models, linked, record_index, linked_cases = set(), {}, set(), {}, {}
        municipal_tenant = _is_structurally_municipal_tenant(SimpleNamespace(
            tipo=bundle["tenant"]["type"], municipio_id=bundle["tenant"]["municipio_id"], pyme_id=bundle["tenant"]["pyme_id"]))
        for row in bundle["records"]:
            if (row["model"] not in _FIELDS or type(row["id"]) is not int or row["id"] <= 0
                or row["key"] != _key(row["model"], row["id"]) or row["key"] in keys
                or type(row["tenant_id"]) is not int or row["tenant_id"] != tenant_id):
                raise PortabilityError("portability_record_identity_invalid")
            if row["model"] == "MunicipioTicket" and not municipal_tenant:
                raise PortabilityError("portability_municipal_domain_invalid")
            fields = set(_FIELDS[row["model"]]) | ({"presentation_comments"} if row["model"] == "TenantTicket" else set())
            if set(row["data"]) != fields:
                raise PortabilityError("portability_record_fields_invalid")
            keys.add(row["key"]); models[row["key"]] = row["model"]
            record_index[row["key"]] = row
        for edge in bundle["relations"]:
            if (edge["from"] not in keys or edge["to"] not in keys or models[edge["to"]] not in _CASES
                or edge["from"] in linked):
                raise PortabilityError("portability_relation_invalid")
            source, target = record_index[edge["from"]], record_index[edge["to"]]
            data, relation = source["data"], edge["relation"]
            expected_model, expected_id, expected_relation = None, None, None
            if source["model"] in {"TenantTicketReplyEvent", "MunicipioTicketReplyEvent"}:
                expected_model = "TenantTicket" if source["model"] == "TenantTicketReplyEvent" else "MunicipioTicket"
                expected_id, expected_relation = data["ticket_id"], "immutable_reply"
                if source["model"] == "MunicipioTicketReplyEvent" and data["source_model"] != "MunicipioTicket":
                    raise PortabilityError("portability_relation_invalid")
            elif source["model"] == "InboxTicketArtifact":
                expected_model, expected_id, expected_relation = data["source_model"], data["ticket_id"], "artifact"
            elif source["model"] == "AuditEvent":
                expected_model, expected_relation = _AUDIT_CASES.get(data["resource_type"]), "audit"
                if isinstance(data["resource_id"], str) and re.fullmatch(r"[1-9][0-9]*", data["resource_id"]):
                    expected_id = int(data["resource_id"])
            elif source["model"] in {"ArchivoAdjunto", "TicketComentario"}:
                present = [(name, data[field]) for name, field in (
                    ("MunicipioTicket", "municipio_ticket_id"), ("PymeTicket", "pyme_ticket_id")) if data[field] is not None]
                if len(present) == 1:
                    expected_model, expected_id = present[0]
                expected_relation = "attachment" if source["model"] == "ArchivoAdjunto" else "legacy_comment"
            if (type(expected_id) is not int or expected_id != target["id"]
                or expected_model != target["model"] or relation != expected_relation):
                raise PortabilityError("portability_relation_invalid")
            linked.add(edge["from"])
            linked_cases[edge["from"]] = edge["to"]
        for row in bundle["records"]:
            if row["model"] not in _CASES and row["key"] not in linked:
                raise PortabilityError("portability_orphan_invalid")
            if row["model"] == "MunicipioTicketReplyEvent":
                comment_id = row["data"]["comment_id"]
                if (type(comment_id) is not int or linked_cases.get(_key("TicketComentario", comment_id))
                    != linked_cases[row["key"]]):
                    raise PortabilityError("portability_reply_comment_invalid")
        object_owners = set()
        for item in bundle["objects"]:
            data = base64.b64decode(item["base64"], validate=True)
            digest = hashlib.sha256(data).hexdigest()
            if (item["owner"] not in keys or models[item["owner"]] != "ArchivoAdjunto" or item["owner"] in object_owners
                or item["sha256"] != digest or item["path"] != "objects/" + digest
                or type(item["byte_size"]) is not int or item["byte_size"] != len(data) or not 0 < len(data) <= MAX_OBJECT_BYTES
                or record_index[item["owner"]]["data"]["tamano"] != len(data)):
                raise PortabilityError("portability_object_invalid")
            object_owners.add(item["owner"])
        missing = sorted(key for key, model in models.items() if model == "ArchivoAdjunto" and key not in object_owners)
        unresolved = []
        for row in bundle["records"]:
            if row["model"] == "TicketComentario" and row["data"]["archivo_adjunto_id"] is not None:
                reference = row["data"]["archivo_adjunto_id"]
                if type(reference) is not int or linked_cases.get(_key("ArchivoAdjunto", reference)) != linked_cases[row["key"]]:
                    unresolved.append(row["key"])
        return {"integrity_valid": True, "case_graph_closed": not bundle["issues"] and not missing and not unresolved,
                "missing_object_owners": missing, "aif_complete": False,
                "unresolved_attachment_comments": unresolved, "incomplete_domains": list(DOMAINS), "restoration_verified": False}
    except PortabilityError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PortabilityError("portability_bundle_invalid") from None
