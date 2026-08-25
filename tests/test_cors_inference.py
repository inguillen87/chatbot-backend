import os
import importlib
from pathlib import Path
import subprocess
import sys
import textwrap
from unittest import TestCase
from unittest.mock import patch
import config as app_config

class TestCorsInference(TestCase):
    def test_backend_root_domain_added(self):
        env = {
            "BACKEND_URL": "https://api.example.com",
            "ENV": "prod"
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertIn("https://example.com", cfg.ALLOWED_ORIGINS)
            self.assertIn("https://www.example.com", cfg.ALLOWED_ORIGINS)
        importlib.reload(app_config)

    def test_public_root_domain_env(self):
        env = {
            "PUBLIC_ROOT_DOMAIN": "example.org"
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertIn("https://example.org", cfg.ALLOWED_ORIGINS)
            self.assertIn("https://www.example.org", cfg.ALLOWED_ORIGINS)
        importlib.reload(app_config)

    def test_local_dev_origins_allow_dynamic_vite_ports(self):
        with patch.dict(os.environ, {"CORS_ALLOW_LOCAL_DEV": "1"}, clear=True):
            cfg = importlib.reload(app_config)
            origin = "http://127.0.0.1:4194"
            self.assertTrue(
                any(
                    getattr(allowed, "match", None) and allowed.match(origin)
                    for allowed in cfg.ALLOWED_ORIGINS
                ),
                "local Vite preview ports should be accepted by CORS in dev",
            )
        importlib.reload(app_config)

    def test_production_ignores_local_dev_flag_and_wildcard_previews(self):
        env = {
            "ENV": "production",
            "BACKEND_URL": "https://api.chatboc.ar",
            "PUBLIC_ROOT_DOMAIN": "chatboc.ar",
            "CORS_ALLOW_LOCAL_DEV": "1",
            "CORS_ALLOWED_ORIGINS": (
                "https://www.chatboc.ar,"
                "https://chatboc-production.vercel.app"
            ),
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertNotIn(
                "https://chatboc-production.vercel.app",
                cfg.CREDENTIALS_ALLOWED_ORIGINS,
            )
            self.assertIn("https://chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertIn("https://www.chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertFalse(
                any(
                    getattr(allowed, "match", None)
                    and allowed.match("http://localhost:5173")
                    for allowed in cfg.CREDENTIALS_ALLOWED_ORIGINS
                )
            )
            self.assertFalse(
                any(
                    getattr(allowed, "match", None)
                    and allowed.match("https://attacker.vercel.app")
                    for allowed in cfg.CREDENTIALS_ALLOWED_ORIGINS
                )
            )
        importlib.reload(app_config)

    def test_render_service_url_keeps_explicit_public_root_origin(self):
        env = {
            "ENV": "production",
            "BACKEND_URL": "https://chatbot-backend-2e14.onrender.com",
            "PUBLIC_ROOT_DOMAIN": "chatboc.ar",
            "CORS_ALLOWED_ORIGINS": (
                "https://chatboc.ar,"
                "https://www.chatboc.ar,"
                "https://untrusted-preview.vercel.app"
            ),
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertIn("https://chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertIn("https://www.chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertNotIn(
                "https://untrusted-preview.vercel.app",
                cfg.CREDENTIALS_ALLOWED_ORIGINS,
            )
        importlib.reload(app_config)

    def test_wildcard_is_never_a_credentialed_origin(self):
        env = {
            "ENV": "production",
            "BACKEND_URL": "https://api.chatboc.ar",
            "PUBLIC_ROOT_DOMAIN": "chatboc.ar",
            "CORS_ALLOWED_ORIGINS": "*",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertNotIn("*", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertIn("https://chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
        importlib.reload(app_config)

    def test_socket_cors_accepts_only_explicit_exact_https_preview_origin(self):
        env = {
            "ENV": "production",
            "BACKEND_URL": "https://api-preview.chatboc.ar",
            "PUBLIC_ROOT_DOMAIN": "chatboc.ar",
            "SOCKET_CORS_ALLOWED_ORIGINS": (
                "https://chatboc-r2-preview.vercel.app,"
                "https://chatboc-r2-preview.vercel.app/,"
                "https://*.vercel.app,"
                "http://insecure-preview.vercel.app,"
                "https://attacker.vercel.app/path"
            ),
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertIn(
                "https://chatboc-r2-preview.vercel.app",
                cfg.SOCKET_CORS_ALLOWED_ORIGINS,
            )
            self.assertEqual(
                cfg.SOCKET_CORS_ALLOWED_ORIGINS.count(
                    "https://chatboc-r2-preview.vercel.app"
                ),
                1,
            )
            self.assertNotIn("https://*.vercel.app", cfg.SOCKET_CORS_ALLOWED_ORIGINS)
            self.assertNotIn(
                "http://insecure-preview.vercel.app",
                cfg.SOCKET_CORS_ALLOWED_ORIGINS,
            )
            self.assertNotIn(
                "https://attacker.vercel.app/path",
                cfg.SOCKET_CORS_ALLOWED_ORIGINS,
            )
            self.assertNotIn(
                "https://chatboc-r2-preview.vercel.app",
                cfg.CREDENTIALS_ALLOWED_ORIGINS,
            )
        importlib.reload(app_config)

    def test_socketio_engine_accepts_preview_origin_and_rejects_attacker(self):
        """Exercise the Socket.IO object wired by ``socket_service`` in isolation."""

        script = textwrap.dedent(
            """
            from flask import Flask
            import socket_service

            app = Flask("socket-cors-wiring-test")
            app.config["SECRET_KEY"] = "socket-cors-wiring-test-only"
            socket_service.socketio.init_app(app)

            engine_origins = (
                socket_service.socketio.server.eio.cors_allowed_origins or []
            )
            assert "https://chatboc-r2-preview.vercel.app" in engine_origins
            assert "https://attacker.vercel.app" not in engine_origins

            client = app.test_client()
            endpoint = "/api/socket.io/?EIO=4&transport=polling"
            preview = client.get(
                endpoint,
                headers={"Origin": "https://chatboc-r2-preview.vercel.app"},
            )
            attacker = client.get(
                endpoint,
                headers={"Origin": "https://attacker.vercel.app"},
            )
            assert preview.status_code == 200, preview.status_code
            assert attacker.status_code == 400, attacker.status_code
            print("SOCKET_CORS_STATUSES=200,400")
            """
        )
        runtime_env = os.environ.copy()
        runtime_env.update(
            {
                "ENV": "production",
                "BACKEND_URL": "https://api-preview.chatboc.ar",
                "PUBLIC_ROOT_DOMAIN": "chatboc.ar",
                "SOCKETIO_ASYNC_MODE": "threading",
                "SOCKET_CORS_ALLOWED_ORIGINS": (
                    "https://chatboc-r2-preview.vercel.app"
                ),
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=runtime_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        diagnostic_tail = (completed.stdout + completed.stderr)[-2000:]
        self.assertEqual(completed.returncode, 0, diagnostic_tail)
        self.assertIn("SOCKET_CORS_STATUSES=200,400", completed.stdout)
