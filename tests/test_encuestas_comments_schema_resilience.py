from models import EncComentario, EncEncuesta
from services import encuestas_service


def test_list_comentarios_degrades_when_report_count_column_missing(client, monkeypatch):
    encuesta = EncEncuesta(
        tenant_id=1,
        slug="encuesta-resilience",
        titulo="Encuesta Resilience",
        tipo="opinion",
        estado="publicada",
        permitir_comentarios=True,
    )
    encuestas_service.db.session.add(encuesta)
    encuestas_service.db.session.flush()

    comentario = EncComentario(
        encuesta_id=encuesta.id,
        nombre_autor="Tester",
        texto="Comentario demo",
        estado="publicado",
    )
    encuestas_service.db.session.add(comentario)
    encuestas_service.db.session.commit()

    monkeypatch.setattr(encuestas_service, "_enc_comentario_has_report_count", lambda: False)

    items = encuestas_service.list_comentarios(encuesta.id, limit=10, offset=0)
    assert isinstance(items, list)
    assert items
    assert items[0]["texto"] == "Comentario demo"
