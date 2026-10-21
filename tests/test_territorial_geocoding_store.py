from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

from models_territorial_geocoding import (
    TerritorialGeocodingAttempt,
    TerritorialGeocodingJob,
)
from services.territorial_geocoding import (
    build_territorial_geocoding_candidate,
    process_territorial_geocoding_candidate,
)
from services.territorial_geocoding_store import (
    SQLAlchemyTerritorialGeocodingAuditStore,
    build_ticket_coordinate_applier,
)


def _candidate():
    candidate = build_territorial_geocoding_candidate(
        {
            "source": "municipio_ticket",
            "id": 419,
            "address": "San Martin 250, Junin",
            "category": "luminarias",
        },
        tenant_id=7,
        tenant_slug="junin",
        jurisdiction={
            "state": "configured",
            "enforced": True,
            "city": "Junin",
            "state_name": "Mendoza",
            "country": "AR",
            "bounds": {
                "west": -68.0,
                "south": -33.2,
                "east": -67.0,
                "north": -32.6,
            },
        },
    )
    assert candidate is not None
    return candidate


def _provider(address, context):
    return {
        "place_id": "place-419",
        "geometry": {
            "location": {"lat": -33.05, "lng": -67.55},
            "location_type": "ROOFTOP",
        },
        "address_components": [
            {"long_name": "Junin", "short_name": "Junin", "types": ["locality"]},
            {
                "long_name": "Mendoza",
                "short_name": "Mendoza",
                "types": ["administrative_area_level_1"],
            },
            {"long_name": "Argentina", "short_name": "AR", "types": ["country"]},
        ],
    }


def test_sqlalchemy_store_persists_safe_attempt_and_does_not_downgrade_applied(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'territorial-store.sqlite3').as_posix()}"
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE tenant_profile (id INTEGER PRIMARY KEY)")
        )
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (7)"))
    TerritorialGeocodingJob.__table__.create(engine)
    TerritorialGeocodingAttempt.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    store = SQLAlchemyTerritorialGeocodingAuditStore(session)

    applied = process_territorial_geocoding_candidate(
        _candidate(),
        geocoder=_provider,
        audit_store=store,
        idempotency_key="apply-001",
        external_calls_enabled=True,
        dry_run=False,
        writes_enabled=True,
        writer_authority_confirmed=True,
        apply_callback=lambda candidate, proposal: True,
    )
    diagnostic = process_territorial_geocoding_candidate(
        _candidate(),
        geocoder=_provider,
        audit_store=store,
        idempotency_key="diagnostic-002",
        external_calls_enabled=True,
        dry_run=True,
    )
    session.commit()

    assert applied["status"] == "applied"
    assert diagnostic["status"] == "pending"
    job = session.query(TerritorialGeocodingJob).one()
    attempts = (
        session.query(TerritorialGeocodingAttempt)
        .order_by(TerritorialGeocodingAttempt.attempt_number)
        .all()
    )
    assert job.status == "applied"
    assert job.attempt_count == 2
    assert len(attempts) == 2
    assert attempts[0].write_performed is True
    assert attempts[1].write_performed is False
    assert "San Martin 250" not in str(job.result_json)
    assert "San Martin 250" not in str([attempt.result_json for attempt in attempts])

    replay = process_territorial_geocoding_candidate(
        _candidate(),
        geocoder=lambda address, context: (_ for _ in ()).throw(
            AssertionError("provider must not run on replay")
        ),
        audit_store=store,
        idempotency_key="apply-001",
        external_calls_enabled=True,
        dry_run=False,
        writes_enabled=True,
        writer_authority_confirmed=True,
        apply_callback=lambda candidate, proposal: False,
    )
    assert replay["status"] == "applied"
    assert replay["idempotent_replay"] is True
    session.close()


class _FakeQuery:
    def __init__(self, record):
        self.record = record
        self.filters = None

    def filter_by(self, **filters):
        self.filters = filters
        return self

    def with_for_update(self):
        return self

    def one_or_none(self):
        return self.record


class _FakeSession:
    def __init__(self, record):
        self.query_object = _FakeQuery(record)
        self.flush_count = 0

    def query(self, model):
        return self.query_object

    def flush(self):
        self.flush_count += 1


def test_ticket_coordinate_applier_is_tenant_scoped_and_rejects_stale_address():
    record = SimpleNamespace(
        direccion="Otra direccion 999",
        latitud=None,
        longitud=None,
    )
    session = _FakeSession(record)
    apply = build_ticket_coordinate_applier(session)

    written = apply(_candidate(), {"lat": -33.05, "lng": -67.55})

    assert written is False
    assert session.query_object.filters == {"id": 419, "tenant_id": 7}
    assert record.latitud is None
    assert record.longitud is None
    assert session.flush_count == 0


def test_ticket_coordinate_applier_updates_matching_source_once():
    record = SimpleNamespace(
        direccion="San Martin 250, Junin",
        latitud=None,
        longitud=None,
    )
    session = _FakeSession(record)
    apply = build_ticket_coordinate_applier(session)

    assert apply(_candidate(), {"lat": -33.05, "lng": -67.55}) is True
    assert record.latitud == -33.05
    assert record.longitud == -67.55
    assert session.flush_count == 1
    # Replays must be handled by the durable execution receipt. Once the
    # source has coordinates, the low-level writer never silently accepts it.
    assert apply(_candidate(), {"lat": -33.05, "lng": -67.55}) is False
    assert session.flush_count == 1
