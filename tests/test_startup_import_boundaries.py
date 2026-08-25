import os
from pathlib import Path
import subprocess
import sys


def _run_import_probe(code: str, *, flask_env: str = "production", timeout: int = 30):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["FLASK_SKIP_GLOBAL_APP"] = "1"
    env["FLASK_ENV"] = flask_env
    env["TESTING"] = "1"
    env["CHATBOC_DISABLE_SPACY"] = "1"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["SECRET_KEY"] = "startup-boundary-test-only-secret-key-32chars"
    env.pop("VERCEL", None)
    env.pop("VERCEL_ENV", None)

    return subprocess.run(
        [
            sys.executable,
            "-c",
            code,
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_socket_startup_keeps_chat_and_ticket_stacks_lazy():
    probe = _run_import_probe(
        "import sys; import socket_service; "
        "assert 'services.ticket_service' not in sys.modules; "
        "assert 'services.logic' not in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_rubro_classification_is_a_dependency_free_leaf():
    probe = _run_import_probe(
        "import sys; from types import SimpleNamespace; "
        "from services.rubro_classification import ("
        "RUBROS_PUBLICOS, es_rubro_publico, normalizar_rubro); "
        "assert 'municipio' in RUBROS_PUBLICOS; "
        "assert normalizar_rubro(SimpleNamespace(clave=' Gobierno ')) == 'gobierno'; "
        "assert es_rubro_publico(SimpleNamespace(nombre='Municipalidad')); "
        "assert not es_rubro_publico('pyme'); "
        "assert 'services.logic' not in sys.modules; "
        "assert 'services.llm_bridge' not in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_auth_import_keeps_chat_and_commerce_stacks_lazy():
    probe = _run_import_probe(
        "import sys; import routes.auth; "
        "assert 'services.logic' not in sys.modules; "
        "assert 'services.pymes' not in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_app_factory_does_not_eagerly_import_conversation_logic():
    probe = _run_import_probe(
        "import sys; from app import create_app; from config import TestingConfig; "
        "app = create_app(TestingConfig); assert app.testing; "
        "assert 'services.logic' not in sys.modules",
        flask_env="testing",
        timeout=45,
    )

    assert probe.returncode == 0, probe.stderr


def test_logic_keeps_legacy_rubro_exports():
    probe = _run_import_probe(
        "from services import logic; from services import rubro_classification as leaf; "
        "assert logic.RUBROS_PUBLICOS is leaf.RUBROS_PUBLICOS; "
        "assert logic.normalizar_rubro is leaf.normalizar_rubro; "
        "assert logic.es_rubro_publico is leaf.es_rubro_publico"
    )

    assert probe.returncode == 0, probe.stderr
