from utils.turnstile import turnstile_public_intake_contract, verify_turnstile


def test_verify_turnstile_without_secret_skips_check(app, monkeypatch):
    with app.app_context():
        monkeypatch.delitem(app.config, "CLOUDFLARE_TURNSTILE_SECRET_KEY", raising=False)
        monkeypatch.delitem(app.config, "TURNSTILE_SECRET_KEY", raising=False)
        assert verify_turnstile("dummy") is True


def test_verify_turnstile_posts_siteverify(app, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(app.config, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "turnstile-secret")
        posted = {}

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"success": True}

        def fake_post(url, data, timeout):
            posted["url"] = url
            posted["data"] = data
            posted["timeout"] = timeout
            return Response()

        monkeypatch.setattr("utils.turnstile.requests.post", fake_post)

        assert verify_turnstile("token-123", remote_ip="203.0.113.10", idempotency_key="idem-1") is True
        assert posted["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        assert posted["data"] == {
            "secret": "turnstile-secret",
            "response": "token-123",
            "remoteip": "203.0.113.10",
            "idempotency_key": "idem-1",
        }
        assert posted["timeout"] == 5


def test_turnstile_public_intake_contract_exposes_retry_reset_state(app, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(app.config, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "turnstile-secret")
        monkeypatch.setitem(app.config, "CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "true")

        contract = turnstile_public_intake_contract(
            surface="marketplace_assisted_upload",
            status="verification_failed",
            reason="invalid_or_expired_token",
        )

        assert contract["contract_version"] == "cloudflare.turnstile.public_intake.v1"
        assert contract["provider"] == "cloudflare_turnstile"
        assert contract["surface"] == "marketplace_assisted_upload"
        assert contract["configured"] is True
        assert contract["enforced"] is True
        assert contract["required"] is True
        assert contract["retryable"] is True
        assert contract["reset_required"] is True
        assert contract["token_header"] == "X-Turnstile-Token"
        assert "turnstile_token" in contract["token_fields"]
        assert contract["reason"] == "invalid_or_expired_token"
