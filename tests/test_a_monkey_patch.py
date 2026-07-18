import os
import subprocess
import sys
import unittest

class TestMonkeyPatch(unittest.TestCase):
    def test_monkey_patch(self):
        env = os.environ.copy()
        env["EVENTLET_NO_GREENDNS"] = "YES"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import eventlet; "
                    "eventlet.monkey_patch(); "
                    "from eventlet import patcher; "
                    "assert patcher.is_monkey_patched('thread')"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=result.stderr or result.stdout,
        )
