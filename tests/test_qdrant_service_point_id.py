import importlib
import sys
import types


def _load_service_module():
    fake_qdrant_search = types.ModuleType("services.qdrant_search")
    fake_qdrant_search.coleccion_catalogo_para_rubro = lambda rubro: "catalogo_pyme"
    sys.modules.setdefault("services.qdrant_search", fake_qdrant_search)

    return importlib.import_module("services.qdrant_service")


def test_normalize_qdrant_point_id_with_int():
    mod = _load_service_module()
    assert mod._normalize_qdrant_point_id(132, "tenant-1") == 132


def test_normalize_qdrant_point_id_with_numeric_string():
    mod = _load_service_module()
    assert mod._normalize_qdrant_point_id("133", "tenant-1") == 133


def test_normalize_qdrant_point_id_with_non_numeric_string_returns_uuid():
    mod = _load_service_module()
    point_id = mod._normalize_qdrant_point_id("catalog-item-abc", "tenant-1")
    assert isinstance(point_id, str)
    assert len(point_id) == 36


def test_normalize_qdrant_point_id_fallback_for_missing_id_is_stable_per_tenant():
    mod = _load_service_module()
    first = mod._normalize_qdrant_point_id(None, "tenant-1")
    second = mod._normalize_qdrant_point_id(None, "tenant-1")
    other_tenant = mod._normalize_qdrant_point_id(None, "tenant-2")

    assert first == second
    assert first != other_tenant
