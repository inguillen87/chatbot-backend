import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_render_zoho_aliases_feed_canonical_email_configuration():
    env = dict(os.environ)
    for key in (
        "SMTP_HOST",
        "MAIL_SERVER",
        "MAIL_HOST",
        "SMTP_PORT",
        "MAIL_PORT",
        "SMTP_USER",
        "SMTP_USERNAME",
        "MAIL_USERNAME",
        "MAIL_USER",
        "MAIL_FROM_ADDRESS",
        "SMTP_PASSWORD",
        "SMTP_PASS",
        "MAIL_PASSWORD",
        "ENABLE_EMAIL_NOTIFICATIONS",
    ):
        env.pop(key, None)
    env.update(
        {
            "ZOHO_SMTP_HOST": "smtp.alias.test",
            "ZOHO_SMTP_PORT": "2525",
            "ZOHO_SMTP_USER": "mailer@alias.test",
            "ZOHO_SMTP_PASSWORD": "test-password",
            "AUTH_EMAIL_FROM": "notices@alias.test",
            "EMAIL_NOTIFICATIONS_ENABLED": "true",
            "SMTP_REQUIRE_AUTH": "true",
        }
    )
    code = (
        "import json; from config import Config; "
        "print(json.dumps({"
        "'host': Config.SMTP_HOST, 'port': Config.SMTP_PORT, "
        "'user': Config.SMTP_USER, 'password': Config.SMTP_PASSWORD, "
        "'from': Config.MAIL_FROM_ADDRESS, "
        "'enabled': Config.EMAIL_NOTIFICATIONS_ENABLED, "
        "'require_auth': Config.SMTP_REQUIRE_AUTH}))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(completed.stdout.strip())

    assert payload == {
        "host": "smtp.alias.test",
        "port": 2525,
        "user": "mailer@alias.test",
        "password": "test-password",
        "from": "notices@alias.test",
        "enabled": True,
        "require_auth": True,
    }
