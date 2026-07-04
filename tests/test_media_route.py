from flask import Flask

from routes.media import media_bp


def _media_app(data_dir):
    app = Flask(__name__)
    app.config["DATA_DIR"] = str(data_dir)
    app.register_blueprint(media_bp)
    return app


def test_media_reclamo_attachment_uses_private_cache_headers(tmp_path):
    attachment = tmp_path / "municipios" / "junin" / "reclamos" / "M-378430" / "foto.jpg"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"jpeg")

    response = _media_app(tmp_path).test_client().get("/media/municipios/junin/reclamos/M-378430/foto.jpg")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_media_fixed_menu_audio_uses_immutable_public_cache(tmp_path):
    audio = tmp_path / "static" / "audio_cache" / "menu.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"mp3")

    response = _media_app(tmp_path).test_client().get("/media/static/audio_cache/menu.mp3")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
