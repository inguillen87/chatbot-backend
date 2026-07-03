from models import User
from utils.admin_decorators import super_admin_required
from utils.permissions import require_role


def test_require_role_rejects_non_allowlisted_superadmin_when_email_guard_is_configured(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    @require_role("super_admin")
    def protected(current_user):
        return {"ok": True}

    with client.application.test_request_context("/api/test-superadmin"):
        response, status = protected(User(email="legacy-super@chatboc.test", rol="super_admin"))

    assert status == 403
    assert response.get_json()["reason_code"] == "superadmin_email_not_authorized"


def test_require_role_accepts_allowlisted_superadmin_email(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    @require_role("super_admin")
    def protected(current_user):
        return {"ok": True}

    with client.application.test_request_context("/api/test-superadmin"):
        response = protected(User(email="guillen.marce@gmail.com", rol="super_admin"))

    assert response == {"ok": True}


def test_super_admin_required_rejects_non_allowlisted_superadmin(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    @super_admin_required
    def protected(current_user):
        return {"ok": True}

    with client.application.test_request_context("/api/test-superadmin"):
        response, status = protected(User(email="legacy-super@chatboc.test", rol="super_admin"))

    assert status == 403
    assert "Requires Super Admin privileges" in response.get_json()["error"]
