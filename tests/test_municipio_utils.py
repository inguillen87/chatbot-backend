from types import SimpleNamespace

from utils.municipio_utils import resolve_municipio_identifier


def test_resolve_identifier_prefers_explicit_municipio_id():
    owner = SimpleNamespace(municipio_id=7, tipo_chat="municipio", id=4, empresa_id=2)
    assert resolve_municipio_identifier(owner, "default") == 7


def test_resolve_identifier_falls_back_to_owner_id_for_municipios():
    owner = SimpleNamespace(municipio_id=None, tipo_chat="municipio", id=5, empresa_id=None)
    assert resolve_municipio_identifier(owner, "default") == 5


def test_resolve_identifier_uses_empresa_id_for_employees():
    owner = SimpleNamespace(municipio_id=None, tipo_chat="empleado", id=8, empresa_id=3)
    assert resolve_municipio_identifier(owner, "default") == 3


def test_resolve_identifier_returns_fallback_when_missing():
    owner = SimpleNamespace(municipio_id=None, tipo_chat="empleado", id=None, empresa_id=None)
    assert resolve_municipio_identifier(owner, "default") == "default"
