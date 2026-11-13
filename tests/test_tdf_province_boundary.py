"""Real IGN snapshot, independent province identity and exact-tenant containment."""
from copy import deepcopy
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from services.territorial_evidence import (
    _load_verified_boundary,
    coordinate_jurisdiction_status,
    resolve_tenant_jurisdiction,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "data/tenants/tierra-del-fuego/geo.json"
SNAPSHOT = CONFIG.parent / "official_province_boundary.geojson"


@pytest.fixture(scope="module")
def config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def boundary(config):
    result = _load_verified_boundary(str(CONFIG), config)
    assert result is not None
    return result


def test_committed_ign_snapshot_keeps_original_bytes(config):
    raw = SNAPSHOT.read_bytes()
    assert len(raw) == 1_753_050
    assert hashlib.sha256(raw).hexdigest() == "83e543b765d0879f1743892fb0267656f83816efaa3ea326dcb1f3a61df8930b"
    assert hashlib.sha256(raw).hexdigest() == config["boundary"]["snapshot_sha256"]
    if (ROOT / ".git").exists():
        committed = subprocess.check_output(
            ["git", "show", "HEAD:data/tenants/tierra-del-fuego/official_province_boundary.geojson"], cwd=ROOT,
        )
        assert committed == raw
        attributes = subprocess.check_output(
            ["git", "check-attr", "text", "--", str(SNAPSHOT.relative_to(ROOT))], cwd=ROOT, text=True,
        )
        assert attributes.rstrip().endswith(": text: unset")


def test_vercel_container_context_keeps_and_verifies_official_snapshots(tmp_path):
    assert json.loads((ROOT / 'vercel.json').read_text())['framework'] == 'container'
    configs = [CONFIG, ROOT / 'data/municipios/junin/geo.json']
    runtime_files = []
    for path in configs:
        spec = json.loads(path.read_text(encoding='utf-8'))['boundary']
        runtime_files.extend([path, path.parent / spec['file']])
    for filename in ('.vercelignore', '.dockerignore'):
        for rule in (ROOT / filename).read_text().splitlines():
            rule = rule.strip()
            if not rule or rule.startswith(('#', '!')):
                continue
            for path in runtime_files:
                relative = path.relative_to(ROOT)
                candidates = [relative.as_posix(), *[p.as_posix() for p in relative.parents if p != Path('.')]]
                assert not any(fnmatchcase(candidate, rule.strip('/')) for candidate in candidates), (
                    f'{filename} excludes an official boundary input: {rule}'
                )
    dockerfile = (ROOT / 'Dockerfile.vercel').read_text().replace('\\\n', ' ')
    guard_line = next(line for line in dockerfile.splitlines() if 'Corrupt official boundary snapshot in container:' in line)
    assert dockerfile.index('COPY . .') < dockerfile.index(guard_line)
    command = shlex.split(guard_line.removeprefix('RUN '))
    assert command[:2] == ['python', '-c']
    command[0] = sys.executable
    for path in runtime_files:
        exported = tmp_path / path.relative_to(ROOT)
        exported.parent.mkdir(parents=True, exist_ok=True)
        exported.write_bytes(path.read_bytes())
    valid = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr
    exported_snapshot = tmp_path / SNAPSHOT.relative_to(ROOT)
    exported_snapshot.unlink()
    missing = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert missing.returncode != 0
    assert SNAPSHOT.name in missing.stderr
    exported_snapshot.write_bytes(b'corrupt snapshot')
    corrupt = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert corrupt.returncode != 0
    assert 'Corrupt official boundary snapshot in container:' in corrupt.stderr


def test_ign_province_identity_and_all_official_components(boundary):
    authority = boundary["authority"]
    assert authority["identity_schema"] == "ign.province.v1"
    assert authority["province_code"] == "94"
    assert authority["object_id"] == "361"
    assert authority["publisher"] == "Instituto Geográfico Nacional de la República Argentina"
    assert authority["source_ref"] == authority["source_url"]
    assert boundary["truth_boundary"] == "official_province_boundary_generalized"
    assert boundary["geometry"]["type"] == "MultiPolygon"
    assert len(boundary["geometry"]["coordinates"]) == 1332
    assert sum(len(ring) for poly in boundary["geometry"]["coordinates"] for ring in poly) == 73487
    assert boundary["bounds"] == {"west": -74.029854, "south": -90.0, "east": -25.023145, "north": -51.000057}
    public = boundary["feature_collection"]
    assert public["metadata"]["official"] is True
    assert public["metadata"]["synthetic"] is False
    assert public["metadata"]["provenance"] == authority
    assert public["features"][0]["id"] == "94"
    assert public["features"][0]["properties"]["name"] == "Tierra del Fuego, Antártida e Islas del Atlántico Sur"


@pytest.mark.parametrize("label,lat,lng,expected", [
    ("ushuaia", -54.802, -68.303, "within"),
    ("rio_grande", -53.787, -67.710, "within"),
    ("tolhuin", -54.510, -67.195, "within"),
    ("porvenir_chile_inside_bounding_box", -53.297, -70.369, "outside"),
    ("punta_arenas_chile_inside_bounding_box", -53.164, -70.917, "outside"),
    ("buenos_aires", -34.604, -58.382, "outside"),
    ("malvinas_in_ign_province", -51.693, -57.858, "within"),
    ("marambio_in_ign_province", -64.241, -56.626, "within"),
    ("invalid_latitude", -90.00000001, -50, "missing"),
])
def test_provincial_membership_uses_polygon_not_envelope(boundary, label, lat, lng, expected):
    jurisdiction = {
        "enforced": True, "containment_verified": True,
        "bounds": boundary["bounds"], "boundary_geometry": boundary["geometry"],
    }
    assert coordinate_jurisdiction_status(lat, lng, jurisdiction) == expected, label


@pytest.mark.parametrize("section,key,value", [
    ("authority", "kind", "unverified"),
    ("authority", "publisher", "Another publisher"),
    ("authority", "province_code", "50"),
    ("authority", "province_code", None),
    ("authority", "province_code", ""),
    ("authority", "object_id", "362"),
    ("authority", "object_id", None),
    ("authority", "identity_schema", "mendoza.department.v1"),
    ("authority", "identity_schema", "unknown.province.v1"),
    ("authority", "match", {"name_property": "FNA", "features": [{"id": "94", "properties": {"IN1": "94"}}]}),
    ("authority", "service", "servicio_fondos/MapServer/23"),
    ("boundary", "out_sr", 3857),
    ("boundary", "max_allowable_offset_degrees", None),
    ("boundary", "max_allowable_offset_degrees", 0.1),
    ("boundary", "geometry_precision", True),
    ("boundary", "snapshot_sha256", "0" * 64),
    ("boundary", "file", "../official_province_boundary.geojson"),
    ("payload", "state", "Mendoza"),
    ("payload", "state", ""),
    ("payload", "jurisdiction_level", "department"),
])
def test_inconsistent_authority_and_identity_fail_closed(config, section, key, value):
    changed = deepcopy(config)
    target = changed if section == "payload" else changed["boundary"]
    if section == "authority":
        target = target["authority"]
    target[key] = value
    assert _load_verified_boundary(str(CONFIG), changed) is None


@pytest.mark.parametrize("old,new", [
    ("https://", "http://"),
    ("ide.ign.gob.ar", "ide.ign.gob.ar.example.com"),
    ("ide.ign.gob.ar", "example.com@ide.ign.gob.ar"),
    ("MapServer/0/query", "MapServer/23/query"),
    ("IN1%3D%2794%27", "IN1%3D%2750%27"),
    ("outSR=4326", "outSR=3857"),
    ("maxAllowableOffset=0.0005", "maxAllowableOffset=0.01"),
    ("geometryPrecision=6", "geometryPrecision=3"),
    ("f=geojson", "f=geojson&f=json"),
    ("geometryPrecision=6", "geometryPrecision=6#another-source"),
])
def test_source_reference_is_the_exact_supported_official_query(config, old, new):
    changed = deepcopy(config)
    changed["boundary"]["authority"]["source_url"] = changed["boundary"]["authority"]["source_url"].replace(old, new)
    assert _load_verified_boundary(str(CONFIG), changed) is None


@pytest.mark.parametrize("mutation", ["wrong_code", "wrong_object_id", "wrong_feature_id", "wrong_name", "wrong_object", "missing_identity", "extra_feature", "point_geometry", "invalid_latitude"])
def test_rehashed_wrong_source_content_is_not_accepted(config, tmp_path, mutation):
    changed = deepcopy(config)
    collection = json.loads(SNAPSHOT.read_bytes())
    feature = collection["features"][0]
    if mutation == "wrong_code":
        feature["properties"]["IN1"] = "50"
    elif mutation == "wrong_object_id":
        feature["properties"]["OBJECTID"] = 362
    elif mutation == "wrong_feature_id":
        feature["id"] = 362
    elif mutation == "wrong_name":
        feature["properties"]["FNA"] = "Mendoza"
    elif mutation == "wrong_object":
        feature["properties"]["Objeto"] = "Departamento"
    elif mutation == "missing_identity":
        feature["properties"] = {}
    elif mutation == "extra_feature":
        collection["features"].append(feature)
    elif mutation == "point_geometry":
        feature["geometry"] = {"type": "Point", "coordinates": [-68.303, -54.802]}
    elif mutation == "invalid_latitude":
        feature["geometry"]["coordinates"][0][0][0][1] = -90.00000001
    raw = json.dumps(collection, ensure_ascii=False, separators=(",", ":")).encode()
    (tmp_path / SNAPSHOT.name).write_bytes(raw)
    changed["boundary"]["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
    assert _load_verified_boundary(str(tmp_path / "geo.json"), changed) is None


def test_tdf_resolution_and_junin_regression_remain_tenant_specific(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    tdf = resolve_tenant_jurisdiction(SimpleNamespace(slug="tierra-del-fuego", municipio_id=None))
    assert tdf["containment_verified"] is True
    assert tdf["containment_method"] == "point_in_polygon"
    assert tdf["truth_boundary"] == "official_province_boundary_generalized"
    assert tdf["source"]["ref"] == "tenants/tierra-del-fuego/geo.json"
    assert coordinate_jurisdiction_status(-54.802, -68.303, tdf) == "within"
    junin = resolve_tenant_jurisdiction(SimpleNamespace(slug="junin", municipio_id=None))
    assert junin["containment_verified"] is True
    assert junin["boundary_authority"]["department_code"] == "09"
    assert junin["truth_boundary"] == "official_department_boundary_generalized"
    assert coordinate_jurisdiction_status(-54.802, -68.303, junin) == "outside"
    unknown = resolve_tenant_jurisdiction(SimpleNamespace(slug="unconfigured-province", municipio_id=None))
    assert unknown["enforced"] is False
    assert unknown["truth_boundary"] == "no_tenant_specific_envelope"
