"""Offline SQLite HTTP roundtrips for the notification configuration contract."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from app import create_app, db
from config import Config
from models import TenantProfile, User
from services.auth_session_lifecycle import issue_token


class NotificationHooksTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


@pytest.fixture
def notification_state():
    app = create_app(NotificationHooksTestConfig)
    with app.app_context():
        db.create_all()
        owner = User(name="Notification owner", email="notifications@test.com", rol="admin")
        owner.set_password("notification-test-only")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="notification-contract", nombre="Notification contract", tipo="pyme",
            pyme_id=owner.id, configuracion={"unrelated": {"keep": True}},
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.tenant_slug = tenant.slug
        db.session.commit()
        tenant_id, slug = tenant.id, tenant.slug
        token = issue_token({
            "user_id": owner.id, "rol": owner.rol, "tenant_slug": slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        })
        yield {
            "client": app.test_client(), "tenant_id": tenant_id,
            "headers": {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug},
            "legacy_path": f"/api/admin/tenants/{slug}/notifications", "slug": slug,
        }
        db.session.remove()
        db.drop_all()


def store_config(state, value):
    tenant = db.session.get(TenantProfile, state["tenant_id"])
    tenant.configuracion = deepcopy(value)
    db.session.commit()


def reload_config(state):
    db.session.expire_all()
    return deepcopy(db.session.get(TenantProfile, state["tenant_id"]).configuracion)


def request_v2(state, method="get", **kwargs):
    return getattr(state["client"], method)(
        "/api/v2/notifications/hooks", headers=state["headers"], **kwargs,
    )


def test_post_persists_after_expire_reload_and_get_in_new_session(notification_state):
    state = notification_state
    original = {
        "unrelated": {"keep": True},
        "notification_hooks": {
            "preferences": {"email": False, "telegram": True, "future_channel": {"mode": "digest"}},
            "delivery": {"owner_phone": "+100", "telegram_chat_id": "old-chat", "quiet_hours": {"start": 22}},
            "triggers": [{"event": "ticket.created", "channel": "email"}],
            "future_hook": {"keep": "hook"},
        },
    }
    store_config(state, original)
    response = request_v2(state, "post", json={
        "preferences": {"email": True}, "delivery": {"owner_phone": "+200"},
    })
    assert response.status_code == 200
    expected = deepcopy(original["notification_hooks"])
    expected["preferences"]["email"] = True
    expected["delivery"]["owner_phone"] = "+200"
    persisted = reload_config(state)
    assert persisted["notification_hooks"] == expected
    assert persisted["unrelated"] == original["unrelated"]
    assert persisted["notification_settings"] == expected["preferences"]
    assert persisted["owner_notification_phone"] == "+200"
    assert persisted["owner_telegram_chat_id"] == "old-chat"
    old_session = db.session()
    db.session.remove()
    assert db.session() is not old_session
    fresh = request_v2(state).get_json()
    assert fresh["preferences"] == expected["preferences"]
    assert fresh["delivery"] == expected["delivery"]
    assert fresh["delivery_config"] == fresh["delivery"]


def test_empty_initial_dto_does_not_mutate_configuration(notification_state):
    state = notification_state
    before = reload_config(state)
    response = request_v2(state)
    assert response.status_code == 200
    dto = response.get_json()
    assert dto["contract_version"] == "notifications.hooks.v1"
    assert dto["tenant"]["id"] == state["tenant_id"]
    assert dto["tenant"]["slug"] == state["slug"]
    assert dto["preferences"] == {}
    assert dto["delivery"] == {"owner_phone": None, "telegram_chat_id": None}
    assert dto["triggers"] == []
    assert reload_config(state) == before


def test_v2_reads_legacy_only_when_canonical_field_is_absent(notification_state):
    state = notification_state
    store_config(state, {
        "notification_settings": {"email": True, "future_channel": {"enabled": True}},
        "owner_notification_phone": "+300", "owner_telegram_chat_id": "legacy-chat",
    })
    before = reload_config(state)
    dto = request_v2(state).get_json()
    assert dto["preferences"] == before["notification_settings"]
    assert dto["delivery"] == {"owner_phone": "+300", "telegram_chat_id": "legacy-chat"}
    assert reload_config(state) == before
    store_config(state, {
        **before,
        "notification_hooks": {"preferences": {}, "delivery": {"owner_phone": None, "telegram_chat_id": ""}},
    })
    dto = request_v2(state).get_json()
    assert dto["preferences"] == {}
    assert dto["delivery"] == {"owner_phone": None, "telegram_chat_id": ""}


def test_legacy_and_v2_writes_reconcile_the_same_configuration(notification_state):
    state = notification_state
    store_config(state, {
        "notification_settings": {"email": True, "future_channel": {"mode": "digest"}},
        "owner_notification_phone": "+300", "owner_telegram_chat_id": "legacy-chat",
        "unrelated": {"keep": True},
    })
    response = request_v2(state, "post", json={"preferences": {"telegram": False}})
    assert response.status_code == 200
    legacy = state["client"].get(state["legacy_path"], headers=state["headers"]).get_json()
    assert legacy["notification_settings"] == {"email": True, "telegram": False, "future_channel": {"mode": "digest"}}
    assert legacy["owner_phone"] == "+300"
    response = state["client"].put(state["legacy_path"], headers=state["headers"], json={
        "owner_phone": "", "telegram_chat_id": None, "notification_settings": {"email": False},
    })
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    db.session.remove()
    fresh = request_v2(state).get_json()
    assert fresh["preferences"] == {"email": False, "telegram": False, "future_channel": {"mode": "digest"}}
    assert fresh["delivery"] == {"owner_phone": "", "telegram_chat_id": None}
    assert reload_config(state)["unrelated"] == {"keep": True}


def test_old_frontend_alias_roundtrip_and_inert_legacy_payload(notification_state):
    state = notification_state
    response = request_v2(state, "post", json={
        "preferences": {"email": True},
        "delivery_config": {"owner_phone": "+400", "telegram_chat_id": "old-fe-chat"},
        "legacy_payload": {"owner_phone": "ignored", "notification_settings": {"email": False}, "unrelated": "ignored"},
    })
    assert response.status_code == 200
    db.session.remove()
    dto = request_v2(state).get_json()
    assert dto["delivery"] == dto["delivery_config"] == {"owner_phone": "+400", "telegram_chat_id": "old-fe-chat"}
    assert dto["preferences"] == {"email": True}
    assert reload_config(state)["unrelated"] == {"keep": True}


@pytest.mark.parametrize("body", [
    None, False, True, 1, "body", [], [1],
    {"preferences": []}, {"preferences": {"email": "true"}},
    {"preferences": {"telegram": 1}}, {"preferences": {"whatsapp": None}},
    {"delivery": []}, {"delivery": {"owner_phone": 1}},
    {"delivery": {"telegram_chat_id": False}}, {"triggers": {}},
    {"triggers": ["event"]}, {"triggers": [{"event": 1}]},
    {"triggers": [{"channel": False}]}, {"delivery_config": {"owner_phone": []}},
    {"legacy_payload": []}, {"unknown": True},
    {"preferences": {"future_channel": float("nan")}},
    {"delivery": {"owner_phone": "+1"}, "delivery_config": {"owner_phone": "+2"}},
    {"delivery": {"quiet_hours": {"enabled": True}}, "delivery_config": {"quiet_hours": {"enabled": 1}}},
])
def test_invalid_request_rejected_before_auxiliary_reads_or_commit(notification_state, body):
    state = notification_state
    before = reload_config(state)
    with patch("routes.v2.saas._notification_delivery_status") as status, patch("routes.v2.saas._templates_payload") as templates, patch.object(db.session, "commit") as commit:
        response = request_v2(state, "post", data=json.dumps(body), content_type="application/json")
        assert response.status_code == 400
        status.assert_not_called()
        templates.assert_not_called()
        commit.assert_not_called()
    assert reload_config(state) == before


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("period", ["abc", "1.5", "0", "91", "", "-1", "7&period_days=8"])
def test_invalid_period_is_rejected_before_mutation_or_auxiliary_reads(notification_state, method, period):
    state = notification_state
    before = reload_config(state)
    with patch("routes.v2.saas._notification_delivery_status") as status, patch("routes.v2.saas._templates_payload") as templates, patch.object(db.session, "commit") as commit:
        response = getattr(state["client"], method)(
            f"/api/v2/notifications/hooks?period_days={period}", headers=state["headers"],
            **({"json": {"preferences": {"email": True}}} if method == "post" else {}),
        )
        assert response.status_code == 400
        status.assert_not_called()
        templates.assert_not_called()
        commit.assert_not_called()
    assert reload_config(state) == before


def test_auxiliary_read_failure_cannot_follow_a_configuration_commit(notification_state):
    state = notification_state
    before = reload_config(state)
    with patch("routes.v2.saas._templates_payload", side_effect=RuntimeError("offline read failure")), patch.object(db.session, "commit") as commit:
        with pytest.raises(RuntimeError, match="offline read failure"):
            request_v2(state, "post", json={"preferences": {"email": True}})
        commit.assert_not_called()
    assert reload_config(state) == before


@pytest.mark.parametrize("stored_key", ["canonical", "legacy"])
@pytest.mark.parametrize("blank", [None, ""])
def test_stale_alias_only_clear_requires_refresh_without_mutation(notification_state, stored_key, blank):
    state = notification_state
    config = (
        {"notification_hooks": {"delivery": {"owner_phone": "+500", "telegram_chat_id": "stored-chat"}}}
        if stored_key == "canonical" else {"owner_notification_phone": "+500", "owner_telegram_chat_id": "stored-chat"}
    )
    store_config(state, config)
    with patch("routes.v2.saas._notification_delivery_status") as status, patch("routes.v2.saas._templates_payload") as templates, patch.object(db.session, "commit") as commit:
        response = request_v2(state, "post", json={
            "preferences": {"email": True}, "delivery_config": {"owner_phone": blank},
        })
        assert response.status_code == 409
        assert response.get_json()["reason_code"] == "notification_settings_refresh_required"
        status.assert_not_called()
        templates.assert_not_called()
        commit.assert_not_called()
    assert reload_config(state) == config
    # The new client can explicitly express a clear through canonical delivery.
    response = request_v2(state, "post", json={"delivery": {"owner_phone": blank}})
    assert response.status_code == 200
    db.session.remove()
    assert request_v2(state).get_json()["delivery"]["owner_phone"] == blank


def test_matching_and_disjoint_delivery_alias_values_are_accepted(notification_state):
    state = notification_state
    response = request_v2(state, "post", json={
        "delivery": {"owner_phone": "+600", "quiet_hours": {"start": 22, "end": 7}},
        "delivery_config": {"owner_phone": "+600", "telegram_chat_id": "same-alias"},
    })
    assert response.status_code == 200
    response = request_v2(state, "post", json={"delivery": {"owner_phone": "+601"}})
    assert response.status_code == 200
    db.session.remove()
    assert request_v2(state).get_json()["delivery"] == {
        "owner_phone": "+601", "telegram_chat_id": "same-alias", "quiet_hours": {"start": 22, "end": 7},
    }


@pytest.mark.parametrize("stored", [[], "bad", 1, {"notification_hooks": []}, {"notification_hooks": {"preferences": []}}, {"notification_hooks": {"delivery": {"owner_phone": False}}}])
def test_invalid_stored_configuration_is_not_replaced_by_empty_defaults(notification_state, stored):
    state = notification_state
    store_config(state, stored)
    with patch("routes.v2.saas._notification_delivery_status") as status, patch("routes.v2.saas._templates_payload") as templates, patch.object(db.session, "commit") as commit:
        response = request_v2(state, "post", json={"preferences": {"email": True}})
        assert response.status_code == 409
        assert response.get_json()["reason_code"] == "notification_settings_stored_invalid"
        status.assert_not_called()
        templates.assert_not_called()
        commit.assert_not_called()
    assert reload_config(state) == stored


def test_null_stored_configuration_is_a_valid_empty_initial_state(notification_state):
    state = notification_state
    store_config(state, None)
    assert request_v2(state).get_json()["preferences"] == {}
    response = request_v2(state, "post", json={"preferences": {"email": True}})
    assert response.status_code == 200
    db.session.remove()
    assert request_v2(state).get_json()["preferences"] == {"email": True}


def test_legacy_only_preference_extras_are_preserved_without_becoming_canonical_authority(notification_state):
    state = notification_state
    store_config(state, {
        "notification_hooks": {"preferences": {"email": False}},
        "notification_settings": {"email": True, "legacy_extra": {"schedule": ["morning"]}},
    })
    response = request_v2(state, "post", json={"preferences": {"telegram": True}})
    assert response.status_code == 200
    db.session.remove()
    assert request_v2(state).get_json()["preferences"] == {"email": False, "telegram": True}
    assert reload_config(state)["notification_settings"] == {
        "email": False, "telegram": True, "legacy_extra": {"schedule": ["morning"]},
    }


def test_malformed_legacy_preferences_are_not_discarded_during_canonical_write(notification_state):
    state = notification_state
    original = {"notification_hooks": {"preferences": {}}, "notification_settings": ["legacy-artifact"]}
    store_config(state, original)
    assert request_v2(state).get_json()["preferences"] == {}
    with patch.object(db.session, "commit") as commit:
        response = request_v2(state, "post", json={"preferences": {"email": True}})
        assert response.status_code == 409
        assert response.get_json()["reason_code"] == "notification_settings_stored_invalid"
        commit.assert_not_called()
    assert reload_config(state) == original


@pytest.mark.parametrize("raw,content_type", [("{invalid-json", "application/json"), ("{}", "text/plain")])
def test_malformed_or_non_json_body_is_rejected_without_commit(notification_state, raw, content_type):
    state = notification_state
    before = reload_config(state)
    with patch.object(db.session, "commit") as commit:
        response = request_v2(state, "post", data=raw, content_type=content_type)
        assert response.status_code == 400
        commit.assert_not_called()
    assert reload_config(state) == before


@pytest.mark.parametrize("period", [1, 90])
def test_valid_period_boundaries_report_requested_window(notification_state, period):
    state = notification_state
    response = state["client"].get(
        f"/api/v2/notifications/hooks?period_days={period}", headers=state["headers"],
    )
    assert response.status_code == 200
    assert response.get_json()["delivery_status"]["period_days"] == period


@pytest.mark.parametrize("body", [None, [], {"notification_settings": {"email": "yes"}}, {"owner_phone": 1}, {"unrecognized": True}])
def test_invalid_legacy_put_cannot_mutate_configuration(notification_state, body):
    state = notification_state
    before = reload_config(state)
    with patch.object(db.session, "commit") as commit:
        response = state["client"].put(
            state["legacy_path"], headers=state["headers"], data=json.dumps(body), content_type="application/json",
        )
        assert response.status_code == 400
        commit.assert_not_called()
    assert reload_config(state) == before


def test_valid_partial_update_preserves_false_empty_null_and_unknown_json(notification_state):
    state = notification_state
    response = request_v2(state, "post", json={
        "preferences": {"email": False, "future_channel": {"enabled": True, "options": [1, "json"]}},
        "delivery": {"owner_phone": "", "telegram_chat_id": None},
        "triggers": [{"event": "ticket.created", "channel": "email", "future_trigger": True}],
    })
    assert response.status_code == 200
    db.session.remove()
    dto = request_v2(state).get_json()
    assert dto["preferences"] == {"email": False, "future_channel": {"enabled": True, "options": [1, "json"]}}
    assert dto["delivery"] == {"owner_phone": "", "telegram_chat_id": None}
    assert dto["triggers"] == [{"event": "ticket.created", "channel": "email", "future_trigger": True}]
