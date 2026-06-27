from datetime import datetime, timezone

from database import db
from models import User
from services.auth_notification_service import build_email_verification_url, send_verification_email


class FakeSMTP:
    sent_messages = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        return None

    def login(self, user, password):
        self.logged_in = True
        self.user = user
        self.password = password

    def send_message(self, msg):
        self.sent_messages.append(msg)


def test_build_email_verification_url_uses_app_base_url(client, monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://www.chatboc.ar")
    with client.application.app_context():
        user = User(
            name="Tester",
            email="tester@chatboc.test",
            email_verification_token="verify-token",
            email_verification_sent_at=datetime.now(timezone.utc),
        )

        assert build_email_verification_url(user) == "https://www.chatboc.ar/auth/verify-email?token=verify-token"


def test_send_verification_email_uses_zoho_smtp_without_logging_token(client, monkeypatch):
    FakeSMTP.sent_messages = []
    monkeypatch.setenv("ZOHO_SMTP_USER", "info@chatboc.ar")
    monkeypatch.setenv("ZOHO_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("APP_BASE_URL", "https://www.chatboc.ar")
    monkeypatch.setattr("services.auth_notification_service.smtplib.SMTP", FakeSMTP)

    with client.application.app_context():
        user = User(
            name="Tester",
            email="tester@chatboc.test",
            email_verification_token="verify-token",
            email_verification_sent_at=datetime.now(timezone.utc),
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.commit()

        assert send_verification_email(user) is True
        assert len(FakeSMTP.sent_messages) == 1
        msg = FakeSMTP.sent_messages[0]
        assert msg["From"] == "info@chatboc.ar"
        assert msg["To"] == "tester@chatboc.test"
        assert "Verifica tu cuenta" in msg["Subject"]
