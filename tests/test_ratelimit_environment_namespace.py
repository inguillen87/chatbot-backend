"""Environment namespace propagation and real limiter counter isolation."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from flask import Flask
from flask_limiter import Limiter
from limits.storage import MemoryStorage
import pytest

ROOT = Path(__file__).resolve().parents[1]


def configured_prefix(value):
    # Fresh config import, with no inherited provider credentials or .env file.
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    environment.update({"PYTHON_DOTENV_DISABLED": "1", "FLASK_SKIP_GLOBAL_APP": "1",
                        "TESTING": "1", "ENV": "dev", "DATABASE_URL": "sqlite:///:memory:"})
    if value is not None:
        environment["RATELIMIT_KEY_PREFIX"] = value
    result = subprocess.run([sys.executable, "-c",
                             "import config,json; print(json.dumps(config.Config.RATELIMIT_KEY_PREFIX))"],
                            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, "isolated config import failed"
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize(("value", "expected"), [(None, ""),
    (" chatboc-preview-neon-test ", "chatboc-preview-neon-test"),
    ("chatboc-production-test", "chatboc-production-test")])
def test_config_reads_namespace_without_changing_existing_default(value, expected):
    assert configured_prefix(value) == expected


def test_shared_storage_limits_coordinate_within_preview_and_do_not_debit_production():
    shared_storage = MemoryStorage()

    def app_for(prefix):
        app = Flask(__name__)
        app.config.update(RATELIMIT_KEY_PREFIX=prefix, RATELIMIT_STORAGE_URI="memory://",
                          RATELIMIT_DEFAULT="1 per minute")
        with patch(Limiter.__module__ + ".storage_from_string", return_value=shared_storage):
            Limiter(key_func=lambda: "same-test-actor", app=app)
        app.add_url_rule("/limit", "limit", lambda: "ok")
        return app.test_client()

    preview_prefix = configured_prefix("chatboc-preview-neon-test")
    production_prefix = configured_prefix("chatboc-production-test")
    preview = app_for(preview_prefix)
    production = app_for(production_prefix)
    assert preview.get("/limit").status_code == 200
    assert preview.get("/limit").status_code == 429
    assert app_for(preview_prefix).get("/limit").status_code == 429
    assert production.get("/limit").status_code == 200
    assert production.get("/limit").status_code == 429
