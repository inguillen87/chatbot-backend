import os
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class TestNativeThreadRuntime(unittest.TestCase):
    repo_root = Path(__file__).resolve().parents[1]

    def test_importing_app_keeps_standard_library_unpatched(self):
        env = os.environ.copy()
        env.pop("EVENTLET_NO_GREENDNS", None)
        env.pop("TESTING", None)
        env["FLASK_SKIP_GLOBAL_APP"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import socket, sys, threading; "
                    "native_socket = socket.socket; native_thread = threading.Thread; "
                    "import app; "
                    "assert socket.socket is native_socket; "
                    "assert threading.Thread is native_thread; "
                    "assert 'eventlet' not in sys.modules"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=result.stderr or result.stdout,
        )

    def test_socketio_runtime_defaults_to_threading(self):
        from socket_service import _resolve_socket_async_mode

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_resolve_socket_async_mode(), "threading")

    def test_socketio_runtime_rejects_green_thread_override(self):
        from socket_service import _resolve_socket_async_mode

        with patch.dict(os.environ, {"SOCKETIO_ASYNC_MODE": "eventlet"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must be 'threading'"):
                _resolve_socket_async_mode()

    def test_deployment_and_dependency_manifests_are_native_thread_only(self):
        app_source = (self.repo_root / "app.py").read_text(encoding="utf-8")
        gunicorn_source = (self.repo_root / "gunicorn.conf.py").read_text(
            encoding="utf-8"
        )
        render_source = (self.repo_root / "render.yaml").read_text(encoding="utf-8")

        self.assertNotIn("monkey_patch", app_source)
        self.assertIn('worker_class = "gthread"', gunicorn_source)
        self.assertIn("--config gunicorn.conf.py", render_source)
        self.assertNotIn("--worker-class eventlet", render_source)

        for filename in ("requirements.txt", "requirements_fixed.txt"):
            manifest = (self.repo_root / filename).read_text(encoding="utf-8")
            self.assertNotRegex(manifest, r"(?m)^eventlet(?:\[.*\])?==")
            self.assertIn("simple-websocket==1.1.0", manifest)

    def test_gunicorn_rejects_multiple_workers_in_one_instance(self):
        config_path = self.repo_root / "gunicorn.conf.py"

        with patch.dict(os.environ, {"GUNICORN_WORKERS": "2"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "must be 1"):
                runpy.run_path(str(config_path))

        with patch.dict(os.environ, {"GUNICORN_WORKERS": "1"}, clear=False):
            config = runpy.run_path(str(config_path))

        self.assertEqual(config["workers"], 1)
        self.assertEqual(config["worker_class"], "gthread")

    def test_linux_websocket_gate_is_hash_locked_and_least_privileged(self):
        lock_path = (
            self.repo_root
            / ".github"
            / "requirements"
            / "native-thread-runtime-linux-py312.txt"
        )
        workflow_path = (
            self.repo_root / ".github" / "workflows" / "native-thread-runtime.yml"
        )
        lock = lock_path.read_text(encoding="utf-8")
        workflow = workflow_path.read_text(encoding="utf-8")

        self.assertNotRegex(lock, r"(?mi)^eventlet(?:\[.*\])?==")
        self.assertIn("simple-websocket==1.1.0", lock)
        self.assertIn("gunicorn==22.0.0", lock)
        self.assertNotIn("SECRET_KEY", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertIn('transports=["websocket"]', workflow)
        self.assertIn('client.transport() != "websocket"', workflow)


if __name__ == "__main__":
    unittest.main()
