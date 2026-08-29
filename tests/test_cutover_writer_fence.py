from pathlib import Path

from config import Config


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _set_fence(client, enabled: bool) -> bool:
    previous = client.application.config.get("CUTOVER_WRITER_FENCE_ENABLED")
    client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = enabled
    return previous


def test_cutover_writer_fence_defaults_fail_open_for_normal_operation():
    assert Config.CUTOVER_WRITER_FENCE_ENABLED is False
    assert (
        "CUTOVER_WRITER_FENCE_ENABLED=false"
        in (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    )


def test_cutover_writer_fence_blocks_mutating_methods_before_route_handlers(client):
    previous = _set_fence(client, True)
    try:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = client.open("/api/cutover-probe", method=method)

            assert response.status_code == 503
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Retry-After"] == "60"
            assert response.get_json() == {
                "contract_version": "cutover.writer_fence.v1",
                "status": "maintenance",
                "reason_code": "cutover_writer_fence_enabled",
                "retryable": True,
            }
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous


def test_cutover_writer_fence_keeps_health_and_safe_methods_available(client):
    previous = _set_fence(client, True)
    try:
        assert client.get("/health").status_code == 200
        assert client.head("/health").status_code == 200
        assert client.open("/api/cutover-probe", method="OPTIONS").status_code != 503
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous


def test_disabled_cutover_writer_fence_does_not_replace_normal_routing(client):
    previous = _set_fence(client, False)
    try:
        assert client.post("/api/cutover-probe").status_code == 404
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous
