"""Límites oficiales declarados por entidad (varias jurisdicciones en una organización).

Las geometrías de estas pruebas son sintéticas y viven sólo en tmp_path: prueban
el cargador, no representan ningún límite real.
"""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from services.territorial_evidence import (
    _load_verified_boundary,
    coordinate_jurisdiction_status,
    resolve_tenant_jurisdiction,
)

JUNIN_CONFIG = Path(__file__).resolve().parents[1] / 'data' / 'municipios' / 'junin' / 'geo.json'


def _square(west, south, size=1.0):
    east, north = west + size, south + size
    return {'type': 'Polygon', 'coordinates': [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}


def _feature(code, name, geometry):
    return {'type': 'Feature', 'properties': {'in1': code, 'nam': name, 'gid': int(code)}, 'geometry': geometry}


FEATURES = [_feature('94007', 'Alfa', _square(-68.0, -55.0)), _feature('94014', 'Beta', _square(-66.0, -55.0))]
MATCH = {
    'name_property': 'nam',
    'features': [
        {'id': '94007', 'properties': {'in1': '94007', 'nam': 'Alfa'}},
        {'id': '94014', 'properties': {'in1': '94014', 'nam': 'Beta'}},
    ],
}


def _write(tmp_path, features=FEATURES, match=MATCH, slug='organizacion-sintetica'):
    folder = tmp_path / 'tenants' / slug
    folder.mkdir(parents=True)
    raw = json.dumps({'type': 'FeatureCollection', 'features': features}).encode('utf-8')
    (folder / 'limite.geojson').write_bytes(raw)
    config = {
        'city': 'Jurisdicción sintética', 'state': 'Provincia sintética', 'country': 'AR',
        'bounds': [-70.0, -56.0, -60.0, -50.0],
        'boundary': {
            'file': 'limite.geojson', 'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
            'authority': {'kind': 'official', 'publisher': 'Organismo sintético',
                          'source_url': 'https://example.test/limites', 'match': match},
        },
    }
    (folder / 'geo.json').write_text(json.dumps(config), encoding='utf-8')
    return folder / 'geo.json', config


def test_declared_entities_form_one_containment_geometry(tmp_path):
    path, config = _write(tmp_path)
    boundary = _load_verified_boundary(str(path), config)

    assert boundary['geometry']['type'] == 'MultiPolygon'
    assert len(boundary['geometry']['coordinates']) == 2
    assert boundary['bounds'] == {'west': -68.0, 'south': -55.0, 'east': -65.0, 'north': -54.0}
    assert [item['id'] for item in boundary['feature_collection']['features']] == ['94007', '94014']
    assert [item['properties']['name'] for item in boundary['feature_collection']['features']] == ['Alfa', 'Beta']


def test_resolved_jurisdiction_contains_points_of_any_declared_entity_only(tmp_path, monkeypatch):
    _write(tmp_path)
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    jurisdiction = resolve_tenant_jurisdiction(SimpleNamespace(slug='organizacion-sintetica', municipio_id=None))

    assert jurisdiction['containment_verified'] is True
    assert jurisdiction['containment_method'] == 'point_in_polygon'
    assert coordinate_jurisdiction_status(-54.5, -67.5, jurisdiction) == 'within'
    assert coordinate_jurisdiction_status(-54.5, -65.5, jurisdiction) == 'within'
    # Entre las dos entidades: dentro del rectángulo operativo, fuera del límite oficial.
    assert coordinate_jurisdiction_status(-54.5, -66.5, jurisdiction) == 'outside'


def test_snapshot_cannot_widen_or_narrow_the_declared_set(tmp_path):
    extra = FEATURES + [_feature('94999', 'Gamma', _square(-64.0, -55.0))]
    path, config = _write(tmp_path / 'extra', features=extra)
    assert _load_verified_boundary(str(path), config) is None

    path, config = _write(tmp_path / 'missing', features=FEATURES[:1])
    assert _load_verified_boundary(str(path), config) is None

    duplicated = [FEATURES[0], copy.deepcopy(FEATURES[0])]
    path, config = _write(tmp_path / 'duplicated', features=duplicated)
    assert _load_verified_boundary(str(path), config) is None


def test_entity_with_another_identity_is_rejected(tmp_path):
    renamed = [FEATURES[0], _feature('94014', 'Otra', _square(-66.0, -55.0))]
    path, config = _write(tmp_path, features=renamed)
    assert _load_verified_boundary(str(path), config) is None


def test_malformed_match_never_verifies(tmp_path):
    for index, match in enumerate([
        {}, {'name_property': 'nam', 'features': []}, {'features': MATCH['features']},
        {'name_property': 'nam', 'features': [{'id': '1', 'properties': {}}, {'id': '2', 'properties': {'in1': '94014'}}]},
        {'name_property': 'nam', 'features': [MATCH['features'][0], MATCH['features'][0]]},
        'ign',
    ]):
        path, config = _write(tmp_path / str(index), match=match)
        assert _load_verified_boundary(str(path), config) is None, match


def test_bundled_junin_boundary_keeps_its_single_department_contract():
    config = json.loads(JUNIN_CONFIG.read_text(encoding='utf-8'))
    boundary = _load_verified_boundary(str(JUNIN_CONFIG), config)

    assert 'match' not in config['boundary']['authority']
    assert boundary['geometry']['type'] in {'Polygon', 'MultiPolygon'}
    assert len(boundary['feature_collection']['features']) == 1
    assert boundary['feature_collection']['features'][0]['id'] == '09'
    assert boundary['feature_collection']['features'][0]['properties']['name']
