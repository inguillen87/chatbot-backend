"""Keep the real official snapshot byte-identical in Windows and Linux releases."""
import hashlib
import json
from pathlib import Path
import subprocess

from services.territorial_evidence import _load_verified_boundary


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'data/municipios/junin/geo.json'
BOUNDARY = CONFIG.parent / 'official_department_boundary.geojson'


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
