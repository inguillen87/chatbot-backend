import os
from pathlib import Path
import subprocess
import sys


def test_socket_startup_keeps_chat_and_ticket_stacks_lazy():
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["FLASK_SKIP_GLOBAL_APP"] = "1"
    env["FLASK_ENV"] = "production"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env.pop("VERCEL", None)
    env.pop("VERCEL_ENV", None)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import socket_service; "
                "assert 'services.ticket_service' not in sys.modules; "
                "assert 'services.logic' not in sys.modules"
            ),
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
