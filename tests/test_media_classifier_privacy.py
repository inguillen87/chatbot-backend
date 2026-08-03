from types import SimpleNamespace
from unittest.mock import Mock, patch

from services import attachment_service
from services import interpretacion_imagen_service as image_service
from services.media_classifier import clasificar_adjunto_whatsapp
from services.multimodal_analyzer import UnsafeImageSource


def test_media_classifier_never_logs_or_returns_provider_payload(caplog):
    sensitive = (
        "https://signed.example.test/image.jpg?token=private-token "
        "DNI 32877851 PIN 7788"
    )
    attachment = {
        "url": "https://storage.example.test/evidence.jpg?signature=private",
        "mime_type": "image/jpeg",
    }

    with patch(
        "services.media_classifier.interpretar_imagen_para_chat",
        side_effect=RuntimeError(sensitive),
    ):
        with caplog.at_level("ERROR", logger="services.media_classifier"):
            result = clasificar_adjunto_whatsapp(
                attachment,
                SimpleNamespace(tipo_chat="municipio"),
            )

    rendered = caplog.text
    assert result == {
        "error": "media_classification_failed",
        "error_type": "RuntimeError",
    }
    assert "RuntimeError" in rendered
    assert sensitive not in rendered
    assert "private-token" not in rendered
    assert "32877851" not in rendered
    assert "7788" not in rendered


def test_media_classifier_keeps_pyme_interpretation_contract():
    expected = {"items": [{"nombre": "Producto"}]}
    owner = SimpleNamespace(tipo_chat="pyme")

    with patch(
        "services.media_classifier.interpretar_imagen_para_chat",
        return_value=expected,
    ) as interpret:
        result = clasificar_adjunto_whatsapp(
            {"url": "https://storage.example.test/order.jpg", "mime_type": "image/jpeg"},
            owner,
        )

    assert result == expected
    interpret.assert_called_once_with(
        archivo_adjunto={
            "url": "https://storage.example.test/order.jpg",
            "mime_type": "image/jpeg",
        },
        tipo_interpretacion="pedido_pyme",
        pyme_user=owner,
    )


def test_attachment_record_failure_never_logs_signed_url_or_filename(app):
    sensitive = "private-citizen-name-DNI-32877851"
    upload = {
        "unique_name": f"{sensitive}.jpg",
        "original_name": f"{sensitive}.jpg",
        "mimetype": "image/jpeg",
        "size": 128,
        "original_url": f"https://storage.example.test/file?token={sensitive}",
        "thumb_meta": {"width": 100, "height": 100},
    }
    fake_db = SimpleNamespace(
        session=SimpleNamespace(add=Mock(side_effect=RuntimeError(sensitive)))
    )

    with app.app_context(), patch.object(
        attachment_service,
        "guardar_adjunto_y_thumbnail",
        return_value=upload,
    ), patch.object(attachment_service, "db", fake_db), patch.object(
        app.logger,
        "error",
    ) as error_log:
        result = attachment_service.create_attachment_with_thumbnail(
            SimpleNamespace(filename=f"{sensitive}.jpg"),
            session_id="anonymous-session",
        )

    assert result is None
    error_log.assert_called_once()
    rendered_arguments = repr(error_log.call_args)
    assert "RuntimeError" in rendered_arguments
    assert sensitive not in rendered_arguments
    assert "token=" not in rendered_arguments


def test_second_hop_media_download_never_forwards_twilio_credentials(app):
    response = Mock()
    response.headers = {"Content-Length": "4"}
    response.iter_content.return_value = [b"data"]
    response.raise_for_status.return_value = None
    response.close.return_value = None
    app.config.update(
        TWILIO_ACCOUNT_SID="AC-private-account",
        TWILIO_AUTH_TOKEN="private-auth-token",
    )

    with app.app_context(), patch.object(
        image_service,
        "_assert_public_remote_url",
    ), patch.object(image_service.requests, "get", return_value=response) as get:
        result = image_service._descargar_imagen(
            "https://storage.example.test/evidence.jpg?signature=private"
        )

    assert result == b"data"
    assert get.call_args.kwargs["auth"] is None
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["stream"] is True
    assert "private-auth-token" not in repr(get.call_args)
    response.close.assert_called_once()


def test_second_hop_media_download_rejects_unsafe_host_without_leaking_url(
    app, caplog
):
    sensitive_url = "http://127.0.0.1/admin?token=private-citizen-token"

    with app.app_context(), patch.object(
        image_service,
        "_assert_public_remote_url",
        side_effect=UnsafeImageSource(sensitive_url),
    ), patch.object(image_service.requests, "get") as get:
        with caplog.at_level(
            "ERROR", logger="services.interpretacion_imagen_service"
        ):
            result = image_service._descargar_imagen(sensitive_url)

    assert result is None
    get.assert_not_called()
    assert "UnsafeImageSource" in caplog.text
    assert sensitive_url not in caplog.text
    assert "private-citizen-token" not in caplog.text


def test_image_interpretation_logs_only_counts_not_ocr_or_visual_text(caplog):
    sensitive = "DNI 32877851 vive en Don Bosco 55 PIN 7788"
    vision = {
        "labels": [{"description": sensitive, "confidence": 0.99}],
        "objects": [],
    }

    with patch(
        "services.llm_utils.generar_descripcion_natural_de_imagen",
        return_value=sensitive,
    ), patch.object(
        image_service,
        "extract_complaint_details_llm",
        return_value={
            "tipo_problema": "Luminaria",
            "descripcion_problema": sensitive,
        },
    ):
        with caplog.at_level(
            "INFO", logger="services.interpretacion_imagen_service"
        ):
            result = image_service._procesar_interpretacion_reclamo(
                None,
                vision,
                sensitive,
                auto_mode=True,
            )

    assert result["es_reclamo"] is True
    assert sensitive in result["texto_ocr"]
    assert sensitive not in caplog.text
    assert "32877851" not in caplog.text
    assert "7788" not in caplog.text
