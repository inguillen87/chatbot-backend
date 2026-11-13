"""Vista inicial del mapa: sólo cámara, nunca territorio."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from services.territorial_evidence import _initial_view_bounds, resolve_tenant_jurisdiction

ROOT = Path(__file__).resolve().parents[1]
ENVELOPE = {'west': -74.0, 'south': -90.0, 'east': -25.0, 'north': -51.0}


def _tenant(slug):
    return SimpleNamespace(slug=slug, municipio_id=None)


def test_tdf_declares_an_island_view_inside_the_provincial_envelope_without_touching_containment():
    jurisdiction = resolve_tenant_jurisdiction(_tenant('tierra-del-fuego'))
    view = jurisdiction['initial_view_bounds']

    assert view == {'west': -68.75, 'south': -55.15, 'east': -63.7, 'north': -52.55}
    # La jurisdicción sigue siendo la provincia completa publicada por el IGN.
    assert jurisdiction['bounds']['south'] == -90.0
    assert jurisdiction['containment_verified'] is True
    assert jurisdiction['truth_boundary'] == 'official_province_boundary_generalized'
    for lng, lat in ((-68.3030, -54.8019), (-67.7097, -53.7877), (-67.1955, -54.5108)):
        assert view['west'] <= lng <= view['east'] and view['south'] <= lat <= view['north']


def test_junin_has_no_declared_view_and_keeps_its_contract():
    jurisdiction = resolve_tenant_jurisdiction(_tenant('junin'))
    assert jurisdiction['initial_view_bounds'] is None
    assert jurisdiction['containment_verified'] is True


def test_view_must_be_a_valid_box_inside_the_jurisdiction():
    inside = {'bounds': [-68.75, -55.15, -63.7, -52.55]}
    assert _initial_view_bounds(inside, ENVELOPE) == {'west': -68.75, 'south': -55.15, 'east': -63.7, 'north': -52.55}
    for value in (
        None, [], 'isla', {}, {'bounds': None}, {'bounds': [-68.75, -55.15, -63.7]},
        {'bounds': [-63.7, -55.15, -68.75, -52.55]},      # invertida
        {'bounds': [-80.0, -55.15, -63.7, -52.55]},       # se sale por el oeste
        {'bounds': [-68.75, -55.15, -63.7, -40.0]},       # se sale por el norte
        {'bounds': [-68.75, 'sur', -63.7, -52.55]},
    ):
        assert _initial_view_bounds(value, ENVELOPE) is None, value


def test_a_view_outside_the_territory_is_ignored_and_the_jurisdiction_stays_verified(tmp_path, monkeypatch):
    folder = tmp_path / 'tenants' / 'tierra-del-fuego'
    shutil.copytree(ROOT / 'data' / 'tenants' / 'tierra-del-fuego', folder)
    config = json.loads((folder / 'geo.json').read_text(encoding='utf-8'))
    config['initial_view'] = {'bounds': [-58.6, -34.8, -58.2, -34.5]}  # Buenos Aires
    (folder / 'geo.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))

    jurisdiction = resolve_tenant_jurisdiction(_tenant('tierra-del-fuego'))
    assert jurisdiction['initial_view_bounds'] is None
    assert jurisdiction['containment_verified'] is True


def test_empty_heatmap_preset_uses_the_declared_view_and_points_still_win():
    # Importa el módulo completo: corre en la CI del backend, no con --noconftest.
    from services.operational_intelligence import _heatmap_viewport_presets

    view = {'west': -68.75, 'south': -55.15, 'east': -63.7, 'north': -52.55}
    empty = _heatmap_viewport_presets(bounds=None, hotspots=[], quality={}, geocoding_candidates=[], initial_view=view)
    assert empty['default_preset_id'] == 'tenant_region_empty'
    assert empty['presets'][0]['mode'] == 'fit_bounds'
    assert empty['presets'][0]['bounds'] == view
    assert 'requires_frontend_default_center' not in empty['presets'][0]

    undeclared = _heatmap_viewport_presets(bounds=None, hotspots=[], quality={}, geocoding_candidates=[])
    assert undeclared['presets'][0]['mode'] == 'frontend_default_region'

    active = {'west': -68.4, 'south': -54.9, 'east': -67.1, 'north': -53.7}
    with_points = _heatmap_viewport_presets(bounds=active, hotspots=[], quality={}, geocoding_candidates=[], initial_view=view)
    assert with_points['default_preset_id'] == 'fit_operational_bounds'
    assert [preset['id'] for preset in with_points['presets']] == ['fit_operational_bounds']
