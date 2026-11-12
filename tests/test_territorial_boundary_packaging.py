"""Keep the real official snapshot byte-identical in Windows and Linux releases."""
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from services.territorial_evidence import (
    _load_verified_boundary,
    coordinate_jurisdiction_disposition,
    coordinate_jurisdiction_status,
    resolve_tenant_jurisdiction,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'data/municipios/junin/geo.json'
BOUNDARY = CONFIG.parent / 'official_department_boundary.geojson'
TDF_CONFIG = ROOT / 'data/tenants/tierra-del-fuego/geo.json'
TDF_BOUNDARY = TDF_CONFIG.parent / 'official_province_boundary.geojson'
TDF_SHA256 = '71811b67b81af7bd9715e957ea4a0f6c3e6376f490c7e0fc8f7f67c6798aa90b'


def test_committed_official_snapshot_matches_runtime_integrity_and_lf_checkout():
    raw = BOUNDARY.read_bytes()
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    assert b'\r\n' not in raw
    assert hashlib.sha256(raw).hexdigest() == config['boundary']['snapshot_sha256']
    boundary = _load_verified_boundary(str(CONFIG), config)
    assert boundary is not None
    assert boundary['authority']['publisher'] == 'Infraestructura de Datos Espaciales de Mendoza'
    assert boundary['authority']['department_code'] == '09'
    assert boundary['authority']['global_id'] == '{FEA13AA1-46F3-4570-BAEE-188FF11AFF94}'
    assert boundary['geometry']['type'] == 'Polygon'
    if (ROOT / '.git').exists():
        git_bytes = subprocess.check_output([
            'git', 'show', 'HEAD:data/municipios/junin/official_department_boundary.geojson',
        ], cwd=ROOT)
        assert raw == git_bytes, 'A Windows-only snapshot digest cannot certify the deployed Linux artifact'


def test_line_ending_or_geometry_corruption_still_fails_closed(tmp_path):
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    copied = tmp_path / BOUNDARY.name
    copied.write_bytes(BOUNDARY.read_bytes().replace(b'\n', b'\r\n'))
    assert _load_verified_boundary(str(tmp_path / 'geo.json'), config) is None
    copied.write_bytes(BOUNDARY.read_bytes().replace(b'JUNIN', b'OTHER', 1))
    assert _load_verified_boundary(str(tmp_path / 'geo.json'), config) is None


@pytest.mark.parametrize('field,value', [
    ('publisher', 'Unverified publisher'),
    ('source_url', 'https://ide.mendoza.gov.ar.attacker.invalid/boundary'),
    ('source_url', 'https://ide.mendoza.gov.ar@attacker.invalid/boundary'),
    ('source_url', 'http://ide.mendoza.gov.ar/boundary'),
    ('source_url', 'https://ide.mendoza.gov.ar/unrelated-resource'),
    ('department_code', '08'),
    ('global_id', '{OTHER-DEPARTMENT}'),
    ('schema', 'unrecognized'),
])
def test_department_authority_and_identity_cannot_be_spoofed(field, value):
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    config['boundary']['authority'][field] = value
    assert _load_verified_boundary(str(CONFIG), config) is None


def test_missing_department_identity_is_not_verified_by_matching_absence(tmp_path):
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    collection = json.loads(BOUNDARY.read_bytes())
    for authority_field, property_field in (
        ('department_code', 'codigo_dep'), ('global_id', 'globalid'),
    ):
        config['boundary']['authority'].pop(authority_field)
        collection['features'][0]['properties'].pop(property_field)
    raw = json.dumps(collection).encode('utf-8')
    config['boundary']['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
    (tmp_path / BOUNDARY.name).write_bytes(raw)
    assert _load_verified_boundary(str(tmp_path / 'geo.json'), config) is None


def test_junin_resolution_keeps_official_identity_and_polygon_containment(monkeypatch):
    monkeypatch.delenv('DATA_DIR', raising=False)
    jurisdiction = resolve_tenant_jurisdiction(SimpleNamespace(slug='junin', municipio_id=None))
    assert jurisdiction['containment_verified'] is True
    assert jurisdiction['containment_method'] == 'point_in_polygon'
    assert jurisdiction['truth_boundary'] == 'official_department_boundary_generalized'
    assert jurisdiction['boundary_authority']['department_code'] == '09'
    assert jurisdiction['boundary_feature_collection']['features'][0]['id'] == '09'
    assert coordinate_jurisdiction_status(-33.136, -68.49, jurisdiction) == 'within'
    assert coordinate_jurisdiction_status(-54.8, -68.3, jurisdiction) == 'outside'


def test_unknown_tenant_does_not_inherit_a_bundled_jurisdiction(monkeypatch):
    monkeypatch.delenv('DATA_DIR', raising=False)
    jurisdiction = resolve_tenant_jurisdiction(SimpleNamespace(slug='no-territory-config', municipio_id=None))
    assert jurisdiction['state'] == 'unconfigured'
    assert jurisdiction['enforced'] is False
    assert not jurisdiction.get('containment_verified')
    assert jurisdiction['bounds'] is None


def test_tdf_snapshot_matches_official_province_identity_and_all_parts():
    raw = TDF_BOUNDARY.read_bytes()
    config = json.loads(TDF_CONFIG.read_text(encoding='utf-8'))
    assert b'\r\n' not in raw
    assert hashlib.sha256(raw).hexdigest() == config['boundary']['snapshot_sha256'] == TDF_SHA256
    boundary = _load_verified_boundary(str(TDF_CONFIG), config)
    assert boundary is not None
    assert boundary['geometry']['type'] == 'MultiPolygon'
    assert len(boundary['geometry']['coordinates']) == 1332
    assert boundary['authority']['publisher'] == 'Instituto Geográfico Nacional'
    assert boundary['authority']['province_code'] == '94'
    assert boundary['authority']['feature_id'] == '361'
    assert boundary['authority']['source_ref'] == config['boundary']['authority']['source_url']
    assert boundary['bounds'] == {
        'west': -74.029854, 'south': -90.0, 'east': -25.023145, 'north': -51.000057,
    }
    public = boundary['feature_collection']
    assert public['metadata']['official'] is True
    assert public['metadata']['synthetic'] is False
    assert public['metadata']['provenance'] == boundary['authority']
    assert public['features'][0]['id'] == '94'
    assert public['features'][0]['properties']['OBJECTID'] == 361
    assert public['features'][0]['geometry'] == json.loads(raw)['features'][0]['geometry']


def test_tdf_resolution_uses_polygon_containment_with_correct_coordinate_axes(monkeypatch):
    monkeypatch.delenv('DATA_DIR', raising=False)
    jurisdiction = resolve_tenant_jurisdiction(SimpleNamespace(slug='tierra-del-fuego', municipio_id=None))
    assert jurisdiction['containment_verified'] is True
    assert jurisdiction['containment_method'] == 'point_in_polygon'
    assert jurisdiction['truth_boundary'] == 'official_province_boundary_generalized'
    assert jurisdiction['source']['ref'] == 'tenants/tierra-del-fuego/geo.json'
    assert jurisdiction['boundary_authority']['snapshot_sha256'] == TDF_SHA256
    for name, lat, lng in (
        ('Ushuaia', -54.80191, -68.30295),
        ('Río Grande', -53.78769, -67.71049),
        ('Tolhuin', -54.51083, -67.20266),
    ):
        assert coordinate_jurisdiction_status(lat, lng, jurisdiction) == 'within', name
    for name, lat, lng in (
        ('Junín', -33.1473, -68.4804),
        ('Punta Arenas', -53.1638, -70.9078),
        ('Océano', -54.0, -60.0),
    ):
        assert coordinate_jurisdiction_status(lat, lng, jurisdiction) == 'outside', name
    # Both points lie inside the province's bounding box, but not its polygons.
    bounds = jurisdiction['bounds']
    assert bounds['south'] < -53.1638 < bounds['north']
    assert bounds['west'] < -70.9078 < bounds['east']


@pytest.mark.parametrize('field,value', [
    ('publisher', 'Unverified publisher'),
    ('source_url', 'https://ide.ign.gob.ar.attacker.invalid/boundary'),
    ('source_url', 'https://ide.ign.gob.ar@attacker.invalid/boundary'),
    ('source_url', 'http://ide.ign.gob.ar/boundary'),
    ('source_url', 'https://ide.ign.gob.ar/unrelated-resource'),
    ('service', 'another-service'),
    ('province_code', '06'),
    ('feature_id', '999'),
    ('schema', 'unrecognized'),
    ('kind', 'synthetic'),
])
def test_province_authority_and_identity_cannot_be_spoofed(field, value):
    config = json.loads(TDF_CONFIG.read_text(encoding='utf-8'))
    config['boundary']['authority'][field] = value
    assert _load_verified_boundary(str(TDF_CONFIG), config) is None


@pytest.mark.parametrize('config_path,old,new', [
    (CONFIG, 'outSR=4326', 'outSR=3857'),
    (CONFIG, '%2709%27', '%2708%27'),
    (TDF_CONFIG, 'outSR=4326', 'outSR=3857'),
    (TDF_CONFIG, '%2794%27', '%2706%27'),
    (TDF_CONFIG, 'Entidad%3D5', 'Entidad%3D3'),
    (TDF_CONFIG, 'outSR=4326', 'outSR=4326&outSR=3857'),
])
def test_source_query_cannot_claim_another_jurisdiction_or_projection(config_path, old, new):
    config = json.loads(config_path.read_text(encoding='utf-8'))
    authority = config['boundary']['authority']
    assert old in authority['source_url']
    authority['source_url'] = authority['source_url'].replace(old, new)
    assert _load_verified_boundary(str(config_path), config) is None


@pytest.mark.parametrize('field,value', [
    ('OBJECTID', 999),
    ('Entidad', 3),
    ('Objeto', 'Departamento'),
    ('IN1', '06'),
    ('FNA', 'Buenos Aires'),
])
def test_province_feature_identity_is_checked_even_with_a_matching_hash(tmp_path, field, value):
    config = json.loads(TDF_CONFIG.read_text(encoding='utf-8'))
    collection = json.loads(TDF_BOUNDARY.read_bytes())
    collection['features'][0]['properties'][field] = value
    raw = json.dumps(collection).encode('utf-8')
    config['boundary']['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
    (tmp_path / TDF_BOUNDARY.name).write_bytes(raw)
    assert _load_verified_boundary(str(tmp_path / 'geo.json'), config) is None


@pytest.mark.parametrize('mutation', ['feature_id', 'feature_type', 'additional_feature', 'missing_identity'])
def test_province_collection_cannot_substitute_or_mix_features(tmp_path, mutation):
    config = json.loads(TDF_CONFIG.read_text(encoding='utf-8'))
    collection = json.loads(TDF_BOUNDARY.read_bytes())
    feature = collection['features'][0]
    if mutation == 'feature_id':
        feature['id'] = 999
    elif mutation == 'feature_type':
        feature['type'] = 'NotAFeature'
    elif mutation == 'additional_feature':
        collection['features'].append({'type': 'Feature', 'properties': {}, 'geometry': None})
    else:
        feature.pop('id')
        feature['properties'].pop('OBJECTID')
        config['boundary']['authority'].pop('feature_id')
    raw = json.dumps(collection).encode('utf-8')
    config['boundary']['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
    (tmp_path / TDF_BOUNDARY.name).write_bytes(raw)
    assert _load_verified_boundary(str(tmp_path / 'geo.json'), config) is None


def test_province_snapshot_byte_changes_fail_integrity(tmp_path):
    config = json.loads(TDF_CONFIG.read_text(encoding='utf-8'))
    (tmp_path / TDF_BOUNDARY.name).write_bytes(TDF_BOUNDARY.read_bytes() + b'\n')
    assert _load_verified_boundary(str(tmp_path / 'geo.json'), config) is None


def test_invalid_persistent_province_boundary_does_not_bypass_verification(tmp_path, monkeypatch):
    config = json.loads(TDF_CONFIG.read_text(encoding='utf-8'))
    folder = tmp_path / 'tenants/tierra-del-fuego'
    folder.mkdir(parents=True)
    (folder / 'geo.json').write_text(json.dumps(config), encoding='utf-8')
    (folder / TDF_BOUNDARY.name).write_bytes(b'corrupted snapshot')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    jurisdiction = resolve_tenant_jurisdiction(SimpleNamespace(slug='tierra-del-fuego', municipio_id=None))
    assert jurisdiction['containment_verified'] is False
    assert jurisdiction['boundary_authority'] is None
    assert jurisdiction['boundary_geometry'] is None
    assert jurisdiction['source']['storage'] == 'persistent_data'
    assert coordinate_jurisdiction_disposition(
        coordinate_jurisdiction_status(-54.80191, -68.30295, jurisdiction),
        require_verified_jurisdiction=True,
        jurisdiction_verified=jurisdiction['containment_verified'],
    ) == 'unverified'
