"""HTTP regressions for tenant-scoped canonical and legacy CRM contacts.

Real create_ticket producer, token, Flask handlers and ORM. Only synthetic
actors/data in SQLite memory and owned temporary static are permitted.
"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from models import MunicipioTicket, PymeTicket, TenantProfile, User, db
from services.auth_session_lifecycle import issue_token
from services.v2.ticket_service import create_ticket


def _headers(actor, tenant):
    token = issue_token({"user_id": actor.id, "rol": actor.rol, "tipo_chat": actor.tipo_chat,
                         "exp": datetime.now(timezone.utc) + timedelta(minutes=5)})
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return {"Authorization": "Bearer " + token, "X-Tenant-Slug": tenant.slug}


@pytest.fixture
def contacts_context(client, monkeypatch, tmp_path):
    assert str(db.engine.url) == "sqlite:///:memory:"
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(client.application, "static_folder", str(static))
    records = []
    for suffix in ("a", "b"):
        owner = User(name="Synthetic owner " + suffix, email="canonical-owner-" + suffix + "@example.invalid",
                     rol="admin", tipo_chat="municipio")
        owner.set_password("synthetic-contact-regression-only")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="canonical-contact-" + suffix, nombre="Synthetic contacts " + suffix,
                               tipo="municipio", municipio_id=owner.id, is_active=True, plan="enterprise")
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        employee = User(name="Synthetic category operator " + suffix,
                        email="canonical-operator-" + suffix + "@example.invalid", rol="empleado",
                        tipo_chat="municipio", es_empleado=True, tenant_id=tenant.id,
                        tenant_slug=tenant.slug, municipio_id=owner.id)
        employee.set_password("synthetic-contact-regression-only")
        employee.accesibilidad = {"employee_scope": {"categorias": ["arbolado"], "permisos": ["tickets.read"]}}
        employee.categorias_lista = ["arbolado"]
        db.session.add(employee)
        records.append({"owner": owner, "employee": employee, "tenant": tenant, "static": static})
    db.session.commit()
    return records


def _produce(record, contact, *, category="arbolado", channel="whatsapp"):
    # Actual producer accepts this contact dictionary and stores it in
    # datos_extra.contact. actor=None represents a visitor without a User;
    # no object is hand-built or copied from a mock API response.
    legacy_before = (MunicipioTicket.query.count(), PymeTicket.query.count())
    case = create_ticket(tenant=record["tenant"], actor_user=None, payload={
        "title": "Synthetic canonical contact case", "description": "Synthetic visitor inquiry",
        "category": category, "channel": channel, "contact": contact,
    })
    db.session.commit()
    assert case.user_id is None
    assert (MunicipioTicket.query.count(), PymeTicket.query.count()) == legacy_before
    return case


def _observed(client, record, actor=None):
    actor = actor or record["owner"]
    headers = _headers(actor, record["tenant"])
    summary = client.get("/api/v2/backoffice/contacts/summary",
                         query_string={"tenant_slug": record["tenant"].slug}, headers=headers)
    export = client.post("/api/v2/backoffice/export", json={
        "tenant_slug": record["tenant"].slug, "tenant_id": record["tenant"].id,
        "resource": "contacts", "format": "csv", "delivery": "inline",
    }, headers=headers)
    assert summary.status_code == 200 and export.status_code == 200
    assert export.mimetype == "text/csv" and export.headers["X-Export-Contract"] == "backoffice.export.v2"
    assert export.headers["X-Tenant-Id"] == str(record["tenant"].id)
    assert export.headers["X-Tenant-Slug"] == record["tenant"].slug
    rows = list(csv.DictReader(io.StringIO(export.data.decode("utf-8-sig"))))
    assert int(export.headers["X-Export-Row-Count"]) == len(rows)
    assert int(export.headers["X-Export-Total-Rows"]) == len(rows)
    assert export.headers["X-Export-Complete"] == "true"
    assert summary.get_json()["summary"]["total"] == len(rows)
    assert not (record["static"] / "exports").exists()
    return summary.get_json(), rows, export.data


def test_canonical_visitor_without_user_is_present_in_summary_and_private_csv(client, contacts_context):
    own, _ = contacts_context
    email = "canonical-visitor@example.invalid"
    contact = {"name": "Synthetic visitor", "email": email, "phone": "+5491100000011",
               "accepts_marketing": True, "private_fixture_note": "UNTRUSTED-EXTRA-NOT-A-CONTACT-FIELD"}
    case = _produce(own, contact)
    assert db.session.get(type(case), case.id).datos_extra["contact"]["email"] == email
    assert User.query.filter_by(email=email).count() == 0
    summary, rows, data = _observed(client, own)
    visitor = [row for row in rows if row["email"] == email]
    assert len(visitor) == 1, {"summary": summary["summary"], "exported_emails": [row["email"] for row in rows]}
    assert visitor[0]["name"] == contact["name"]
    # CSV safety intentionally prefixes a leading '+' string. Verify the safe
    # representation rather than requiring the raw value in a spreadsheet.
    assert visitor[0]["phone"] == "'" + contact["phone"]
    assert summary["summary"]["total"] == 3 and summary["summary"]["with_email"] == 3
    assert summary["summary"]["with_phone"] == 1 and summary["summary"]["opt_in_marketing"] == 0
    assert visitor[0]["accepts_marketing"] == "False"
    assert b"UNTRUSTED-EXTRA-NOT-A-CONTACT-FIELD" not in data


def test_foreign_tenant_and_disallowed_category_contacts_never_leak_to_employee(client, contacts_context):
    own, foreign = contacts_context
    allowed_email = "allowed-canonical@example.invalid"
    denied_email = "denied-category@example.invalid"
    foreign_email = "foreign-canonical@example.invalid"
    _produce(own, {"name": "Synthetic allowed", "email": allowed_email})
    _produce(own, {"name": "Synthetic denied", "email": denied_email}, category="bacheo")
    _produce(foreign, {"name": "Synthetic foreign", "email": foreign_email})
    summary, rows, data = _observed(client, own, own["employee"])
    exposed = json.dumps(summary).encode() + data
    assert denied_email.encode() not in exposed and foreign_email.encode() not in exposed
    assert {foreign[key].email for key in ("owner", "employee")}.isdisjoint({row["email"] for row in rows})
    assert allowed_email in {row["email"] for row in rows}
    assert summary["summary"]["total"] == 3 and len(rows) == 3
    owner_summary, owner_rows, _ = _observed(client, own)
    assert {allowed_email, denied_email}.issubset({row["email"] for row in owner_rows})
    assert owner_summary["summary"]["total"] == 4


def test_explicit_normalized_email_deduplicates_legitimate_cross_model_identity(client, contacts_context):
    own, _ = contacts_context
    canonical = _produce(own, {"name": "Synthetic known visitor", "email": "same@example.invalid",
                               "phone": "+5491100000022"})
    assert db.session.get(MunicipioTicket, canonical.id) is None
    legacy = MunicipioTicket(id=canonical.id, tenant_id=own["tenant"].id, municipio_id=own["owner"].id,
                              pregunta="Synthetic legacy contact", categoria="arbolado", estado="nuevo",
                              nombre_vecino="Synthetic known visitor", email_vecino=" SAME@example.invalid ",
                              telefono_vecino="+54 9 11 0000 0022", canal_ingreso="web")
    db.session.add(legacy)
    db.session.commit()
    assert legacy.id == canonical.id  # Explicit collision with the observed canonical ID.
    summary, rows, _ = _observed(client, own)
    same = [row for row in rows if row["email"].strip().lower() == "same@example.invalid"]
    assert len(same) == 1 and int(same[0]["records"]) == 2
    assert len(set(json.loads(same[0]["sources"]))) == 2
    assert {"web", "whatsapp"}.issubset(set(json.loads(same[0]["channels"])))
    assert summary["summary"]["total"] == 3 and summary["summary"]["possible_duplicates"] == 1


def test_matching_name_and_numeric_id_alone_never_merge_two_contacts(client, contacts_context):
    own, _ = contacts_context
    same_name = "Synthetic namesake"
    canonical = _produce(own, {"name": same_name})
    assert db.session.get(MunicipioTicket, canonical.id) is None
    legacy = MunicipioTicket(id=canonical.id, tenant_id=own["tenant"].id, municipio_id=own["owner"].id,
                              pregunta="Synthetic unidentified legacy", categoria="arbolado", estado="nuevo",
                              nombre_vecino=same_name, canal_ingreso="web")
    db.session.add(legacy)
    db.session.commit()
    assert legacy.id == canonical.id
    summary, rows, _ = _observed(client, own)
    namesakes = [row for row in rows if row["name"] == same_name]
    assert len(namesakes) == 2
    assert len({row["key"] for row in namesakes}) == 2
    assert all(int(row["records"]) == 1 for row in namesakes)
    assert summary["summary"]["total"] == 4 and summary["summary"]["missing_minimum_data"] == 2


def test_two_legacy_namesakes_without_contact_identity_remain_separate(client, contacts_context):
    own, _ = contacts_context
    for index in range(2):
        db.session.add(MunicipioTicket(
            tenant_id=own["tenant"].id, municipio_id=own["owner"].id,
            pregunta="Synthetic legacy namesake " + str(index), categoria="arbolado",
            estado="nuevo", nombre_vecino="Synthetic legacy namesake", canal_ingreso="web",
        ))
    db.session.commit()
    summary, rows, _ = _observed(client, own)
    namesakes = [row for row in rows if row["name"] == "Synthetic legacy namesake"]
    assert len(namesakes) == 2 and len({row["key"] for row in namesakes}) == 2
    assert all(int(row["records"]) == 1 for row in namesakes)
    assert summary["summary"]["missing_minimum_data"] == 2
    assert summary["summary"]["possible_duplicates"] == 0


def test_malformed_persisted_contact_shapes_do_not_become_strings_or_consent(client, contacts_context):
    own, _ = contacts_context
    marker = "MALFORMED-CONTACT-PRIVATE-MARKER"
    # The producer sanitizes a non-dict payload to {}. Persisted legacy JSON
    # can still have bad shapes; set these explicitly after the real producer.
    malformed = [
        [marker],
        marker,
        {"name": {"raw": marker}, "email": [marker], "phone": True,
         "nombre": 42, "correo": False, "telefono": {"raw": marker},
         "accepts_marketing": True, "dni": marker},
    ]
    for contact in malformed:
        case = _produce(own, {})
        case.datos_extra = {**case.datos_extra, "contact": contact, "dni": marker}
    db.session.commit()
    summary, rows, data = _observed(client, own)
    anonymous = [row for row in rows if json.loads(row["sources"]) == ["tenant_ticket"]]
    assert len(anonymous) == 3 and len({row["key"] for row in anonymous}) == 3
    assert all(row["name"] == row["email"] == row["phone"] == "" for row in anonymous)
    assert all(row["accepts_marketing"] == "False" and int(row["records"]) == 1 for row in anonymous)
    assert summary["summary"]["total"] == 5
    assert summary["summary"]["missing_minimum_data"] == 3
    assert summary["summary"]["opt_in_marketing"] == 0
    assert marker.encode() not in data + json.dumps(summary).encode()


def test_empty_profile_never_borrows_the_case_creators_contact(client, contacts_context):
    own, _ = contacts_context
    case = create_ticket(tenant=own["tenant"], actor_user=own["owner"], payload={
        "title": "Synthetic operator-created empty profile", "description": "No visitor contact supplied",
        "category": "arbolado", "channel": "manual", "contact": {},
    })
    db.session.commit()
    assert case.user_id == own["owner"].id
    summary, rows, _ = _observed(client, own)
    empty = [row for row in rows if json.loads(row["sources"]) == ["tenant_ticket"]]
    assert len(empty) == 1 and empty[0]["name"] == empty[0]["email"] == empty[0]["phone"] == ""
    assert json.loads(empty[0]["channels"]) == ["manual"]
    owner = [row for row in rows if row["email"] == own["owner"].email]
    assert len(owner) == 1 and int(owner[0]["records"]) == 1
    assert summary["summary"]["total"] == 3 and summary["summary"]["missing_minimum_data"] == 1


def test_canonical_alias_email_dedup_and_user_marketing_consent_remain_distinct(client, contacts_context):
    own, _ = contacts_context
    own["owner"].acepta_marketing = True
    _produce(own, {"nombre": "Synthetic alias visitor", "correo": " ALIAS@example.invalid ",
                   "telefono": "+5491100000033", "accepts_marketing": True})
    _produce(own, {"contact_name": "Same declared contact", "contact_email": "alias@example.invalid",
                   "contact_phone": "+54 9 11 0000 0033"}, channel="widget")
    _produce(own, {"email": own["owner"].email, "accepts_marketing": False})
    summary, rows, _ = _observed(client, own)
    alias = [row for row in rows if row["email"] == "alias@example.invalid"]
    assert len(alias) == 1 and int(alias[0]["records"]) == 2
    assert alias[0]["name"] == "Synthetic alias visitor"
    assert json.loads(alias[0]["channels"]) == ["whatsapp", "widget"]
    assert alias[0]["accepts_marketing"] == "False"
    owner = [row for row in rows if row["email"] == own["owner"].email]
    assert len(owner) == 1 and owner[0]["accepts_marketing"] == "True" and int(owner[0]["records"]) == 2
    assert summary["summary"]["total"] == 3 and summary["summary"]["opt_in_marketing"] == 1


@pytest.mark.parametrize("kind", ["pyme", "colegio"])
def test_canonical_contacts_are_not_limited_to_municipal_organizations(client, contacts_context, kind):
    own, _ = contacts_context
    owner_id = own["owner"].id  # Load the expired ID before changing owner columns.
    # School profiles use the existing business owner association and pyme
    # actor type (as in the real backoffice school fixture). Both CHECKs stay
    # enforced; the final owner-column pair is assigned before any flush.
    with db.session.no_autoflush:
        own["tenant"].tipo = kind
        own["tenant"].municipio_id = None
        own["tenant"].pyme_id = owner_id
        for field in ("owner", "employee"):
            own[field].tipo_chat = "pyme"
            own[field].municipio_id = None
            own[field].pyme_id = owner_id
    db.session.commit()
    _produce(own, {"name": "Synthetic " + kind + " visitor", "email": kind + "-visitor@example.invalid"})
    summary, rows, _ = _observed(client, own)
    assert kind + "-visitor@example.invalid" in {row["email"] for row in rows}
    assert summary["summary"]["total"] == 3


def test_canonical_contact_batch_does_not_lazy_load_creators_per_case(client, contacts_context):
    own, _ = contacts_context
    creators = [
        User(name="Synthetic creator " + str(index), email="creator-" + str(index) + "@example.invalid",
             rol="usuario", tipo_chat="municipio", tenant_id=own["tenant"].id,
             tenant_slug=own["tenant"].slug, municipio_id=own["owner"].id)
        for index in range(13)
    ]
    for creator in creators:
        creator.set_password("synthetic-contact-regression-only")
    db.session.add_all(creators)
    db.session.commit()

    def produce_linked(index):
        create_ticket(tenant=own["tenant"], actor_user=creators[index], payload={
            "title": "Synthetic linked case " + str(index), "description": "No borrowed creator contact",
            "category": "arbolado", "channel": "web", "contact": {},
        })
        db.session.commit()

    def observe_queries():
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().lower().startswith("select"):
                statements.append(statement)

        # Expire ORM state so the test cannot conceal a per-case relationship
        # load behind creator objects already in the identity map.
        db.session.expire_all()
        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            summary, rows, _ = _observed(client, own)
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)
        return len(statements), summary, rows

    produce_linked(0)
    small_count, _, _ = observe_queries()
    for index in range(1, 13):
        produce_linked(index)
    large_count, summary, rows = observe_queries()
    anonymous = [row for row in rows if json.loads(row["sources"]) == ["tenant_ticket"]]
    assert len(anonymous) == 13 and summary["summary"]["missing_minimum_data"] == 13
    assert summary["summary"]["total"] == 28  # 15 actual User contacts + 13 independent empty snapshots.
    assert large_count <= small_count + 2, {"small_selects": small_count, "large_selects": large_count}
