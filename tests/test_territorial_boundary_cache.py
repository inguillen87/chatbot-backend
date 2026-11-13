"""Caché del límite verificado y descarte por envolvente: mismos resultados, menos trabajo."""
import hashlib
import json
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.territorial_evidence as evidence

ROOT = Path(__file__).resolve().parents[1]
TDF = ROOT / 'data' / 'tenants' / 'tierra-del-fuego'
JUNIN = ROOT / 'data' / 'municipios' / 'junin'


@pytest.fixture(autouse=True)
def _clean_cache():
    evidence.clear_verified_boundary_cache()
    yield
    evidence.clear_verified_boundary_cache()


def _load(folder):
    config = json.loads((folder / 'geo.json').read_text(encoding='utf-8'))
    return evidence._load_verified_boundary(str(folder / 'geo.json'), config), config


def _unindexed(geometry, lng, lat):
    return any(evidence._polygon_contains_position(polygon, lng, lat) for polygon in geometry['coordinates'])


def test_envelopes_never_change_a_containment_answer_on_the_official_snapshot():
    boundary, _ = _load(TDF)
    geometry = boundary['geometry']
    assert id(geometry) in evidence._BOUNDARY_ENVELOPES

    generator = random.Random(94)
    points = [(generator.uniform(-75.0, -24.0), generator.uniform(-90.0, -50.0)) for _ in range(350)]
    points += [(generator.uniform(-69.0, -64.5), generator.uniform(-55.2, -52.6)) for _ in range(350)]
    # Vértices y puntos medios de aristas: el borde debe seguir contando como dentro.
    for polygon in geometry['coordinates'][::40]:
        ring = polygon[0]
        points.append(tuple(ring[0]))
        points.append(((ring[0][0] + ring[1][0]) / 2, (ring[0][1] + ring[1][1]) / 2))

    differences = [
        point for point in points
        if evidence._boundary_contains_position(geometry, *point) != _unindexed(geometry, *point)
    ]
    assert differences == []
    assert any(evidence._boundary_contains_position(geometry, *point) for point in points)
    assert not all(evidence._boundary_contains_position(geometry, *point) for point in points)


def test_reference_places_keep_their_answer():
    jurisdiction = evidence.resolve_tenant_jurisdiction(SimpleNamespace(slug='tierra-del-fuego', municipio_id=None))
    inside = [(-54.8019, -68.3030), (-53.7877, -67.7097), (-54.5108, -67.1955)]
    outside = [(-53.1638, -70.9171), (-54.0, -68.8), (-34.6, -58.38)]
    assert [evidence.coordinate_jurisdiction_status(lat, lng, jurisdiction) for lat, lng in inside] == ['within'] * 3
    assert [evidence.coordinate_jurisdiction_status(lat, lng, jurisdiction) for lat, lng in outside] == ['outside'] * 3


def test_same_snapshot_is_read_and_validated_once(monkeypatch):
    reads = []
    original = evidence._read_verified_boundary
    monkeypatch.setattr(evidence, '_read_verified_boundary', lambda *args: reads.append(1) or original(*args))

    first, _ = _load(TDF)
    second, _ = _load(TDF)
    assert first is second and len(reads) == 1


def test_a_snapshot_with_another_sha_never_reuses_the_cached_geometry(tmp_path):
    folder = tmp_path / 'tenants' / 'copia'
    shutil.copytree(JUNIN, folder)
    first, config = _load(folder)
    assert first is not None

    # Mismo nombre de archivo, otro contenido: el SHA declarado ya no coincide.
    snapshot = folder / config['boundary']['file']
    collection = json.loads(snapshot.read_text(encoding='utf-8'))
    ring = collection['features'][0]['geometry']['coordinates'][0]
    ring = ring[0] if isinstance(ring[0][0], list) else ring
    ring[1][0] += 0.01
    snapshot.write_text(json.dumps(collection), encoding='utf-8')
    rejected, _ = _load(folder)
    assert rejected is None

    # Declarado el SHA nuevo, se valida de nuevo y la geometría es otra.
    config['boundary']['snapshot_sha256'] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    (folder / 'geo.json').write_text(json.dumps(config), encoding='utf-8')
    updated, _ = _load(folder)
    assert updated is not None and updated is not first
    assert updated['geometry'] != first['geometry']
    assert updated['authority']['snapshot_sha256'] == config['boundary']['snapshot_sha256']


def test_rejected_snapshots_are_not_remembered(tmp_path):
    folder = tmp_path / 'tenants' / 'copia'
    shutil.copytree(JUNIN, folder)
    config = json.loads((folder / 'geo.json').read_text(encoding='utf-8'))
    good = (folder / config['boundary']['file']).read_bytes()
    (folder / config['boundary']['file']).write_bytes(good + b' ')
    assert _load(folder)[0] is None
    (folder / config['boundary']['file']).write_bytes(good)
    assert _load(folder)[0] is not None


def test_cache_is_bounded_and_drops_envelopes_with_the_entry(tmp_path):
    for index in range(evidence._BOUNDARY_CACHE_LIMIT + 3):
        folder = tmp_path / str(index) / 'copia'
        shutil.copytree(JUNIN, folder)
        assert _load(folder)[0] is not None
    assert len(evidence._VERIFIED_BOUNDARY_CACHE) == evidence._BOUNDARY_CACHE_LIMIT
    assert len(evidence._BOUNDARY_ENVELOPES) <= evidence._BOUNDARY_CACHE_LIMIT
