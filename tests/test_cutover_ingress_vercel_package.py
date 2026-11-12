from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from cutover_ingress.app import _runtime_source_sha256, create_cutover_ingress_app
from cutover_ingress.core import CutoverIngressSettings
from cutover_ingress.migration import migrate_cutover_ingress


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "cutover_ingress"
RUNTIME_MODULES = {
    "__init__.py",
    "app.py",
    "core.py",
    "migration.py",
    "schema.py",
    "twilio_signature.py",
    "wsgi.py",
}
PACKAGING_FILES = {
    ".dockerignore",
    "Dockerfile.vercel",
    "requirements.txt",
    "vercel.json",
}


def _active_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _settings(database_url: str) -> CutoverIngressSettings:
    return CutoverIngressSettings(
        database_url=database_url,
        public_webhook_url="https://ingress.example.test/webhook/whatsapp",
        twilio_auth_token="isolated-ingress-auth-token",
        twilio_account_sid="AC" + "a" * 32,
        expected_to="whatsapp:+17432643718",
        tenant_id=17,
        stream_hash_secret=b"s" * 32,
        active_encryption_key_id="active",
        encryption_keys={"active": b"e" * 32},
        envelope_hmac_key=b"h" * 32,
    )


def test_docker_context_is_deny_first_and_allowlists_only_ingress_files() -> None:
    patterns = _active_lines(PACKAGE / ".dockerignore")

    assert patterns[0] == "*"
    assert set(patterns[1:]) == {
        f"!{name}" for name in RUNTIME_MODULES | PACKAGING_FILES
    }


def test_vercel_link_metadata_and_downloaded_environment_are_never_committed() -> None:
    gitignore = set(_active_lines(PACKAGE / ".gitignore"))

    assert ".vercel" in gitignore
    assert ".env*" in gitignore


def test_dockerfile_copies_only_the_isolated_runtime_and_uses_correct_wsgi() -> None:
    dockerfile = (PACKAGE / "Dockerfile.vercel").read_text(encoding="utf-8")
    active = "\n".join(_active_lines(PACKAGE / "Dockerfile.vercel"))

    assert "COPY . ." not in active
    assert "COPY .." not in active
    assert "COPY requirements.txt /app/requirements.txt" in active
    assert (
        "COPY __init__.py app.py core.py migration.py schema.py twilio_signature.py wsgi.py "
        "/app/cutover_ingress/"
    ) in active
    assert "cutover_ingress.wsgi:app" in dockerfile
    assert "--workers 1" in dockerfile
    assert "USER ingress" in dockerfile


def test_ingress_runtime_has_no_top_level_backend_ai_or_outbound_provider_imports() -> None:
    forbidden_roots = {
        "app",
        "anthropic",
        "celery",
        "cohere",
        "database",
        "google",
        "models",
        "openai",
        "redis",
        "requests",
        "routes",
        "services",
        "twilio",
    }
    forbidden_modules: list[str] = []
    for filename in RUNTIME_MODULES:
        tree = ast.parse((PACKAGE / filename).read_text(encoding="utf-8"))
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                modules = [alias.name for alias in statement.names]
            elif isinstance(statement, ast.ImportFrom) and statement.level == 0:
                modules = [statement.module or ""]
            else:
                continue
            forbidden_modules.extend(
                module
                for module in modules
                if module.split(".", 1)[0] in forbidden_roots
            )

    assert forbidden_modules == []


def test_ingress_http_import_loads_no_backend_ai_or_provider_sdk() -> None:
    blocked = {
        "app",
        "cohere",
        "database",
        "models",
        "openai",
        "routes",
        "services",
        "twilio",
    }
    script = (
        "import json, sys; import cutover_ingress.app; "
        f"blocked={sorted(blocked)!r}; "
        "loaded=sorted(name for name in blocked if name in sys.modules); "
        "print(json.dumps(loaded)); raise SystemExit(bool(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == []


def test_vercel_manifest_registers_no_crons_or_application_routes() -> None:
    manifest = json.loads((PACKAGE / "vercel.json").read_text(encoding="utf-8"))

    assert manifest == {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "framework": "container",
        "fluid": True,
        "regions": ["gru1"],
    }
    assert "crons" not in manifest
    assert "rewrites" not in manifest
    assert "routes" not in manifest


def test_runtime_dependencies_are_minimal_and_exclude_outbound_or_ai_clients() -> None:
    dependencies = {
        line.split("==", 1)[0]
        for line in _active_lines(PACKAGE / "requirements.txt")
    }

    assert dependencies == {
        "Flask",
        "SQLAlchemy",
        "cryptography",
        "gunicorn",
        "psycopg[binary]",
    }
    assert dependencies.isdisjoint(
        {
            "anthropic",
            "celery",
            "cohere",
            "google-cloud-aiplatform",
            "openai",
            "redis",
            "requests",
        }
    )


def test_health_contract_is_buffer_only_and_never_enables_providers(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'ingress-health.sqlite3').as_posix()}"
    engine = create_engine(database_url, future=True)
    migrate_cutover_ingress(engine)
    app = create_cutover_ingress_app(_settings(database_url), engine=engine)
    try:
        response = app.test_client().get("/health")
        assert response.status_code == 200
        assert response.get_json() == {
            "contract_version": "chatboc.cutover_ingress.health.v1",
            "database": "reachable",
            "database_check_ttl_seconds": 5,
            "mode": "buffer_only",
            "providers_enabled": False,
            "source_sha256": _runtime_source_sha256(),
            "status": "buffer_ready",
        }
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        engine.dispose()


def test_health_reuses_a_bounded_database_check_instead_of_amplifying_connections(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'ingress-health-cache.sqlite3').as_posix()}"
    engine = create_engine(database_url, future=True)
    migrate_cutover_ingress(engine)
    app = create_cutover_ingress_app(_settings(database_url), engine=engine)
    try:
        with patch.object(engine, "connect", wraps=engine.connect) as connect:
            with app.test_client() as client:
                assert client.get("/health").status_code == 200
                assert client.get("/health").status_code == 200
        assert connect.call_count == 1
    finally:
        engine.dispose()


def test_postgres_engine_delegates_pooling_to_neon_without_per_instance_pool() -> None:
    settings = _settings(
        "postgresql+psycopg://ingress:secret@ep-direct.example.test/"
        "cutover_ingress?sslmode=require"
    )
    sentinel = object()
    with patch("cutover_ingress.core.create_engine", return_value=sentinel) as factory:
        assert settings.build_engine() is sentinel

    _, options = factory.call_args
    assert options == {
        "future": True,
        "pool_pre_ping": False,
        "poolclass": NullPool,
    }
