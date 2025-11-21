import json
from datetime import datetime, timedelta, timezone

from services import subastas as subastas_service


def _write_subastas(tmp_path, entries):
    data_path = tmp_path / "subastas.json"
    data_path.write_text(json.dumps(entries), encoding="utf-8")
    return data_path


def test_listar_subastas_activas_filtra_y_ordena(monkeypatch, tmp_path):
    data_path = _write_subastas(
        tmp_path,
        [
            {
                "id": "activa-1",
                "titulo": "Activa Uno",
                "fecha_publicacion": "2024-12-10T12:00:00Z",
                "fecha_cierre": "2025-01-05T10:00:00Z",
                "precio_base": 100,
            },
            {
                "id": "activa-2",
                "titulo": "Activa Dos",
                "fecha_publicacion": "2024-12-11T12:00:00Z",
                "fecha_cierre": "2025-01-03T10:00:00Z",
                "precio_base": 200,
            },
            {
                "id": "cerrada",
                "titulo": "Cerrada",
                "estado": "finalizada",
                "fecha_cierre": "2024-12-01T12:00:00Z",
            },
            {
                "id": "futura",
                "titulo": "Futura",
                "fecha_publicacion": "2025-02-01T00:00:00Z",
                "fecha_cierre": "2025-03-01T00:00:00Z",
            },
        ],
    )

    monkeypatch.setenv("SUBASTAS_DATA_PATH", str(data_path))
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)

    activas = subastas_service.listar_subastas_activas(now=now)

    assert [subasta["id"] for subasta in activas] == ["activa-2", "activa-1"]
    assert activas[0]["precio_base"] == 200


def test_obtener_subasta_por_id_busca_por_slug(monkeypatch, tmp_path):
    cierre = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    data_path = _write_subastas(
        tmp_path,
        [
            {
                "titulo": "Equipo Tecnológico",
                "slug": "equipo-tecnologico",
                "fecha_publicacion": "2024-12-10T12:00:00Z",
                "fecha_cierre": cierre,
            }
        ],
    )

    monkeypatch.setenv("SUBASTAS_DATA_PATH", str(data_path))

    encontrado = subastas_service.obtener_subasta_por_id("equipo-tecnologico")

    assert encontrado is not None
    assert encontrado["titulo"] == "Equipo Tecnológico"
