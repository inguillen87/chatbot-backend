from types import SimpleNamespace

from services import encuestas_service


class _QueryStub:
    def __init__(self, rows):
        self._rows = list(rows)

    def options(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *_):
        return self

    def all(self):
        return list(self._rows)


class _DummyColumn:
    def desc(self):  # pragma: no cover - helper
        return self


class _DummyComparator:
    def __eq__(self, *_):  # pragma: no cover - helper
        return self


class _FakeEncuesta:
    tenant_id = _DummyComparator()
    estado = _DummyComparator()
    created_at = _DummyColumn()
    id = _DummyColumn()
    links = None

    def __init__(self, slug: str, public_slug: str, *, active: bool):
        self.slug = slug
        self.estado = "publicada"
        self.links = [SimpleNamespace(slug_publico=public_slug, id=1)]
        self._active = active

    def esta_activa(self, at=None):  # pragma: no cover - helper
        return self._active


def test_list_public_encuestas_respects_model_schedule(monkeypatch):
    """The public listing must reuse EncEncuesta.esta_activa logic."""

    monkeypatch.setattr(
        encuestas_service,
        "_bootstrap_sample_if_needed",
        lambda tenant_id: None,
        raising=False,
    )

    active = _FakeEncuesta("activa", "52e3e4", active=True)
    inactive = _FakeEncuesta("inactiva", "caduca", active=False)
    _FakeEncuesta.query = _QueryStub([inactive, active])

    monkeypatch.setattr(encuestas_service, "EncEncuesta", _FakeEncuesta, raising=False)
    monkeypatch.setattr(encuestas_service, "joinedload", lambda *_, **__: None, raising=False)

    resultados = encuestas_service.list_public_encuestas_for_tenant(
        tenant_id=7, limit=5
    )

    assert resultados == [(active, "52e3e4")]


def test_bootstrap_sample_is_disabled_by_default(client, monkeypatch):
    client.application.config["ENABLE_DEMO_MODE"] = False
    client.application.config["ALLOW_SURVEY_DEMO_SEEDING"] = False

    monkeypatch.setattr(encuestas_service, "_BOOTSTRAP_SAMPLE_ENABLED", False)
    monkeypatch.setattr(
        encuestas_service,
        "ensure_enc_encuesta_schema",
        lambda *_args, **_kwargs: None,
    )

    def fail_profile(_tenant_id):
        raise AssertionError("bootstrap profiles must not be inspected in production defaults")

    monkeypatch.setattr(encuestas_service, "_match_bootstrap_profile", fail_profile)

    encuestas_service._bootstrap_sample_if_needed(tenant_id=4)
