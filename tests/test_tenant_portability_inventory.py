"""New internal inventory contracts: real ORM, synthetic identities/objects.

No HTTP exposure, provider call, persistent DB, restore or full-AIF claim.
"""
import base64
from copy import deepcopy
import hashlib
import json

import pytest
from sqlalchemy import event, inspect

from models import (
    db, ArchivoAdjunto, AuditEvent, InboxTicketArtifact, MunicipioTicket,
    MunicipioTicketReplyEvent, ProviderSender, PymeTicket, Role, TenantProfile, TenantTicketReplyEvent,
    TicketComentario, User, UserRole,
)
from services.tenant_portability_inventory import (
    DOMAINS, PortabilityError, build_case_inventory, verify_case_inventory,
)
from services.v2.ticket_service import create_ticket
from services.tenant_ticket_scope import municipio_ticket_scope_filter


@pytest.fixture
def graph_context(client):
    assert str(db.engine.url) == "sqlite:///:memory:"
    values = []
    for suffix in ("a", "b"):
        owner = User(name="Synthetic portability owner", email=f"portability-{suffix}@example.invalid",
                     rol="admin", tipo_chat="municipio")
        owner.set_password("synthetic-portability-test-only")
        db.session.add(owner); db.session.flush()
        tenant = TenantProfile(slug=f"portability-{suffix}", nombre="Synthetic portability",
                               tipo="municipio", municipio_id=owner.id, is_active=True, plan="enterprise")
        db.session.add(tenant); db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        case = create_ticket(tenant=tenant, actor_user=None, payload={
            "title": "Synthetic case", "description": f"Case of tenant {suffix}", "category": "arbolado", "channel": "web"})
        case.datos_extra = {**case.datos_extra, "comments": [{"id": "presentation-only", "body": "Latest visible summary"}]}
        legacy = MunicipioTicket(id=case.id, tenant_id=tenant.id, municipio_id=owner.id,
                                 pregunta="Synthetic legacy case", categoria="arbolado", estado="nuevo")
        db.session.add(legacy); db.session.flush()
        comment = TicketComentario(municipio_ticket_id=legacy.id, comentario="Persisted legacy comment",
                                    user_id=owner.id, es_admin=True)
        db.session.add(comment); db.session.flush()
        # The immutable municipal event requires a real sender FK. This draft
        # lives only in the disposable SQLite fixture, without a connection,
        # credential, webhook, external registration or delivery configuration.
        sender = ProviderSender(tenant_id=tenant.id, channel="whatsapp", sender_type="whatsapp_business", status="draft")
        db.session.add(sender); db.session.flush()
        reply = TenantTicketReplyEvent(tenant_id=tenant.id, ticket_id=case.id,
            event_id="immutable-" + suffix, body="History outside bounded JSON " + suffix,
            actor_user_id=owner.id, actor_name=owner.name, actor_role="admin", visibility="internal")
        municipal_reply = MunicipioTicketReplyEvent(tenant_id=tenant.id, ticket_id=legacy.id,
            comment_id=comment.id, event_id="municipal-" + suffix, body="Immutable municipal reply " + suffix,
            visibility="public", actor_user_id=owner.id, actor_name=owner.name, actor_role="admin",
            recipient_phone="+5491100000000", whatsapp_policy_snapshot={"synthetic": True},
            whatsapp_provider_sender_id=sender.id)
        artifact = InboxTicketArtifact(tenant_id=tenant.id, source_model="TenantTicket", ticket_id=case.id,
            action="share_location", payload_json={"lat": -54.8, "lon": -68.3}, actor_user_id=owner.id,
            idempotency_key_hash="a" * 64, request_digest="b" * 64)
        data = b"synthetic private attachment"
        attachment = ArchivoAdjunto(municipio_ticket_id=legacy.id, filename="opaque.bin", nombre_original="evidence.bin",
            mime="application/octet-stream", tamano=len(data), tipo="chat_adjunto",
            url=f"r2-private://synthetic-private/private-attachments/{'a' * 32}/tenants/{tenant.slug}/attachments/{'b' * 32}.bin")
        audit = AuditEvent(tenant_id=tenant.id, actor_user_id=owner.id, event_type="synthetic.case_event",
                           resource_type="municipio_ticket", resource_id=str(legacy.id), details={"state": "nuevo"})
        db.session.add_all([reply, municipal_reply, artifact, attachment, audit])
        values.append({"owner": owner, "tenant": tenant, "case": case, "legacy": legacy,
                       "comment": comment, "reply": reply, "municipal_reply": municipal_reply,
                       "artifact": artifact, "attachment": attachment, "bytes": data})
    db.session.commit()
    return values


def _reader(record):
    attachment_id, tenant_id, value = record["attachment"].id, record["tenant"].id, record["bytes"]
    def read(identifier, scope):
        assert (identifier, scope) == (attachment_id, tenant_id)
        return value
    return read


def _build(own):
    return build_case_inventory(own["owner"], own["tenant"], object_reader=_reader(own))


def _reseal(bundle):
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    bundle["checksums"] = {name: digest(bundle[name]) for name in ("records", "relations", "objects", "dictionary", "registry", "issues")}
    bundle["checksums"]["payload_sha256"] = digest(bundle)


def test_real_graph_preserves_model_identity_immutable_history_and_private_bytes(graph_context):
    own, foreign = graph_context
    inventory = _build(own)
    # Actual JSON round-trip, including reordered object keys.
    inventory = json.loads(json.dumps(inventory, sort_keys=True))
    checked = verify_case_inventory(inventory)
    assert checked["integrity_valid"] and checked["case_graph_closed"]
    assert checked["aif_complete"] is False and checked["restoration_verified"] is False
    assert set(checked["incomplete_domains"]) == set(DOMAINS)
    keys = {row["key"] for row in inventory["records"]}
    assert {f"TenantTicket:{own['case'].id}", f"MunicipioTicket:{own['case'].id}"}.issubset(keys)
    assert f"TenantTicket:{foreign['case'].id}" not in keys
    actual = next(row for row in inventory["records"] if row["model"] == "TenantTicket")
    assert actual["data"]["presentation_comments"] == own["case"].datos_extra["comments"]
    reply = next(row for row in inventory["records"] if row["model"] == "TenantTicketReplyEvent")
    assert reply["data"]["body"] == "History outside bounded JSON a"
    assert "History outside bounded JSON a" not in json.dumps(actual["data"]["presentation_comments"])
    assert base64.b64decode(inventory["objects"][0]["base64"]) == own["bytes"]
    assert "r2-private://" not in json.dumps(inventory)
    assert all(row["tenant_id"] == own["tenant"].id for row in inventory["records"])


def test_category_scoped_employee_cannot_build_complete_tenant_inventory(graph_context):
    own, _ = graph_context
    employee = User(name="Synthetic operator", email="portability-operator@example.invalid", rol="empleado",
        tipo_chat="municipio", tenant_id=own["tenant"].id, tenant_slug=own["tenant"].slug,
        municipio_id=own["owner"].id, es_empleado=True)
    employee.set_password("synthetic-portability-test-only")
    employee.categorias_lista = ["arbolado"]
    employee.accesibilidad = {"employee_scope": {"categorias": ["arbolado"], "permisos": ["tickets.read"]}}
    db.session.add(employee); db.session.commit()
    with pytest.raises(PortabilityError, match="portability_admin_required"):
        build_case_inventory(employee, own["tenant"])


def test_other_tenant_admin_and_boolean_authority_are_denied(graph_context):
    own, foreign = graph_context
    for actor in (foreign["owner"], True, None):
        with pytest.raises(PortabilityError, match="portability_admin_required"):
            build_case_inventory(actor, own["tenant"])
    with pytest.raises(PortabilityError, match="portability_server_adapter_required"):
        build_case_inventory(own["owner"], own["tenant"], object_reader={"url": "https://example.invalid"})


def test_own_artifact_pointing_to_foreign_ticket_is_not_exported_or_closed(graph_context):
    own, foreign = graph_context
    own["artifact"].ticket_id = foreign["case"].id
    db.session.commit()
    inventory = _build(own)
    assert not any(row["key"] == f"InboxTicketArtifact:{own['artifact'].id}" for row in inventory["records"])
    assert verify_case_inventory(inventory)["case_graph_closed"] is False
    assert any(issue["code"] == "case_relation_unresolved" for issue in inventory["issues"])


def test_orphan_artifact_is_explicitly_incomplete(graph_context):
    own, _ = graph_context
    own["artifact"].ticket_id = 987654321  # This polymorphic field has no FK; no constraints are disabled.
    db.session.commit()
    inventory = _build(own)
    assert verify_case_inventory(inventory)["case_graph_closed"] is False
    assert any(issue["key"] == f"InboxTicketArtifact:{own['artifact'].id}" for issue in inventory["issues"])


def test_missing_object_adapter_never_becomes_zero_or_complete(graph_context):
    own, _ = graph_context
    inventory = build_case_inventory(own["owner"], own["tenant"])
    checked = verify_case_inventory(inventory)
    assert checked["case_graph_closed"] is False
    assert checked["missing_object_owners"] == [f"ArchivoAdjunto:{own['attachment'].id}"]
    assert any(issue["code"] == "object_reader_missing" for issue in inventory["issues"])


def test_missing_object_bytes_preserve_record_and_explicit_failure(graph_context):
    own, _ = graph_context
    def missing(identifier, tenant_id):
        raise FileNotFoundError("synthetic object missing")
    inventory = build_case_inventory(own["owner"], own["tenant"], object_reader=missing)
    assert any(row["key"] == f"ArchivoAdjunto:{own['attachment'].id}" for row in inventory["records"])
    assert verify_case_inventory(inventory)["case_graph_closed"] is False
    assert {issue["code"] for issue in inventory["issues"]} == {"object_unavailable"}


def test_offline_verifier_rejects_bitflip_even_with_new_envelope_checksums(graph_context):
    own, _ = graph_context
    inventory = _build(own)
    inventory["objects"][0]["base64"] = base64.b64encode(b"X" + own["bytes"][1:]).decode()
    _reseal(inventory)
    with pytest.raises(PortabilityError, match="portability_object_invalid"):
        verify_case_inventory(inventory)


def test_numeric_id_collision_cannot_retarget_artifact_or_audit_offline(graph_context):
    own, _ = graph_context
    original = _build(own)
    for model in ("InboxTicketArtifact", "AuditEvent"):
        inventory = deepcopy(original)
        edge = next(item for item in inventory["relations"] if item["from"].startswith(model + ":"))
        edge["to"] = ("MunicipioTicket:" if edge["to"].startswith("TenantTicket:") else "TenantTicket:") + str(own["case"].id)
        _reseal(inventory)
        with pytest.raises(PortabilityError, match="portability_relation_invalid"):
            verify_case_inventory(inventory)


def test_offline_verifier_rejects_foreign_record_with_recomputed_checksums(graph_context):
    own, foreign = graph_context
    inventory = _build(own)
    inventory["records"][0]["tenant_id"] = foreign["tenant"].id
    _reseal(inventory)
    with pytest.raises(PortabilityError, match="portability_record_identity_invalid"):
        verify_case_inventory(inventory)


def test_signed_urls_and_credential_values_are_not_bundled(graph_context):
    own, _ = graph_context
    audit = AuditEvent.query.filter_by(tenant_id=own["tenant"].id).first()
    audit.details = {"api_key": "SYNTHETIC-CREDENTIAL-MARKER", "link": "https://example.invalid/object?signature=SYNTHETIC-SIGNED-MARKER",
                     "pin": "SYNTHETIC-PIN-KEY", "consulta_pin": "SYNTHETIC-CONSULTA-PIN", "otp": "SYNTHETIC-OTP-KEY"}
    db.session.commit()
    inventory = _build(own)
    raw = json.dumps(inventory)
    assert "SYNTHETIC-CREDENTIAL-MARKER" not in raw and "SYNTHETIC-SIGNED-MARKER" not in raw
    assert all(value not in raw for value in ("SYNTHETIC-PIN-KEY", "SYNTHETIC-CONSULTA-PIN", "SYNTHETIC-OTP-KEY"))
    assert verify_case_inventory(inventory)["case_graph_closed"] is False


def test_registry_cannot_claim_aif_complete_or_omit_backup(graph_context):
    own, _ = graph_context
    for mutate in (lambda item: item.update(aif_complete=True), lambda item: item["registry"].pop("backup")):
        inventory = _build(own)
        mutate(inventory); _reseal(inventory)
        with pytest.raises(PortabilityError):
            verify_case_inventory(inventory)


def test_foreign_object_reference_never_calls_server_reader(graph_context):
    own, foreign = graph_context
    own["attachment"].url = foreign["attachment"].url
    db.session.commit()
    calls = []
    def forbidden(identifier, tenant_id):
        calls.append((identifier, tenant_id))
        return own["bytes"]
    inventory = build_case_inventory(own["owner"], own["tenant"], object_reader=forbidden)
    assert calls == [] and verify_case_inventory(inventory)["case_graph_closed"] is False
    assert any(issue["code"] == "attachment_private_binding_unsupported" for issue in inventory["issues"])


def test_ambiguous_legacy_relation_is_reported_without_model_guessing(graph_context):
    own, foreign = graph_context
    other = PymeTicket(tenant_id=foreign["tenant"].id, pregunta="Foreign business case", nro_ticket=984125)
    db.session.add(other); db.session.flush()
    own["comment"].pyme_ticket_id = other.id
    db.session.commit()
    inventory = _build(own)
    assert not any(row["key"] == f"TicketComentario:{own['comment'].id}" for row in inventory["records"])
    assert any(issue["code"] == "legacy_relation_ambiguous" for issue in inventory["issues"])
    assert verify_case_inventory(inventory)["case_graph_closed"] is False


def test_inventory_does_not_flush_pending_writes_or_mutate_preferences(graph_context):
    own, _ = graph_context
    reader = _reader(own)  # Load expired fixture IDs before introducing a pending write.
    pending = AuditEvent(tenant_id=own["tenant"].id, event_type="not_committed", resource_type="tenant_ticket",
                         resource_id=str(own["case"].id))
    db.session.add(pending)
    statements = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement.split()[0])
    event.listen(db.engine, "before_cursor_execute", observe)
    try:
        inventory = build_case_inventory(own["owner"], own["tenant"], object_reader=reader)
    finally:
        event.remove(db.engine, "before_cursor_execute", observe)
    assert statements == [] and pending in db.session.new and pending.id is None
    assert not any(row["model"] == "AuditEvent" and row["data"]["event_type"] == "not_committed" for row in inventory["records"])


def test_attachment_referenced_only_by_comment_is_explicitly_unresolved(graph_context):
    own, _ = graph_context
    own["attachment"].municipio_ticket_id = None
    own["comment"].archivo_adjunto_id = own["attachment"].id
    db.session.commit()
    inventory = _build(own)
    checked = verify_case_inventory(inventory)
    assert checked["case_graph_closed"] is False
    assert checked["unresolved_attachment_comments"] == [f"TicketComentario:{own['comment'].id}"]
    assert any(issue["code"] == "comment_attachment_relation_unresolved" for issue in inventory["issues"])


def test_municipal_reply_cannot_borrow_a_foreign_case_comment(graph_context):
    own, foreign = graph_context
    own["municipal_reply"].comment_id = foreign["comment"].id
    db.session.commit()
    inventory = _build(own)
    assert not any(row["key"] == f"MunicipioTicketReplyEvent:{own['municipal_reply'].id}" for row in inventory["records"])
    assert any(issue["code"] == "municipal_reply_comment_unresolved" for issue in inventory["issues"])
    assert verify_case_inventory(inventory)["case_graph_closed"] is False


def test_credential_or_tracking_fragments_under_neutral_keys_are_not_bundled(graph_context):
    own, _ = graph_context
    audit = AuditEvent.query.filter_by(tenant_id=own["tenant"].id).first()
    audit.details = {"link": "https://example.invalid/#access_token=SYNTHETIC-FRAGMENT-MARKER",
                     "reference": "https://example.invalid/tracking#pin=SYNTHETIC-PIN-MARKER"}
    db.session.commit()
    inventory = _build(own)
    raw = json.dumps(inventory)
    assert "SYNTHETIC-FRAGMENT-MARKER" not in raw and "SYNTHETIC-PIN-MARKER" not in raw
    assert verify_case_inventory(inventory)["case_graph_closed"] is False


def test_malformed_persisted_private_reference_becomes_incomplete_not_exception(graph_context):
    own, _ = graph_context
    own["attachment"].url = "r2-private://["
    db.session.commit()
    reads = []
    def forbidden(identifier, tenant_id):
        reads.append((identifier, tenant_id))
        return own["bytes"]
    inventory = build_case_inventory(own["owner"], own["tenant"], object_reader=forbidden)
    assert reads == []
    assert any(issue["code"] == "attachment_private_binding_unsupported" for issue in inventory["issues"])
    assert verify_case_inventory(inventory)["case_graph_closed"] is False


def test_inventory_preserves_pending_caller_user_preferences_and_tenant_name_or_denies(graph_context):
    own, _ = graph_context
    reader = _reader(own)  # Load fixture identity before changing caller-owned state.
    owner, tenant = own["owner"], own["tenant"]
    _ = (owner.name, owner.acepta_marketing, tenant.nombre)
    owner.name = "Synthetic pending caller name"
    owner.acepta_marketing = True
    tenant.nombre = "Synthetic pending tenant name"
    expected = (owner.name, owner.acepta_marketing, tenant.nombre)
    assert owner in db.session.dirty and tenant in db.session.dirty
    try:
        build_case_inventory(owner, tenant, object_reader=reader)
    except PortabilityError:
        # An explicit value-free denial before export is a valid safe outcome.
        pass
    actual = (owner.name, owner.acepta_marketing, tenant.nombre)
    assert actual == expected, {"pending": expected, "after_inventory": actual}
    assert owner in db.session.dirty and tenant in db.session.dirty


def test_pending_scoped_role_name_never_becomes_export_authority(graph_context):
    own, _ = graph_context
    employee = User(name="Synthetic scoped operator", email="portability-scoped-role@example.invalid",
                    rol="empleado", tipo_chat="municipio", tenant_id=own["tenant"].id,
                    tenant_slug=own["tenant"].slug, municipio_id=own["owner"].id, es_empleado=True)
    employee.set_password("synthetic-portability-test-only")
    role = Role.query.filter_by(name="empleado").first()
    if role is None:
        role = Role(name="empleado", description="Synthetic persisted operator role")
        db.session.add(role)
    db.session.add(employee); db.session.flush()
    assignment = UserRole(user_id=employee.id, role_id=role.id, tenant_id=own["tenant"].id)
    db.session.add(assignment); db.session.commit()
    trusted_reader = _reader(own)
    assert assignment.role is role and role.name == "empleado"
    # Persisted operator authority is denied before introducing the dirty name.
    with pytest.raises(PortabilityError, match="portability_admin_required"):
        build_case_inventory(employee, own["tenant"], object_reader=trusted_reader)
    role.name = "tenant_admin"
    assert role in db.session.dirty and employee not in db.session.dirty and own["tenant"] not in db.session.dirty
    reads = []
    def observe(identifier, tenant_id):
        reads.append((identifier, tenant_id))
        return trusted_reader(identifier, tenant_id)
    denied = False
    try:
        build_case_inventory(employee, own["tenant"], object_reader=observe)
    except PortabilityError:
        denied = True
    assert denied, {"unexpected_object_reads": len(reads), "pending_role": role.name}
    assert role.name == "tenant_admin" and role in db.session.dirty
    assert reads == []


def test_explicit_pyme_tenant_cannot_export_municipal_domain_records(graph_context):
    own, _ = graph_context
    owner_id = own["owner"].id
    # Valid PYME owner/actor columns; retain a historical municipal-domain row
    # with the same explicit tenant and numeric owner. No CHECK is bypassed.
    with db.session.no_autoflush:
        own["tenant"].tipo = "pyme"
        own["tenant"].municipio_id = None
        own["tenant"].pyme_id = owner_id
        own["owner"].tipo_chat = "pyme"
        own["owner"].municipio_id = None
        own["owner"].pyme_id = owner_id
    db.session.commit()
    assert own["legacy"].tenant_id == own["tenant"].id and own["legacy"].municipio_id == owner_id
    # Existing policy is the oracle: a matching number/tenant ID cannot make
    # a business organization own a municipal-domain record.
    assert MunicipioTicket.query.filter(municipio_ticket_scope_filter(own["tenant"]),
                                        MunicipioTicket.id == own["legacy"].id).count() == 0
    reader = _reader(own)
    calls = []
    def observe(identifier, tenant_id):
        calls.append((identifier, tenant_id))
        return reader(identifier, tenant_id)
    inventory = build_case_inventory(own["owner"], own["tenant"], object_reader=observe)
    forbidden_models = {"MunicipioTicket", "MunicipioTicketReplyEvent", "ArchivoAdjunto", "TicketComentario"}
    leaked = [row["model"] for row in inventory["records"] if row["model"] in forbidden_models]
    assert leaked == [], {"unexpected_models": leaked}
    assert calls == []
    assert "Synthetic legacy case" not in json.dumps(inventory)
    assert own["attachment"].nombre_original not in json.dumps(inventory)
    assert any(issue["code"] == "municipal_domain_quarantined" for issue in inventory["issues"])
    assert verify_case_inventory(inventory)["case_graph_closed"] is False


def test_transient_clone_of_admin_id_is_not_an_authenticated_orm_identity(graph_context):
    own, _ = graph_context
    employee = User(name="Synthetic persisted operator", email="portability-transient-control@example.invalid",
                    rol="empleado", tipo_chat="municipio", tenant_id=own["tenant"].id,
                    tenant_slug=own["tenant"].slug, municipio_id=own["owner"].id, es_empleado=True)
    employee.set_password("synthetic-portability-test-only")
    db.session.add(employee); db.session.commit()
    reader = _reader(own)
    assert inspect(employee).persistent
    with pytest.raises(PortabilityError, match="portability_admin_required"):
        build_case_inventory(employee, own["tenant"], object_reader=reader)
    clone = User(id=own["owner"].id)
    assert inspect(clone).transient and inspect(clone).session is None and clone not in db.session
    reads = []
    def observe(identifier, tenant_id):
        reads.append((identifier, tenant_id))
        return reader(identifier, tenant_id)
    denied = False
    try:
        build_case_inventory(clone, own["tenant"], object_reader=observe)
    except PortabilityError:
        denied = True
    assert denied, {"unexpected_object_reads": len(reads), "clone_is_transient": inspect(clone).transient}
    assert reads == []


@pytest.mark.parametrize("coordinate", [float("inf"), float("-inf")], ids=["positive-infinity", "negative-infinity"])
def test_nonfinite_persisted_coordinate_degrades_without_losing_tenant_records(graph_context, coordinate):
    own, foreign = graph_context
    baseline = _build(own)
    own["legacy"].latitud = coordinate
    own["legacy"].longitud = -68.3
    db.session.commit()
    db.session.expire(own["legacy"], ["latitud", "longitud"])
    assert own["legacy"].latitud == coordinate  # SQLite really persisted this Float value.
    inventory = _build(own)
    encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True, allow_nan=False)
    inventory = json.loads(encoded)
    key = f"MunicipioTicket:{own['legacy'].id}"
    record = next(row for row in inventory["records"] if row["key"] == key)
    assert record["data"]["latitud"] is None and record["data"]["longitud"] == -68.3
    assert inventory["issues"] == [{"code": "non_finite_number_omitted", "key": key}]
    assert {row["key"] for row in inventory["records"]} == {row["key"] for row in baseline["records"]}
    assert [len(inventory[name]) for name in ("records", "relations", "objects")] == [
        len(baseline[name]) for name in ("records", "relations", "objects")]
    assert all(row["tenant_id"] == own["tenant"].id for row in inventory["records"])
    assert f"MunicipioTicket:{foreign['legacy'].id}" not in {row["key"] for row in inventory["records"]}
    checked = verify_case_inventory(inventory)
    assert checked["integrity_valid"] is True and checked["case_graph_closed"] is False
    assert checked["aif_complete"] is False and checked["restoration_verified"] is False
    assert set(checked["incomplete_domains"]) == set(DOMAINS)
    db.session.expire(own["legacy"], ["latitud"])
    assert own["legacy"].latitud == coordinate  # Sanitization never rewrites the caller's source.


def test_nonfinite_nested_pending_metadata_is_omitted_without_flushing(graph_context):
    own, _ = graph_context
    baseline = _build(own)
    artifact = own["artifact"]
    stored_payload = deepcopy(artifact.payload_json)
    # These are pending ORM values, not a claim that PostgreSQL JSON accepts NaN.
    artifact.payload_json = {"values": [float("nan"), {"positive": float("inf"), "negative": float("-inf")}],
                             "finite": [0.0, -54.8, 7, True, None, "finite text"]}
    writes = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)
    event.listen(db.engine, "before_cursor_execute", observe)
    try:
        inventory = _build(own)
    finally:
        event.remove(db.engine, "before_cursor_execute", observe)
    key = f"InboxTicketArtifact:{artifact.id}"
    record = next(row for row in inventory["records"] if row["key"] == key)
    assert record["data"]["payload_json"] == {"values": [None, {"positive": None, "negative": None}],
        "finite": [0.0, -54.8, 7, True, None, "finite text"]}
    assert inventory["issues"] == [{"code": "non_finite_number_omitted", "key": key}]
    assert len(inventory["records"]) == len(baseline["records"])
    assert artifact in db.session.dirty and writes == []
    assert artifact.payload_json["values"][0] != artifact.payload_json["values"][0]
    with db.session.no_autoflush:
        persisted = db.session.query(InboxTicketArtifact.payload_json).filter_by(id=artifact.id).scalar()
    assert persisted == stored_payload
    checked = verify_case_inventory(json.loads(json.dumps(inventory, allow_nan=False)))
    assert checked["integrity_valid"] is True and checked["case_graph_closed"] is False
    assert checked["aif_complete"] is False


def test_sqlite_nan_coordinate_round_trips_as_database_null(graph_context):
    own, _ = graph_context
    assert str(db.engine.url) == "sqlite:///:memory:"
    own["legacy"].latitud = float("nan")
    db.session.commit()
    db.session.expire(own["legacy"], ["latitud"])
    # SQLite's adapter converts NaN to SQL NULL; this does not certify PG NaN.
    assert own["legacy"].latitud is None
    inventory = _build(own)
    record = next(row for row in inventory["records"] if row["key"] == f"MunicipioTicket:{own['legacy'].id}")
    assert record["data"]["latitud"] is None and inventory["issues"] == []
    checked = verify_case_inventory(json.loads(json.dumps(inventory, allow_nan=False)))
    assert checked["integrity_valid"] is True and checked["case_graph_closed"] is True
    assert checked["aif_complete"] is False


@pytest.mark.parametrize("coordinate", [0.0, -54.8, None], ids=["zero", "finite-negative", "missing"])
def test_finite_or_missing_coordinate_preserves_value_and_graph_state(graph_context, coordinate):
    own, _ = graph_context
    own["legacy"].latitud = coordinate
    db.session.commit()
    db.session.expire(own["legacy"], ["latitud"])
    inventory = _build(own)
    record = next(row for row in inventory["records"] if row["key"] == f"MunicipioTicket:{own['legacy'].id}")
    assert record["data"]["latitud"] == coordinate and inventory["issues"] == []
    checked = verify_case_inventory(json.loads(json.dumps(inventory, allow_nan=False)))
    assert checked["integrity_valid"] is True and checked["case_graph_closed"] is True
    assert checked["aif_complete"] is False and checked["restoration_verified"] is False


def test_foreign_nonfinite_coordinate_does_not_pollute_own_inventory(graph_context):
    own, foreign = graph_context
    baseline = _build(own)
    foreign["legacy"].latitud = float("inf")
    db.session.commit()
    db.session.expire(foreign["legacy"], ["latitud"])
    assert foreign["legacy"].latitud == float("inf")
    inventory = _build(own)
    assert inventory == baseline and inventory["issues"] == []
    checked = verify_case_inventory(json.loads(json.dumps(inventory, allow_nan=False)))
    assert checked["integrity_valid"] is True and checked["case_graph_closed"] is True


@pytest.mark.parametrize("coordinate", [float("inf"), float("-inf"), float("nan")],
                         ids=["positive-infinity", "negative-infinity", "nan"])
def test_verifier_rejects_unencoded_nonfinite_number(graph_context, coordinate):
    own, _ = graph_context
    inventory = _build(own)
    record = next(row for row in inventory["records"] if row["key"] == f"MunicipioTicket:{own['legacy'].id}")
    record["data"]["latitud"] = coordinate
    with pytest.raises(PortabilityError, match="portability_bundle_invalid"):
        verify_case_inventory(inventory)
