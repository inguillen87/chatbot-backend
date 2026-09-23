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


def test_app_factory_keeps_optional_provider_sdks_out_of_startup():
    probe = _run_import_probe(
        "import sys; from app import create_app; from config import TestingConfig; "
        "app = create_app(TestingConfig); assert app.testing; "
        "targets = ('openai', 'pandas', 'qdrant_client', 'cohere', "
        "'google.cloud.documentai', 'google.cloud.documentai_v1', "
        "'google.cloud.vision'); "
        "loaded = lambda target: any(name == target or name.startswith(target + '.') "
        "for name in sys.modules); "
        "assert all(not loaded(target) for target in targets)",
        flask_env="testing",
        timeout=45,
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_app_factory_keeps_storage_and_document_processors_out_of_startup():
    probe = _run_import_probe(
        "import os, sys; os.environ.update({"
        "'R2_ENDPOINT_URL': 'https://r2.example.test', "
        "'R2_ACCESS_KEY_ID': 'test-access', "
        "'R2_SECRET_ACCESS_KEY': 'test-secret', "
        "'R2_BUCKET_NAME': 'test-bucket', "
        "'CLOUDINARY_CLOUD_NAME': 'test-cloud', "
        "'CLOUDINARY_API_KEY': 'test-key', "
        "'CLOUDINARY_API_SECRET': 'test-secret', "
        "'GCS_ENABLED': 'true'}); "
        "from app import create_app; from config import TestingConfig; "
        "app = create_app(TestingConfig); assert app.testing; "
        "targets = ('boto3', 'cloudinary', 'google.cloud.storage', 'fitz', "
        "'pdfplumber', 'docx', 'bs4'); "
        "loaded = lambda target: any(name == target or name.startswith(target + '.') "
        "for name in sys.modules); "
        "assert all(not loaded(target) for target in targets); "
        "assert 'services.thumbnail_service' not in sys.modules; "
        "assert 'services.analisis_archivo_service' not in sys.modules; "
        "assert 'services.scraper_avanzado' not in sys.modules",
        flask_env="testing",
        timeout=45,
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_app_factory_keeps_passkey_and_government_analytics_stacks_lazy():
    probe = _run_import_probe(
        "import sys; from app import create_app; from config import TestingConfig; "
        "app = create_app(TestingConfig); assert app.testing; "
        "targets = ('webauthn', 'asn1crypto', 'services.government_pipeline', "
        "'numpy'); "
        "loaded = lambda target: any(name == target or name.startswith(target + '.') "
        "for name in sys.modules); "
        "assert all(not loaded(target) for target in targets)",
        flask_env="testing",
        timeout=45,
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_upload_processor_defers_optional_processing_stack_until_first_use():
    probe = _run_import_probe(
        "import sys; import services.upload_processor; "
        "targets = ('openai', 'pandas', 'qdrant_client', 'cohere', "
        "'google.cloud.documentai', 'google.cloud.documentai_v1', "
        "'google.cloud.vision'); "
        "loaded = lambda target: any(name == target or name.startswith(target + '.') "
        "for name in sys.modules); "
        "assert all(not loaded(target) for target in targets); "
        "assert 'services.document_processing_service' not in sys.modules; "
        "assert 'services.vision_fallback_service' not in sys.modules; "
        "assert 'services.catalog.registry' not in sys.modules; "
        "assert 'services.qdrant_search' not in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_lazy_module_loads_dependency_on_first_attribute_use():
    probe = _run_import_probe(
        "import sys; sys.modules.pop('fractions', None); "
        "from utils.lazy_module import LazyModule; "
        "fractions = LazyModule('fractions'); "
        "assert 'fractions' not in sys.modules; "
        "assert fractions.Fraction(1, 2).numerator == 1; "
        "assert 'fractions' in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_vercel_web_create_app_installs_stub_without_migration_cli_stack():
    probe = _run_import_probe(
        "import os, sys; "
        "os.environ['VERCEL'] = '1'; "
        "os.environ.pop('FLASK_RUN_FROM_CLI', None); "
        "os.environ.pop('FLASK_MIGRATIONS_ONLY', None); "
        "os.environ['DATABASE_URL'] = ('postgresql+psycopg://probe:probe@' "
        "'127.0.0.1:1/probe?sslmode=require&connect_timeout=1'); "
        "from cachelib.simple import SimpleCache; import app as app_module; "
        "from config import Config; "
        "ProbeConfig = type('VercelWebProbeConfig', (Config,), {"
        "'SQLALCHEMY_DATABASE_URI': os.environ['DATABASE_URL'], "
        "'SESSION_TYPE': 'cachelib', "
        "'SESSION_CACHELIB': SimpleCache(default_timeout=300), "
        "'SOCKETIO_MESSAGE_QUEUE_URL': '', "
        "'ENABLE_RUNTIME_SCHEMA_SYNC': False, "
        "'ENABLE_RUNTIME_TENANT_INIT': False, "
        "'SKIP_INIT_TENANTS': True}); "
        "web_app = app_module.create_app(ProbeConfig); import extensions; "
        "assert web_app.extensions['migrate'] is extensions.migrate; "
        "assert 'flask_migrate' not in sys.modules; "
        "assert 'alembic' not in sys.modules; "
        "assert type(extensions.migrate).__name__ == '_WebRuntimeMigrate'",
        timeout=45,
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_vercel_cli_flag_keeps_real_migration_extension():
    probe = _run_import_probe(
        "import os, sys; os.environ['VERCEL'] = '1'; "
        "os.environ['FLASK_RUN_FROM_CLI'] = 'true'; import extensions; "
        "assert 'flask_migrate' in sys.modules; "
        "assert type(extensions.migrate).__name__ == 'Migrate'"
    )

    assert probe.returncode == 0, probe.stderr


def test_vercel_migrations_only_create_app_registers_real_cli_without_db():
    probe = _run_import_probe(
        "import os, sys; os.environ['VERCEL'] = '1'; "
        "os.environ['FLASK_MIGRATIONS_ONLY'] = '1'; "
        "os.environ.pop('FLASK_RUN_FROM_CLI', None); "
        "os.environ['DATABASE_URL'] = ('postgresql+psycopg://probe:probe@' "
        "'127.0.0.1:1/probe?sslmode=require&connect_timeout=1'); "
        "import app as app_module; from config import Config; "
        "ProbeConfig = type('VercelMigrationsProbeConfig', (Config,), {"
        "'SQLALCHEMY_DATABASE_URI': os.environ['DATABASE_URL'], "
        "'ENABLE_RUNTIME_SCHEMA_SYNC': False, "
        "'ENABLE_RUNTIME_TENANT_INIT': False, "
        "'SKIP_INIT_TENANTS': True}); "
        "cli_app = app_module.create_app(ProbeConfig); import extensions; "
        "assert 'flask_migrate' in sys.modules; "
        "assert 'alembic' in sys.modules; "
        "assert type(extensions.migrate).__name__ == 'Migrate'; "
        "assert cli_app.extensions['migrate'].db is extensions.db; "
        "assert 'db' in cli_app.cli.commands",
        timeout=45,
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_app_factory_keeps_outbound_http_provider_modules_lazy():
    probe = _run_import_probe(
        "import sys; from app import create_app; from config import TestingConfig; "
        "app = create_app(TestingConfig); assert app.testing; "
        "assert 'httpx' not in sys.modules; "
        "assert 'google.oauth2.id_token' not in sys.modules; "
        "assert 'google.auth.transport.requests' not in sys.modules",
        flask_env="testing",
        timeout=45,
        disable_spacy=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_llm_utils_legacy_document_export_loads_document_ai_only_on_demand():
    probe = _run_import_probe(
        "import sys; import services.llm_utils as llm_utils; "
        "loaded = lambda target: any(name == target or name.startswith(target + '.') "
        "for name in sys.modules); "
        "assert not loaded('google.cloud.documentai'); "
        "assert not loaded('google.cloud.documentai_v1'); "
        "from services.llm_utils import Document; "
        "assert Document is llm_utils.documentai.Document; "
        "assert loaded('google.cloud.documentai'); "
        "assert loaded('google.cloud.documentai_v1')"
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
