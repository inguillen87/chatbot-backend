import os
from pathlib import Path
import subprocess
import sys


def _run_import_probe(
    code: str,
    *,
    flask_env: str = "production",
    timeout: int = 30,
    disable_spacy: bool = True,
):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["FLASK_SKIP_GLOBAL_APP"] = "1"
    env["FLASK_ENV"] = flask_env
    if disable_spacy:
        env["TESTING"] = "1"
        env["CHATBOC_DISABLE_SPACY"] = "1"
    else:
        env.pop("TESTING", None)
        env.pop("CHATBOC_DISABLE_SPACY", None)
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


def test_app_factory_keeps_municipal_conversation_stack_lazy():
    probe = _run_import_probe(
        "import sys; from app import create_app; from config import TestingConfig; "
        "app = create_app(TestingConfig); assert app.testing; "
        "assert 'services.municipio_responder' not in sys.modules",
        flask_env="testing",
        timeout=45,
    )

    assert probe.returncode == 0, probe.stderr


def test_whatsapp_and_tramites_routes_keep_municipal_conversation_stack_lazy():
    probe = _run_import_probe(
        "import sys; import routes.whatsapp_webhook; import routes.tramites; "
        "assert 'services.municipio_responder' not in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_tramites_service_loads_municipal_catalog_only_on_first_use():
    probe = _run_import_probe(
        "import sys; from types import ModuleType; import services.tramites as tramites; "
        "assert 'services.municipio_responder' not in sys.modules; "
        "fake = ModuleType('services.municipio_responder'); "
        "fake.get_tramites_info = lambda: {"
        "'Licencia': {'descripcion': 'Renovar', 'botones': [{'texto': 'Ver'}]}, "
        "'Partida': {'descripcion': 'Solicitar', 'botones': []}}; "
        "sys.modules['services.municipio_responder'] = fake; "
        "result = tramites.buscar_tramites('lic'); "
        "assert result == [{'nombre': 'Licencia', 'descripcion': 'Renovar', "
        "'botones': [{'texto': 'Ver'}]}]"
    )

    assert probe.returncode == 0, probe.stderr


def test_spacy_loader_module_does_not_import_spacy_until_first_use():
    probe = _run_import_probe(
        "import sys; import services.spacy_loader; "
        "assert 'spacy' not in sys.modules",
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_spacy_loader_initializes_on_first_use_with_safe_fallback():
    probe = _run_import_probe(
        "import sys; from services.spacy_loader import get_spacy_model; "
        "assert 'spacy' not in sys.modules; "
        "nlp = get_spacy_model(); assert nlp is not None; "
        "assert getattr(nlp, 'lang', None) == 'es'; "
        "assert 'spacy' in sys.modules",
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_document_ai_import_keeps_spacy_pipeline_lazy():
    probe = _run_import_probe(
        "import sys; import services.google_docai as google_docai; "
        "assert google_docai.NLP_SPACY is None; "
        "assert 'spacy' not in sys.modules",
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_app_factory_keeps_document_ai_and_spacy_out_of_startup():
    probe = _run_import_probe(
        "import sys; from app import create_app; from config import TestingConfig; "
        "app = create_app(TestingConfig); assert app.testing; "
        "assert 'services.google_docai' not in sys.modules; "
        "assert 'spacy' not in sys.modules",
        flask_env="testing",
        timeout=45,
        disable_spacy=False,
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
