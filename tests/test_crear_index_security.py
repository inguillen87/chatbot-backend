import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest
import qdrant_client


MODULE_NAME = "services.crear_index"


def test_import_does_not_construct_client_or_mutate_qdrant():
    sys.modules.pop(MODULE_NAME, None)

    with patch.object(qdrant_client, "QdrantClient") as constructor:
        module = importlib.import_module(MODULE_NAME)

    constructor.assert_not_called()
    assert callable(module.create_catalog_user_id_index)


def test_explicit_index_creation_uses_supplied_client_without_network():
    module = importlib.import_module(MODULE_NAME)
    client = MagicMock()

    module.create_catalog_user_id_index(client)

    client.create_payload_index.assert_called_once_with(
        collection_name="catalogos",
        field_name="user_id",
        field_schema="integer",
    )


def test_client_factory_fails_closed_without_environment(monkeypatch):
    module = importlib.import_module(MODULE_NAME)
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)

    with (
        patch.object(module, "QdrantClient") as constructor,
        pytest.raises(RuntimeError, match="QDRANT_URL"),
    ):
        module.create_catalog_user_id_index()

    constructor.assert_not_called()
