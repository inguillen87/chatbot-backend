"""Exercise model initialization in fresh interpreters, without app preloads."""

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_IMPORT_PROBE = textwrap.dedent(
    """
    import importlib
    import sys

    sys.path.insert(0, sys.argv[1])
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()

    importlib.import_module(sys.argv[2])
    import models
    import models_territorial_geocoding as territorial
    from sqlalchemy.orm import configure_mappers

    configure_mappers()
    assert territorial.db is models.db
    assert territorial.JSONType is models.JSONType
    assert territorial.TimestampMixin is models.TimestampMixin
    for name in (
        "TerritorialGeocodingAttempt",
        "TerritorialGeocodingJob",
        "TerritorialGeocodingReview",
        "TerritorialGeocodingSyncReceipt",
    ):
        model = getattr(territorial, name)
        assert getattr(models, name) is model
        table = model.__table__
        assert models.db.metadata.tables[table.name] is table
        assert sum(
            mapper.local_table is table for mapper in models.db.Model.registry.mappers
        ) == 1
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.metadata is models.db.metadata
    """
)


@pytest.mark.parametrize("first_module", ["models", "models_territorial_geocoding"])
def test_territorial_model_import_order_preserves_one_complete_registry(
    first_module, tmp_path
):
    result = subprocess.run(
        [
            sys.executable, "-I", "-B", "-X", "utf8", "-c",
            _IMPORT_PROBE, str(_ROOT), first_module,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
