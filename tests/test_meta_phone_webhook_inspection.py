"""Real private auth/vault/SQLite; every Graph response is synthetic and offline."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from cutover_writer_fence import is_cutover_read_only_view
from models import (
    AuditEvent, AuthProviderSession, ProviderConnection, ProviderSender,
    TenantProfile, User, WhatsappNumero, db,
)
from routes import meta_credential_admin as route
from services import meta_credential_admin_runtime as admin
from services import meta_credential_operations as ops
from services import meta_phone_webhook_inspection as inspection
from services import meta_whatsapp_credentials as vault
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY
from tests.test_meta_credential_admin import http, post, body, assert_private
from tests.test_meta_credential_operations import environment, TOKEN, KEY, APP_SECRET
from utils import meta_credential_http as boundary

ACTION = "inspect-junin-webhook"
JUNIN, TDF = ops.TARGETS["junin-production"], ops.TARGETS["tdf-sandbox"]
# Deliberately synthetic: no claim that this is Inmovar's real provider ID.
INMOVAR_ID = "900000000000001"
PRIVATE_URL = "https://private-user:private-password@internal.example.invalid/private-path?verify_token=private-value#private-fragment"
TDF_APP_SECRET = "synthetic-distinct-sandbox-app-secret"


def response(payload, status=200):
    return SimpleNamespace(status_code=status, content=json.dumps(payload).encode())


@pytest.fixture
def installed(http, monkeypatch):
    tokens = {JUNIN.name: TOKEN + "_junin", TDF.name: TOKEN + "_tdf"}
    http.app.config.update(META_TDF_SANDBOX_APP_SECRET=TDF_APP_SECRET,
        META_TDF_SANDBOX_GRAPH_VERSION="v24.0", META_WHATSAPP_CALLBACK_ENABLED=False,
        META_WHATSAPP_INBOUND_WORKER_ENABLED=False, META_WHATSAPP_OUTBOUND_ENABLED=False)
    for target in (JUNIN, TDF):
        value = {**body(target.name), "access_token": tokens[target.name]}
        planned = post(http, "plan", value)
        assert planned.status_code == 200, planned.json
        value.update(expires_at=planned.json["plan"]["expires_at"],
            approved_plan_sha256=planned.json["plan_sha256"],
            expected_database_identity_sha256=planned.json["plan"]["database_identity_sha256"])
        applied = post(http, "apply", value)
        assert applied.status_code == 200 and applied.json["committed"] is True, applied.json
    http.calls.clear()
    http.lease["enters"] = 0
    calls = []
    state = SimpleNamespace(http=http, calls=calls, tokens=tokens, alter=None,
                            before_get=None, active_transaction=False)

    def graph(method, url, **kwargs):
        assert method == "GET" and kwargs["allow_redirects"] is False and kwargs["verify"] is True
        assert state.active_transaction is False, "Graph must be outside both database transactions"
        calls.append((method, url, deepcopy(kwargs)))
        if state.before_get is not None:
            state.before_get(method, url, kwargs)
        assert url.startswith("https://graph.facebook.com/")
        if url.endswith("/debug_token"):
            name = next(name for name, token in tokens.items() if kwargs["params"] == {"input_token": token})
            expected_secret = APP_SECRET if name == JUNIN.name else TDF_APP_SECRET
            assert kwargs["headers"] == {"Authorization": "Bearer " + ops.APP_ID + "|" + expected_secret}
            payload = {"data": {"is_valid": True, "app_id": ops.APP_ID,
                "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"],
                "expires_at": http.now + 7200, "data_access_expires_at": http.now + 7200}}
        else:
            name = TDF.name if TDF.waba_id in url or url.endswith("/" + TDF.phone_id) else JUNIN.name
            assert kwargs["headers"] == {"Authorization": "Bearer " + tokens[name]}
            target = ops.TARGETS[name]
            version = "v24.0" if name == TDF.name else "v25.0"
            assert url.startswith("https://graph.facebook.com/" + version + "/")
            if url.endswith("/phone_numbers"):
                assert kwargs["params"] == {"fields": "id,display_phone_number", "limit": "100"}
                payload = {"data": [{"id": target.phone_id, "display_phone_number": target.phone}], "paging": {}}
                if target is JUNIN:
                    payload["data"].append({"id": INMOVAR_ID, "display_phone_number": inspection.INMOVAR_PHONE})
            elif url.endswith("/subscribed_apps"):
                payload = {"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID},
                                     "private_unknown_url": PRIVATE_URL}], "paging": {}}
            else:
                identifier = url.rsplit("/", 1)[-1]
                assert identifier in {JUNIN.phone_id, TDF.phone_id, INMOVAR_ID}
                assert kwargs["params"] == {"fields": "id,display_phone_number,webhook_configuration"}
                phone = inspection.INMOVAR_PHONE if identifier == INMOVAR_ID else target.phone
                callback = PRIVATE_URL if identifier == INMOVAR_ID else target.callback
                payload = {"id": identifier, "display_phone_number": phone,
                    "webhook_configuration": {"application": PRIVATE_URL, "phone_number": callback},
                    "unrelated_private_data": tokens[name]}
        result = response(payload)
        return state.alter(url, payload, result) if state.alter else result

    state.graph = graph
    monkeypatch.setattr(inspection, "native_graph_request", graph)
    return state


def snapshot():
    result = {model.__name__: [ops._columns(row) for row in db.session.scalars(select(model)).unique().all()]
              for model in (ProviderConnection, ProviderSender, TenantProfile, WhatsappNumero, AuditEvent)}
    db.session.rollback()
    db.session.remove()
    return result


def assert_safe(result):
    assert_private(result)
    raw = result.get_data(as_text=True)
    for value in (PRIVATE_URL, "private-user", "private-password", "internal.example.invalid", "private-path",
                  "private-value", "private-fragment", TDF_APP_SECRET, "unrelated_private_data", "access_token"):
        assert value not in raw
    if result.status_code == 200:
        assert result.json["contract"] == inspection.CONTRACT
    if "write_ready" in result.json:
        assert result.json["write_ready"] is False and result.json["inspection_complete"] is False
        assert result.json["provider_mutations"] is False and result.json["messages_sent"] is False


def test_private_cookie_reads_exact_bindings_without_business_writes_or_writer_lease(installed, monkeypatch):
    http = installed.http
    before = snapshot()
    statements, transactions = [], []
    original = admin._transaction

    @contextmanager
    def track(engine, *, read_only):
        transactions.append(read_only)
        assert read_only is True
        installed.active_transaction = True
        try:
            with original(engine, read_only=read_only) as session:
                yield session
        finally:
            installed.active_transaction = False

    def sql(_conn, _cursor, statement, *_):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    monkeypatch.setattr(admin, "_transaction", track)
    http.client.set_cookie("auth_token", http.bearer)
    event.listen(db.engine, "before_cursor_execute", sql)
    try:
        result = post(http, ACTION, headers={"Origin": "https://www.chatboc.ar"})
    finally:
        event.remove(db.engine, "before_cursor_execute", sql)
    assert result.status_code == 200, result.json
    assert transactions == [True, True] and http.lease["enters"] == 0
    assert not {"INSERT", "UPDATE", "DELETE", "REPLACE"} & set(statements)
    assert snapshot() == before
    assert result.json["writes_performed"] is False
    assert len(installed.calls) == 9
    resources = result.json["resources"]
    assert [row["resource"] for row in resources] == ["junin", "inmovar", "tdf"]
    assert [row["phone_number_id"] for row in resources] == [JUNIN.phone_id, INMOVAR_ID, TDF.phone_id]
    assert resources[0]["configuration"]["known_fields"]["phone_number"]["public_callback"] == JUNIN.callback
    assert resources[1]["configuration"]["known_fields"]["phone_number"]["public_callback"] is None
    assert resources[2]["graph_version"] == "v24.0"
    assert all(row["configuration"]["schema_verified"] is False for row in resources)
    assert [(row["environment"], row["revision"]) for row in result.json["credentials"]] == [("production", 1), ("sandbox", 7)]
    assert all(row["inventory_complete"] and row["authority_verified"] for row in result.json["credentials"])
    assert result.json["checked_at"] < result.json["valid_until"]
    assert is_cutover_read_only_view(http.app.view_functions["meta_credential_admin.inspect_junin_webhook"])
    assert_safe(result)


@pytest.mark.parametrize("value", [{"target": "tdf-sandbox"}, {"target": "other"},
    {"target": "junin-production", "phone_number_id": INMOVAR_ID},
    {"target": "junin-production", "url": PRIVATE_URL},
    {"target": "junin-production", "access_token": TOKEN},
    {"target": "junin-production", "actor_user_id": 1},
    {"target": "junin-production", "tenant_id": 46}])
def test_client_cannot_choose_scope_token_or_resource(http, monkeypatch, value):
    monkeypatch.setattr(inspection, "inspect", lambda *_: pytest.fail("Rejected before vault or Graph"))
    result = post(http, ACTION, value)
    assert result.status_code == 400
    assert_safe(result)


@pytest.mark.parametrize("case", ["missing_auth", "invalid_bearer", "bad_origin", "flag_off", "wrong_runtime",
                                 "query", "duplicate_keys", "redis_required", "rate_failure"])
def test_exact_route_keeps_private_boundary(http, monkeypatch, case):
    monkeypatch.setattr(inspection, "inspect", lambda *_: pytest.fail("Rejected before vault or Graph"))
    headers = {"Authorization": "Bearer " + http.bearer}
    if case in {"missing_auth", "invalid_bearer", "bad_origin"}:
        headers = {}
        if case != "missing_auth":
            http.client.set_cookie("auth_token", http.bearer)
            headers["Origin"] = "https://www.chatboc.ar" if case == "invalid_bearer" else "https://evil.invalid"
        if case == "invalid_bearer":
            headers["Authorization"] = "Bearer invalid"
    if case == "flag_off": http.app.config["META_CREDENTIAL_ADMIN_ENABLED"] = False
    if case == "wrong_runtime": monkeypatch.setattr(boundary, "runtime_is_production", lambda: False)
    if case == "redis_required": http.app.config["TESTING"] = False
    if case == "rate_failure":
        from extensions import limiter
        def fail(*_): raise RuntimeError(TOKEN)
        monkeypatch.setattr(limiter.limiter, "hit", fail)
    action = ACTION + "?access_token=forbidden" if case == "query" else ACTION
    if case == "duplicate_keys":
        result = http.client.post(boundary.PREFIX + "/" + ACTION,
            data='{"target":"junin-production","target":"junin-production"}',
            content_type="application/json", headers=headers)
    else:
        result = post(http, action, headers=headers)
    assert result.status_code in {400, 401, 403, 404, 503}, result.json
    assert_safe(result)


@pytest.mark.parametrize("case", ["tenant", "environment", "provider", "app", "waba", "phone", "status", "shared", "foreign_sender", "expiry", "uninstalled"])
def test_invalid_local_binding_fails_before_any_graph_get(installed, case):
    connection = db.session.get(ProviderConnection, 3)
    sender = db.session.get(ProviderSender, 1)
    if case == "tenant": db.session.get(TenantProfile, 46).slug = "foreign"
    if case == "environment": connection.environment = "production"
    if case == "provider": connection.provider = "twilio"
    if case == "app": connection.external_app_id = "900000000000002"
    if case == "waba": connection.external_account_id = JUNIN.waba_id
    if case == "phone": sender.phone_number = JUNIN.phone
    if case == "status": sender.status = "disconnected"
    if case == "shared":
        db.session.add(ProviderSender(tenant_id=46, provider_connection_id=3, channel="whatsapp", phone_number_id="900000000000002"))
    if case == "foreign_sender":
        db.session.add(ProviderSender(tenant_id=22, provider_connection_id=1, channel="whatsapp", phone_number_id=TDF.phone_id))
    if case in {"expiry", "uninstalled"}:
        revision = 6 if case == "uninstalled" else 7
        expiry = installed.http.now - 1 if case == "expiry" else installed.http.now + 3600
        connection.config = {**connection.config, PRIVATE_CONFIG_KEY: vault.seal_token(connection=connection,
            tenant_id=46, access_token=installed.tokens[TDF.name], revision=revision,
            expires_at=expiry, now=installed.http.now - 100, app_config=installed.http.app.config)}
    db.session.commit()
    result = post(installed.http, ACTION)
    assert result.status_code == 409, result.json
    assert installed.calls == []
    assert_safe(result)


@pytest.mark.parametrize("case", ["missing", "duplicate"])
def test_inmovar_requires_unique_inventory_match_without_guessing(installed, case):
    def alter(url, payload, result):
        if url.endswith(JUNIN.waba_id + "/phone_numbers"):
            if case == "missing": payload["data"] = payload["data"][:1]
            else: payload["data"].append({"id": "900000000000002", "display_phone_number": inspection.INMOVAR_PHONE})
            return response(payload)
        return result
    installed.alter = alter
    result = post(installed.http, ACTION)
    assert result.status_code == 200, result.json
    assert "meta_inspection_inmovar_not_unique" in result.json["blockers"]
    assert result.json["resources"][1]["phone_number_id"] is None
    assert result.json["resources"][1]["configuration"]["state"] == "not_requested"
    assert len(installed.calls) == 8
    assert not any(url.endswith("/" + INMOVAR_ID) for _, url, _ in installed.calls)
    assert_safe(result)


@pytest.mark.parametrize("edge", [JUNIN.waba_id + "/phone_numbers", TDF.waba_id + "/phone_numbers",
                                  JUNIN.waba_id + "/subscribed_apps", TDF.waba_id + "/subscribed_apps"])
def test_incomplete_pagination_never_follows_private_url_or_claims_absence(installed, edge):
    def alter(url, payload, result):
        if url.endswith(edge):
            payload["paging"] = {"next": PRIVATE_URL}
            return response(payload)
        return result
    installed.alter = alter
    result = post(installed.http, ACTION)
    assert result.status_code == 409 and len(installed.calls) <= 6, result.json
    assert "resources" not in result.json
    assert not any(url == PRIVATE_URL for _, url, _ in installed.calls)
    assert_safe(result)


@pytest.mark.parametrize("case", ["missing", "null", "empty", "unknown_key", "array", "nested", "bad_id", "bad_phone", "400", "429", "redirect", "invalid_json"])
def test_discovery_is_incomplete_even_when_missing_or_unknown_not_absent_override(installed, case):
    def alter(url, payload, result):
        if not url.endswith("/" + JUNIN.phone_id): return result
        if case == "missing": payload.pop("webhook_configuration")
        if case == "null": payload["webhook_configuration"] = None
        if case == "empty": payload["webhook_configuration"] = {}
        if case == "unknown_key": payload["webhook_configuration"] = {PRIVATE_URL: TOKEN}
        if case == "array": payload["webhook_configuration"] = [PRIVATE_URL]
        if case == "nested": payload["webhook_configuration"] = {"phone_number": {"private": PRIVATE_URL}}
        if case == "bad_id": payload["id"] = INMOVAR_ID
        if case == "bad_phone": payload["display_phone_number"] = inspection.INMOVAR_PHONE
        if case in {"400", "429", "redirect"}: return response({"error": {"message": TOKEN + PRIVATE_URL}}, 302 if case == "redirect" else int(case))
        if case == "invalid_json": return SimpleNamespace(status_code=200, content=(TOKEN + PRIVATE_URL).encode())
        return response(payload)
    installed.alter = alter
    result = post(installed.http, ACTION)
    assert result.status_code == 200 and len(installed.calls) == 9, result.json
    config = result.json["resources"][0]["configuration"]
    assert config["state"] in {"missing", "unknown_shape", "unavailable"}
    assert config["schema_verified"] is False
    assert "no_override" not in result.get_data(as_text=True)
    assert_safe(result)


@pytest.mark.parametrize("case", ["revocation", "role", "tenant", "credential", "sender"])
def test_final_database_revalidation_discards_observation_after_change(installed, case):
    changed = []
    def mutate(_method, url, _kwargs):
        if changed or not url.endswith("/" + TDF.phone_id): return
        changed.append(True)
        if case == "revocation":
            row = AuthProviderSession.query.filter_by(provider_session_id="synthetic-clerk-admin-session").one()
            row.revoked_at = datetime.now(timezone.utc)
        if case == "role": db.session.get(User, 1).rol = "admin"
        if case == "tenant": db.session.get(TenantProfile, 22).is_active = False
        if case == "sender": db.session.get(ProviderSender, 1).webhook_url = PRIVATE_URL
        if case == "credential":
            connection = db.session.get(ProviderConnection, 3)
            connection.config = {**connection.config, PRIVATE_CONFIG_KEY: vault.seal_token(connection=connection,
                tenant_id=46, access_token=installed.tokens[TDF.name] + "_rotated", revision=8,
                expires_at=installed.http.now + 3600, now=installed.http.now, app_config=installed.http.app.config)}
        db.session.commit()
        db.session.remove()
    installed.before_get = mutate
    result = post(installed.http, ACTION)
    assert changed and result.status_code == 409, result.json
    assert "resources" not in result.json
    assert_safe(result)


@pytest.mark.parametrize("clock_case", ["proof", "session", "backwards"])
def test_proof_and_actor_expiry_are_checked_after_second_transaction(installed, monkeypatch, clock_case):
    current = [installed.http.now]
    original_inspect, original_transaction = inspection.inspect, admin._transaction
    transactions = []
    if clock_case == "session":
        original_authenticate = route._authenticate
        def authenticate():
            from dataclasses import replace
            return replace(original_authenticate(), expires_at=installed.http.now + 20)
        monkeypatch.setattr(route, "_authenticate", authenticate)
    def inspect(*args, **kwargs):
        return original_inspect(*args, **kwargs, clock=lambda: current[0])
    @contextmanager
    def transaction(engine, *, read_only):
        with original_transaction(engine, read_only=read_only) as session:
            yield session
        transactions.append(True)
        if len(transactions) == 2:
            current[0] += {"proof": 61, "session": 21, "backwards": -1}[clock_case]
    monkeypatch.setattr(inspection, "inspect", inspect)
    monkeypatch.setattr(admin, "_transaction", transaction)
    result = post(installed.http, ACTION)
    assert result.status_code == 409 and result.json["reason_code"] == "meta_inspection_observation_expired", result.json
    assert len(transactions) == 2 and len(installed.calls) == 9
    assert_safe(result)


def test_private_errors_and_third_party_logs_never_expose_vault_or_provider(installed, monkeypatch, caplog):
    def failed(*_, **__):
        try:
            raise RuntimeError(TOKEN + PRIVATE_URL)
        except RuntimeError:
            logging.getLogger("synthetic-provider").exception("raw %s %s", KEY, APP_SECRET)
            raise
    monkeypatch.setattr(inspection, "native_graph_request", failed)
    with caplog.at_level(logging.DEBUG):
        result = post(installed.http, ACTION)
        logging.getLogger("ordinary-request").warning("ordinary diagnostic retained")
    assert result.status_code == 409
    assert "ordinary diagnostic retained" in caplog.text
    for value in (TOKEN, KEY, APP_SECRET, PRIVATE_URL): assert value not in caplog.text
    assert_safe(result)


def test_internal_transport_rejects_mutation_foreign_url_and_unbounded_calls(installed):
    with admin._transaction(db.engine, read_only=True) as session:
        binding = inspection._load_local(session, JUNIN, installed.http.app.config, installed.http.now)
    reader = inspection._GraphReads(binding, installed.http.app.config, installed.graph)
    for method, url in (("POST", reader.base + "debug_token"),
                        ("GET", PRIVATE_URL), ("GET", reader.base + TDF.phone_id)):
        with pytest.raises(inspection.MetaPhoneInspectionError, match="get_only"):
            reader.get(method, url, allow_redirects=False, verify=True)
    reader.calls = 5
    with pytest.raises(inspection.MetaPhoneInspectionError, match="get_only"):
        reader.get("GET", reader.base + "debug_token", allow_redirects=False, verify=True)
    assert not installed.calls
