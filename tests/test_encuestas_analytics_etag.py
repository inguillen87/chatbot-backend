from flask import Flask

from routes import encuestas_analytics as analytics_routes


def test_json_with_etag_returns_304_on_match():
    app = Flask(__name__)
    payload = {"encuesta_id": 84, "kpis": {"total": 5}}

    with app.test_request_context('/x'):
        first = analytics_routes._json_with_etag(payload)
        assert first.status_code == 200
        etag = first.headers.get('ETag')
        assert etag

    with app.test_request_context('/x', headers={'If-None-Match': etag}):
        second = analytics_routes._json_with_etag(payload)
        assert second.status_code == 304
