import os
from pathlib import Path
import subprocess
import sys


def _run_import_probe(code: str, *, timeout: int = 45):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "FLASK_SKIP_GLOBAL_APP": "1",
            "FLASK_ENV": "testing",
            "TESTING": "1",
            "CHATBOC_DISABLE_SPACY": "1",
            "DATABASE_URL": "sqlite:///:memory:",
            "SECRET_KEY": "startup-boundary-test-only-secret-key-32chars",
        }
    )
    env.pop("VERCEL", None)
    env.pop("VERCEL_ENV", None)

    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_app_factory_keeps_optional_stacks_out_of_startup():
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
        "targets = ('openai', 'pandas', 'qdrant_client', 'cohere', 'boto3', "
        "'cloudinary', 'pdfplumber', 'fitz', 'webauthn', 'asn1crypto', "
        "'google.cloud.storage', 'google.cloud.documentai', "
        "'google.cloud.documentai_v1', 'google.cloud.vision', "
        "'services.government_pipeline', 'numpy'); "
        "loaded = lambda target: any(name == target or name.startswith(target + '.') "
        "for name in sys.modules); "
        "assert all(not loaded(target) for target in targets), "
        "[target for target in targets if loaded(target)]; "
        "assert 'services.thumbnail_service' not in sys.modules; "
        "assert 'services.analisis_archivo_service' not in sys.modules; "
        "assert 'services.scraper_avanzado' not in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_auth_and_routes_keep_conversation_stacks_lazy():
    probe = _run_import_probe(
        "import sys; import routes.auth; import routes.whatsapp_webhook; "
        "import routes.tramites; "
        "assert 'services.logic' not in sys.modules; "
        "assert 'services.pymes' not in sys.modules; "
        "assert 'services.municipio_responder' not in sys.modules"
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


def test_logic_keeps_legacy_rubro_exports():
    probe = _run_import_probe(
        "from services import logic; "
        "from services import rubro_classification as leaf; "
        "assert logic.RUBROS_PUBLICOS is leaf.RUBROS_PUBLICOS; "
        "assert logic.normalizar_rubro is leaf.normalizar_rubro; "
        "assert logic.es_rubro_publico is leaf.es_rubro_publico"
    )

    assert probe.returncode == 0, probe.stderr


def test_upload_processor_defers_optional_processing_stack():
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


def test_r2_client_initializes_only_on_first_object_operation():
    probe = _run_import_probe(
        "import os, sys; os.environ.update({"
        "'R2_ENDPOINT_URL': 'https://r2.example.test', "
        "'R2_ACCESS_KEY_ID': 'test-access', "
        "'R2_SECRET_ACCESS_KEY': 'test-secret', "
        "'R2_BUCKET_NAME': 'test-bucket'}); "
        "from services.r2_service import R2Service; "
        "assert 'boto3' not in sys.modules; service = R2Service(); "
        "sentinel = object(); calls = []; "
        "service._create_client = lambda: (calls.append(1) or sentinel); "
        "assert service.client is None; assert service._get_client() is sentinel; "
        "assert service._get_client() is sentinel; assert calls == [1]; "
        "assert 'boto3' not in sys.modules"
    )

    assert probe.returncode == 0, probe.stderr


def test_llm_utils_legacy_document_export_stays_on_demand():
    probe = _run_import_probe(
        "import sys; import services.llm_utils as llm_utils; "
        "loaded = lambda target: any(name == target or name.startswith(target + '.') "
        "for name in sys.modules); "
        "assert not loaded('google.cloud.documentai'); "
        "from services.llm_utils import Document; "
        "assert Document is llm_utils.documentai.Document; "
        "assert loaded('google.cloud.documentai')"
    )

    assert probe.returncode == 0, probe.stderr
