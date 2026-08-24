from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import inspect

from routes import market as market_route
from routes import ticket as ticket_route


def _payload_and_status(result):
    response, status = result
    return response.get_json(), status


def _bare_route(route):
    """Exercise upload guards without involving authentication decorators."""

    return inspect.unwrap(route)


def test_market_image_limit_plus_one_rejects_before_storage_or_db(app, monkeypatch):
    upload_mock = Mock()
    tenant_resolution_mock = Mock(
        side_effect=AssertionError("tenant/database resolution must not run")
    )
    monkeypatch.setattr(market_route, "MARKET_PRODUCT_IMAGE_MAX_BYTES", 4)
    monkeypatch.setattr(market_route, "upload_to_gcs", upload_mock)
    monkeypatch.setattr(
        market_route,
        "_resolve_admin_tenant",
        tenant_resolution_mock,
    )

    with app.test_request_context(
        "/api/admin/market/catalog/7/images",
        method="POST",
        data={"image": (BytesIO(b"12345"), "oversize.png", "image/png")},
        content_type="multipart/form-data",
    ):
        result = _bare_route(market_route.admin_upload_product_image)(
            SimpleNamespace(id=11),
            7,
        )

    payload, status = _payload_and_status(result)
    assert status == 413
    assert payload == {
        "error": "Una imagen supera el límite permitido.",
        "code": "file_too_large",
        "max_file_bytes": 4,
    }
    upload_mock.assert_not_called()
    tenant_resolution_mock.assert_not_called()


def test_market_image_batch_over_five_rejects_before_storage_or_db(app, monkeypatch):
    upload_mock = Mock()
    tenant_resolution_mock = Mock(
        side_effect=AssertionError("tenant/database resolution must not run")
    )
    monkeypatch.setattr(market_route, "MARKET_PRODUCT_IMAGE_MAX_BYTES", 4)
    monkeypatch.setattr(market_route, "upload_to_gcs", upload_mock)
    monkeypatch.setattr(
        market_route,
        "_resolve_admin_tenant",
        tenant_resolution_mock,
    )
    images = [
        (BytesIO(b"x"), f"image-{index}.png", "image/png")
        for index in range(market_route.MARKET_PRODUCT_IMAGE_MAX_FILES + 1)
    ]

    with app.test_request_context(
        "/api/admin/market/catalog/7/images",
        method="POST",
        data={"images": images},
        content_type="multipart/form-data",
    ):
        result = _bare_route(market_route.admin_upload_product_image)(
            SimpleNamespace(id=11),
            7,
        )

    payload, status = _payload_and_status(result)
    assert status == 413
    assert payload["code"] == "too_many_files"
    assert payload["max_files"] == market_route.MARKET_PRODUCT_IMAGE_MAX_FILES
    upload_mock.assert_not_called()
    tenant_resolution_mock.assert_not_called()


def test_ticket_attachment_limit_plus_one_rejects_before_storage_or_db(app, monkeypatch):
    storage_mock = Mock()
    db_get_mock = Mock(side_effect=AssertionError("database lookup must not run"))
    monkeypatch.setattr(ticket_route, "TICKET_ATTACHMENT_MAX_BYTES", 4)
    monkeypatch.setattr(
        ticket_route,
        "guardar_archivo_adjunto_ticket",
        storage_mock,
    )
    monkeypatch.setattr(
        ticket_route,
        "db",
        SimpleNamespace(session=SimpleNamespace(get=db_get_mock)),
    )

    with app.test_request_context(
        "/tickets/municipio/9/responder",
        method="POST",
        data={
            "comentario": "Respuesta",
            "archivos": (BytesIO(b"12345"), "oversize.txt", "text/plain"),
        },
        content_type="multipart/form-data",
    ):
        result = _bare_route(ticket_route.responder_a_ticket)(
            SimpleNamespace(id=11),
            "municipio",
            9,
        )

    payload, status = _payload_and_status(result)
    assert status == 413
    assert payload == {
        "error": "Uno de los archivos supera el límite permitido.",
        "code": "file_too_large",
        "max_file_bytes": 4,
    }
    storage_mock.assert_not_called()
    db_get_mock.assert_not_called()


def test_ticket_attachment_batch_over_five_rejects_before_storage_or_db(app, monkeypatch):
    storage_mock = Mock()
    db_get_mock = Mock(side_effect=AssertionError("database lookup must not run"))
    monkeypatch.setattr(ticket_route, "TICKET_ATTACHMENT_MAX_BYTES", 4)
    monkeypatch.setattr(
        ticket_route,
        "guardar_archivo_adjunto_ticket",
        storage_mock,
    )
    monkeypatch.setattr(
        ticket_route,
        "db",
        SimpleNamespace(session=SimpleNamespace(get=db_get_mock)),
    )
    attachments = [
        (BytesIO(b"x"), f"attachment-{index}.txt", "text/plain")
        for index in range(ticket_route.TICKET_ATTACHMENT_MAX_FILES + 1)
    ]

    with app.test_request_context(
        "/tickets/municipio/9/responder",
        method="POST",
        data={"comentario": "Respuesta", "archivos": attachments},
        content_type="multipart/form-data",
    ):
        result = _bare_route(ticket_route.responder_a_ticket)(
            SimpleNamespace(id=11),
            "municipio",
            9,
        )

    payload, status = _payload_and_status(result)
    assert status == 413
    assert payload["code"] == "too_many_files"
    assert payload["max_files"] == ticket_route.TICKET_ATTACHMENT_MAX_FILES
    storage_mock.assert_not_called()
    db_get_mock.assert_not_called()
