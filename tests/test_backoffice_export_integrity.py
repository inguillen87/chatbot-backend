"""Private complete operational exports; real HTTP/ORM with synthetic SQLite.

No provider, real personal data, full AIF portability or backup/restore claim.
"""
import csv
import io
import re
from datetime import datetime, timedelta, timezone

import pytest
from models import MunicipioTicket, PymePedido, PymeTicket, TenantProfile, TenantTicket, User, db
from services.auth_session_lifecycle import issue_token
from werkzeug.http import parse_options_header


CONTRACT = "backoffice.export.v2"


def _headers(actor, tenant, request_id="export-red-case"):
    token = issue_token({
        "user_id": actor.id, "rol": actor.rol, "tipo_chat": actor.tipo_chat,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    })
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return {"Authorization": "Bearer " + token,
            "X-Tenant-Slug": tenant.slug, "X-Request-Id": request_id}


@pytest.fixture
def export_context(client, monkeypatch, tmp_path):
    assert str(db.engine.url) == "sqlite:///:memory:"
    owned_static = tmp_path / "static"
    owned_static.mkdir()
    monkeypatch.setattr(client.application, "static_folder", str(owned_static))
    records = []
    for suffix in ("a", "b"):
        owner = User(name="Synthetic export owner " + suffix,
                     email="export-owner-" + suffix + "@example.invalid",
                     rol="admin", tipo_chat="municipio")
        owner.set_password("synthetic-local-export-only")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="synthetic-export-" + suffix,
                               nombre="Synthetic export " + suffix,
                               tipo="municipio", municipio_id=owner.id,
                               is_active=True, plan="enterprise")
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        cases = []
        for index in range(31 if suffix == "a" else 3):
            state = {28: "resuelto", 29: "cerrado", 30: "finalizado"}.get(index, "nuevo")
            case = MunicipioTicket(
                tenant_id=tenant.id, municipio_id=owner.id,
                pregunta="SYNTHETIC-CASE-" + suffix.upper() + "-" + str(index).zfill(2),
                nro_ticket="EXPORT-" + suffix.upper() + "-" + str(index).zfill(2),
                categoria="arbolado" if index % 2 == 0 else "bacheo",
                estado=state, fecha=datetime(2026, 1, 1) + timedelta(minutes=index),
                canal_ingreso="web", detalles="Synthetic fixture; no personal data",
            )
            db.session.add(case)
            cases.append(case)
        employee = User(name="Synthetic category operator " + suffix,
                        email="export-operator-" + suffix + "@example.invalid",
                        rol="empleado", tipo_chat="municipio", es_empleado=True,
                        tenant_id=tenant.id, tenant_slug=tenant.slug, municipio_id=owner.id)
        employee.set_password("synthetic-local-export-only")
        employee.accesibilidad = {"employee_scope": {
            "categorias": ["arbolado"], "permisos": ["tickets.read"],
        }}
        employee.categorias_lista = ["arbolado"]
        citizen = User(name="Synthetic end user " + suffix,
                       email="export-end-user-" + suffix + "@example.invalid",
                       rol="usuario", tipo_chat="municipio", tenant_id=tenant.id,
                       tenant_slug=tenant.slug, municipio_id=owner.id)
        citizen.set_password("synthetic-local-export-only")
        db.session.add_all([employee, citizen])
        records.append({"owner": owner, "tenant": tenant, "cases": cases,
                        "employee": employee, "citizen": citizen})
    db.session.commit()
    return {"records": records, "static": owned_static,
            "export_dir": owned_static / "exports" / "backoffice"}


def _post(client, record, fmt="csv", *, filters=None, actor=None,
          inline=True, request_id="export-red-case"):
    body = {"tenant_slug": record["tenant"].slug, "tenant_id": record["tenant"].id, "resource": "tickets",
            "format": fmt, "filters": {} if filters is None else filters}
    if inline:
        body["delivery"] = "inline"
    return client.post("/api/v2/backoffice/export", json=body,
                       headers=_headers(actor or record["owner"], record["tenant"], request_id))


def _artifact_bytes(response, context):
    assert response.status_code == 200
    assert not response.is_json, response.get_json(silent=True)
    return response.data


def _rows(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _pdf_text(data):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(data)
    try:
        texts = []
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            try:
                texts.append(textpage.get_text_range())
            finally:
                textpage.close()
                page.close()
        return "\n".join(texts)
    finally:
        document.close()


def _assert_inline(response, record, fmt, expected_count, resource="tickets"):
    assert not response.is_json, "export must be returned through its authenticated response"
    assert response.headers["X-Export-Contract"] == CONTRACT
    assert response.headers["X-Tenant-Slug"] == record["tenant"].slug
    assert response.headers["X-Tenant-Id"] == str(record["tenant"].id)
    assert response.headers["X-Export-Resource"] == resource
    assert response.headers["X-Export-Format"] == fmt
    assert response.headers["X-Export-Total-Rows"] == str(expected_count)
    assert response.headers["X-Export-Row-Count"] == str(expected_count)
    assert response.headers["X-Export-Complete"] == "true"
    assert {"private", "no-store"}.issubset({part.strip().lower() for part in response.headers["Cache-Control"].split(",")})
    disposition, options = parse_options_header(response.headers["Content-Disposition"])
    assert disposition.lower() == "attachment"
    filename = options.get("filename", "")
    assert re.fullmatch(r"[A-Za-z0-9_.-]+\." + fmt, filename)
    assert "Expires" not in response.headers and "download_url" not in response.headers
    assert response.mimetype == {
        "csv": "text/csv", "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[fmt]
    exposed = {field.strip() for field in response.headers["Access-Control-Expose-Headers"].split(",")}
    assert exposed == {"Content-Disposition", "X-Export-Contract", "X-Tenant-Slug", "X-Tenant-Id",
                       "X-Export-Resource", "X-Export-Format", "X-Export-Total-Rows",
                       "X-Export-Row-Count", "X-Export-Complete"}


def test_csv_exports_all_authorized_cases_including_closed_and_resolved(client, export_context):
    own, foreign = export_context["records"]
    response = _post(client, own, request_id="export-red-complete-csv")
    rows = _rows(_artifact_bytes(response, export_context))
    assert {int(row["id"]) for row in rows} == {case.id for case in own["cases"]}
    assert len(rows) == 31
    assert {"resuelto", "cerrado", "finalizado"}.issubset({row["status"] for row in rows})
    assert not {case.id for case in foreign["cases"]}.intersection({int(row["id"]) for row in rows})
    _assert_inline(response, own, "csv", 31)
    assert not export_context["export_dir"].exists()


def test_filters_are_applied_and_unknown_or_cross_scope_filters_are_rejected(client, export_context):
    own = export_context["records"][0]
    response = _post(client, own, filters={"scope": "municipio", "statuses": ["resuelto"],
                                          "categories": ["arbolado"]},
                     request_id="export-red-filtered")
    rows = _rows(_artifact_bytes(response, export_context))
    expected = {case.id for case in own["cases"] if case.estado == "resuelto" and case.categoria == "arbolado"}
    assert expected and {int(row["id"]) for row in rows} == expected
    _assert_inline(response, own, "csv", len(expected))
    for invalid in ({"unknown_filter": True}, {"scope": "pyme"}, {"statuses": "resuelto"},
                    {"categories": [None]}, {"scope": "municipio", "statuses": []}):
        result = _post(client, own, filters=invalid, request_id="export-red-invalid-filter")
        assert result.status_code == 400
        assert result.is_json and result.get_json()["reason_code"] == "invalid_export_filters"
    assert not export_context["export_dir"].exists()


def test_pdf_contains_all_authorized_case_labels_across_pages(client, export_context):
    own, foreign = export_context["records"]
    response = _post(client, own, "pdf", request_id="export-red-complete-pdf")
    data = _artifact_bytes(response, export_context)
    assert data.startswith(b"%PDF-")
    text = _pdf_text(data)
    assert all(case.pregunta in text for case in own["cases"])
    assert not any(case.pregunta in text for case in foreign["cases"])
    _assert_inline(response, own, "pdf", 31)
    assert not export_context["export_dir"].exists()


def test_xlsx_contains_every_authorized_case_without_preview_cap(client, export_context):
    from openpyxl import load_workbook
    own = export_context["records"][0]
    response = _post(client, own, "xlsx", request_id="export-red-complete-xlsx")
    data = _artifact_bytes(response, export_context)
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        raw_rows = list(workbook.active.iter_rows(values_only=True))
    finally:
        workbook.close()
    headers, values = raw_rows[0], raw_rows[1:]
    id_index, title_index = headers.index("id"), headers.index("title")
    assert len(values) == 31
    assert {int(row[id_index]) for row in values} == {case.id for case in own["cases"]}
    assert {row[title_index] for row in values} == {case.pregunta for case in own["cases"]}
    _assert_inline(response, own, "xlsx", 31)
    assert not export_context["export_dir"].exists()


def test_legacy_export_fails_closed_and_static_exports_are_private_without_breaking_public_assets(client, export_context):
    own, foreign = export_context["records"]
    legacy_dir = export_context["export_dir"]
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "synthetic-legacy.csv").write_text("id,title\n1,SYNTHETIC-LEGACY\n", encoding="utf-8")
    public_dir = export_context["static"] / "welcome"
    public_dir.mkdir()
    (public_dir / "synthetic-public.txt").write_text("SYNTHETIC-PUBLIC", encoding="utf-8")
    aliases = (
        "/static/exports/backoffice/synthetic-legacy.csv",
        "/static/exports/%62ackoffice/synthetic-legacy.csv",
        "/static/exports/backoffice/../backoffice/synthetic-legacy.csv",
        "/static/exports/backoffice/./synthetic-legacy.csv",
        "/static/exports/backoffice%2Fsynthetic-legacy.csv",
        "/static/exports%5Cbackoffice%5Csynthetic-legacy.csv",
        "/static/exports//backoffice/synthetic-legacy.csv",
        "/static/exports/%2562ackoffice/synthetic-legacy.csv",
    )
    observed = []
    for url in aliases:
        for actor_headers in ({}, _headers(foreign["owner"], foreign["tenant"]),
                              _headers(own["owner"], own["tenant"])):
            response = client.get(url, headers=actor_headers)
            observed.append((url, response.status_code, b"SYNTHETIC-LEGACY" in response.data))
    public = client.get("/static/welcome/synthetic-public.txt")
    assert public.status_code == 200 and public.data == b"SYNTHETIC-PUBLIC"
    before = sorted(path.name for path in legacy_dir.iterdir())
    response = _post(client, own, inline=False, request_id="export-red-legacy-disabled")
    after = sorted(path.name for path in legacy_dir.iterdir())
    assert all(status == 404 and not leaked for _, status, leaked in observed), observed
    assert response.status_code == 409
    assert response.get_json()["reason_code"] == "export_client_upgrade_required"
    assert "download_url" not in response.get_json() and "expires_at" not in response.get_json()
    assert before == after


def test_export_rechecks_tenant_role_and_anonymous_denial_without_data_or_files(client, export_context):
    own, foreign = export_context["records"]
    forged = client.post("/api/v2/backoffice/export", json={
        "tenant_slug": own["tenant"].slug, "tenant_id": own["tenant"].id,
        "resource": "tickets", "format": "csv", "delivery": "inline",
    }, headers=_headers(foreign["owner"], foreign["tenant"]))
    assert forged.status_code in {403, 404}
    denied = _post(client, own, actor=own["citizen"], request_id="export-red-end-user")
    assert denied.status_code == 403
    anonymous = client.post("/api/v2/backoffice/export", json={
        "tenant_slug": own["tenant"].slug, "tenant_id": own["tenant"].id,
        "resource": "tickets", "format": "csv", "delivery": "inline",
    })
    assert anonymous.status_code in {401, 403}
    for denied_response in (forged, denied, anonymous):
        assert "X-Export-Complete" not in denied_response.headers
        assert b"SYNTHETIC-CASE" not in denied_response.data
        assert "download_url" not in (denied_response.get_json(silent=True) or {})
    assert not export_context["export_dir"].exists()


def test_employee_export_preserves_category_scope_including_closed_cases(client, export_context):
    own = export_context["records"][0]
    response = _post(client, own, actor=own["employee"], request_id="export-red-category-scoped")
    rows = _rows(_artifact_bytes(response, export_context))
    expected = {case.id for case in own["cases"] if case.categoria == "arbolado"}
    assert {int(row["id"]) for row in rows} == expected
    assert all(row["area"] == "arbolado" for row in rows)
    _assert_inline(response, own, "csv", len(expected))
    assert not export_context["export_dir"].exists()


def _post_resource(client, record, resource, *, fmt="csv", filters=None):
    return client.post("/api/v2/backoffice/export", json={
        "tenant_slug": record["tenant"].slug, "tenant_id": record["tenant"].id,
        "resource": resource, "format": fmt, "delivery": "inline",
        "filters": {} if filters is None else filters,
    }, headers=_headers(record["owner"], record["tenant"]))


def test_published_tenant_id_is_required_before_selection_and_mismatch_is_denied(client, export_context, monkeypatch):
    own, foreign = export_context["records"]
    calls = []
    monkeypatch.setattr("routes.backoffice._export_rows", lambda *args: calls.append("query"))
    monkeypatch.setattr("routes.backoffice._export_bytes", lambda *args: calls.append("bytes"))
    for value in (None, "1", True, 0, -1, 1.0):
        response = client.post("/api/v2/backoffice/export", json={
            "tenant_slug": own["tenant"].slug, "tenant_id": value,
            "resource": "tickets", "format": "csv", "delivery": "inline",
        }, headers=_headers(own["owner"], own["tenant"]))
        assert response.status_code == 400
        assert response.get_json()["reason_code"] == "export_scope_required"
    response = client.post("/api/v2/backoffice/export", json={
        "tenant_slug": own["tenant"].slug, "resource": "tickets", "format": "csv", "delivery": "inline",
    }, headers=_headers(own["owner"], own["tenant"]))
    assert response.status_code == 400 and response.get_json()["reason_code"] == "export_scope_required"
    mismatched = client.post("/api/v2/backoffice/export", json={
        "tenant_slug": own["tenant"].slug, "tenant_id": foreign["tenant"].id,
        "resource": "tickets", "format": "csv", "delivery": "inline",
    }, headers=_headers(own["owner"], own["tenant"]))
    assert mismatched.status_code == 404
    assert mismatched.get_json()["reason_code"] == "export_scope_mismatch"
    assert calls == [] and not export_context["export_dir"].exists()


def test_orders_export_includes_finalized_rows_and_applies_status_filter(client, export_context):
    own, foreign = export_context["records"]
    orders = []
    for record, amount in ((own, 31), (foreign, 2)):
        for index in range(amount):
            order = PymePedido(pyme_id=record["owner"].id, tenant_id=record["tenant"].id,
                               asunto="Synthetic order", detalles="{}", monto_total=index,
                               nombre_cliente="Synthetic customer")
            order.nro_pedido = "SYNTHETIC-ORDER-" + str(record["tenant"].id) + "-" + str(index)
            order.estado = "finalizado" if index >= 28 else "pendiente"
            db.session.add(order)
            if record is own:
                orders.append(order)
    db.session.commit()
    response = _post_resource(client, own, "orders")
    rows = _rows(_artifact_bytes(response, export_context))
    assert {int(row["id"]) for row in rows} == {order.id for order in orders}
    assert len(rows) == 31 and "finalizado" in {row["status"] for row in rows}
    _assert_inline(response, own, "csv", 31, "orders")
    filtered = _post_resource(client, own, "orders", filters={"statuses": ["finalizado"]})
    assert {int(row["id"]) for row in _rows(_artifact_bytes(filtered, export_context))} == {
        order.id for order in orders if order.estado == "finalizado"}
    _assert_inline(filtered, own, "csv", 3, "orders")


def test_contacts_and_team_keep_existing_authorized_resource_contracts(client, export_context):
    own, foreign = export_context["records"]
    contacts = _post_resource(client, own, "contacts")
    rows = _rows(_artifact_bytes(contacts, export_context))
    emails = {row["email"] for row in rows}
    assert {own[key].email for key in ("owner", "employee", "citizen")}.issubset(emails)
    assert not {foreign[key].email for key in ("owner", "employee", "citizen")}.intersection(emails)
    assert len(rows) == 34
    _assert_inline(contacts, own, "csv", 34, "contacts")
    team = _post_resource(client, own, "team", fmt="xlsx")
    from openpyxl import load_workbook
    workbook = load_workbook(io.BytesIO(_artifact_bytes(team, export_context)), read_only=True)
    try:
        raw_rows = list(workbook.active.iter_rows(values_only=True))
    finally:
        workbook.close()
    assert len(raw_rows) == 2
    assert raw_rows[1][raw_rows[0].index("id")] == own["employee"].id
    _assert_inline(team, own, "xlsx", 1, "team")
    pdf = _post_resource(client, own, "contacts", fmt="pdf")
    assert own["owner"].email in _pdf_text(_artifact_bytes(pdf, export_context))
    assert foreign["owner"].email not in _pdf_text(pdf.data)
    _assert_inline(pdf, own, "pdf", 34, "contacts")


def test_each_resource_rejects_filters_it_cannot_apply(client, export_context):
    own = export_context["records"][0]
    for resource, filters in (("contacts", {"statuses": ["nuevo"]}),
                              ("team", {"categories": ["arbolado"]}),
                              ("orders", {"categories": ["arbolado"]})):
        response = _post_resource(client, own, resource, filters=filters)
        assert response.status_code == 400
        assert response.get_json()["reason_code"] == "invalid_export_filters"
        assert "X-Export-Complete" not in response.headers
    assert not export_context["export_dir"].exists()


def test_pdf_keeps_spanish_unicode_and_escapes_markup(client, export_context):
    own = export_context["records"][0]
    label = "Atención Ñandú · reparación & <control>"
    own["cases"][0].pregunta = label
    db.session.commit()
    response = _post(client, own, "pdf")
    assert label in _pdf_text(_artifact_bytes(response, export_context))
    _assert_inline(response, own, "pdf", 31)


def test_hostile_correlation_id_never_controls_a_file_or_download_name(client, export_context):
    own = export_context["records"][0]
    hostile = r"../../outside\exports%2Fshared"
    first = _post(client, own, request_id=hostile)
    second = _post(client, own, request_id=hostile)
    for response in (first, second):
        _assert_inline(response, own, "csv", 31)
        _, options = parse_options_header(response.headers["Content-Disposition"])
        assert "outside" not in options["filename"] and "%" not in options["filename"]
    assert first.headers["Content-Disposition"] != second.headers["Content-Disposition"]
    assert not export_context["export_dir"].exists()


def test_spreadsheet_formulas_are_neutralized_without_changing_xlsx_string_values(client, export_context):
    from openpyxl import load_workbook
    own = export_context["records"][0]
    formulas = ('=SUM(1,2)', '+SUM(1,2)', '-SUM(1,2)', '@SUM(1,2)', '\t=SUM(1,2)')
    for case, formula in zip(own["cases"], formulas):
        case.pregunta = formula
    db.session.commit()
    csv_response = _post(client, own)
    rows = _rows(_artifact_bytes(csv_response, export_context))
    by_id = {int(row["id"]): row for row in rows}
    for case, formula in zip(own["cases"], formulas):
        assert by_id[case.id]["title"] == "'" + formula
    xlsx_response = _post(client, own, "xlsx")
    workbook = load_workbook(io.BytesIO(_artifact_bytes(xlsx_response, export_context)), read_only=True, data_only=False)
    try:
        raw_rows = list(workbook.active.iter_rows())
        headers = [cell.value for cell in raw_rows[0]]
        by_id = {int(row[headers.index("id")].value): row[headers.index("title")] for row in raw_rows[1:]}
        for case, formula in zip(own["cases"], formulas):
            assert by_id[case.id].value == formula and by_id[case.id].data_type == "s"
    finally:
        workbook.close()


def test_synchronous_export_size_limit_fails_closed_without_partial_bytes(client, export_context, monkeypatch):
    own = export_context["records"][0]
    monkeypatch.setattr("routes.backoffice._EXPORT_MAX_BYTES", 128)
    response = _post(client, own)
    assert response.status_code == 413
    assert response.get_json()["reason_code"] == "export_too_large"
    assert "X-Export-Complete" not in response.headers
    assert b"SYNTHETIC-CASE" not in response.data
    assert not export_context["export_dir"].exists()


def test_no_matching_status_exports_a_genuine_complete_empty_result(client, export_context):
    own = export_context["records"][0]
    response = _post(client, own, filters={"statuses": ["synthetic-no-match"]})
    assert _rows(_artifact_bytes(response, export_context)) == []
    _assert_inline(response, own, "csv", 0)


def test_allowed_cors_origin_sees_only_export_contract_metadata(client, export_context, monkeypatch):
    own = export_context["records"][0]
    monkeypatch.setitem(client.application.config, "BACKEND_URL", "https://api.example.invalid")
    monkeypatch.setitem(client.application.config, "PUBLIC_ROOT_DOMAIN", "example.invalid")
    response = client.post("/api/v2/backoffice/export", json={
        "tenant_slug": own["tenant"].slug, "tenant_id": own["tenant"].id,
        "resource": "tickets", "format": "csv", "delivery": "inline",
    }, headers={**_headers(own["owner"], own["tenant"]), "Origin": "https://panel.example.invalid"})
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "https://panel.example.invalid"
    _assert_inline(response, own, "csv", 31)


def test_canonical_and_legacy_colliding_ids_remain_distinct_and_category_scoped(client, export_context):
    own, foreign = export_context["records"]
    canonical = TenantTicket(id=own["cases"][0].id, tenant_id=own["tenant"].id,
                             user_id=own["owner"].id, categoria="arbolado",
                             descripcion="Synthetic canonical description", estado="cerrado", origen="pwa",
                             datos_extra={"title": "Synthetic canonical title", "legacy_id": own["cases"][0].id})
    restricted = TenantTicket(id=9999, tenant_id=own["tenant"].id, user_id=own["owner"].id,
                              categoria="bacheo", descripcion="Synthetic restricted canonical",
                              estado="resuelto", origen="web", datos_extra={})
    other = TenantTicket(id=foreign["cases"][0].id, tenant_id=foreign["tenant"].id,
                         user_id=foreign["owner"].id, categoria="arbolado",
                         descripcion="Synthetic foreign canonical", estado="nuevo", origen="pwa", datos_extra={})
    db.session.add_all([canonical, restricted, other])
    db.session.commit()
    response = _post(client, own)
    rows = _rows(_artifact_bytes(response, export_context))
    identities = {(row["source_model"], int(row["id"])) for row in rows}
    expected = {("MunicipioTicket", case.id) for case in own["cases"]} | {
        ("TenantTicket", canonical.id), ("TenantTicket", restricted.id)}
    assert identities == expected and len(rows) == len(expected) == 33
    assert len({row["record_key"] for row in rows}) == 33
    assert ("TenantTicket", other.id) not in identities
    canonical_row = next(row for row in rows if row["record_key"] == f"TenantTicket:{canonical.id}")
    assert canonical_row["description"] == canonical.descripcion and canonical_row["status"] == "cerrado"
    assert "attachments" not in canonical_row and "history" not in canonical_row
    _assert_inline(response, own, "csv", 33)
    scoped = _post(client, own, actor=own["employee"])
    scoped_rows = _rows(_artifact_bytes(scoped, export_context))
    expected_scoped = {("MunicipioTicket", case.id) for case in own["cases"] if case.categoria == "arbolado"} | {
        ("TenantTicket", canonical.id)}
    assert {(row["source_model"], int(row["id"])) for row in scoped_rows} == expected_scoped
    _assert_inline(scoped, own, "csv", len(expected_scoped))
    filtered = _post(client, own, filters={"statuses": ["cerrado"], "categories": ["arbolado"]})
    filtered_rows = _rows(_artifact_bytes(filtered, export_context))
    assert {row["record_key"] for row in filtered_rows} == {f"TenantTicket:{canonical.id}"}
    _assert_inline(filtered, own, "csv", 1)


@pytest.mark.parametrize("kind", ["colegio", "pyme"])
def test_school_and_pyme_export_canonical_and_correct_legacy_domain(client, export_context, kind):
    owner = User(name="Synthetic vertical export owner", email="vertical-" + kind + "@example.invalid",
                 rol="admin", tipo_chat="pyme")
    owner.set_password("synthetic-local-export-only")
    db.session.add(owner)
    db.session.flush()
    # Existing school setup is a colegio tenant backed by its PyME owner.
    owner_field = "pyme_id"
    tenant = TenantProfile(slug="synthetic-export-" + kind, nombre="Synthetic " + kind,
                           tipo=kind, plan="enterprise", is_active=True, **{owner_field: owner.id})
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
    setattr(owner, owner_field, owner.id)
    canonical = TenantTicket(tenant_id=tenant.id, user_id=owner.id, categoria="general",
                             descripcion="Synthetic " + kind + " canonical", estado="resuelto", origen="web", datos_extra={})
    db.session.add(canonical)
    # Neither vertical may inherit a municipal record with a conflicting
    # domain merely because its numeric owner happens to match.
    municipal = MunicipioTicket(tenant_id=tenant.id, municipio_id=owner.id,
                                pregunta="Synthetic cross-domain municipal", categoria="general", estado="nuevo")
    db.session.add(municipal)
    pyme = PymeTicket(tenant_id=tenant.id, pregunta="Synthetic " + kind + " legacy", nro_ticket=9001,
                      categoria="general", estado="cerrado")
    db.session.add(pyme)
    db.session.commit()
    record = {"owner": owner, "tenant": tenant}
    response = _post(client, record, filters={"scope": kind})
    rows = _rows(_artifact_bytes(response, export_context))
    expected = {("TenantTicket", canonical.id), ("PymeTicket", pyme.id)}
    assert {(row["source_model"], int(row["id"])) for row in rows} == expected
    assert not any(row["source_model"] == "MunicipioTicket" for row in rows)
    _assert_inline(response, record, "csv", len(expected))
