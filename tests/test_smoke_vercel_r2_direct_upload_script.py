from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_vercel_r2_direct_upload.ps1"


class TestVercelR2DirectUploadSmokeScript(unittest.TestCase):
    def test_canary_lifecycle_is_exact_fail_closed_and_has_valid_powershell(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('canaryPrefix = "r2-smoke-canary-$runId"', source)
        self.assertIn("0814f352fb86bdaf7e38beefd1272a41090e4e700dcdcbcb41898cdf1f6ee37c", source)
        self.assertIn("CHATBOC_SMOKE_BEARER_TOKEN", source)
        self.assertIn("AllowDemoAuthMutation", source)
        self.assertIn('"existing_scoped_token"', source)
        self.assertIn('"isolated_demo_mutation"', source)
        self.assertIn("attachment_cleanup_confirmed", source)
        self.assertIn("attachment_cleanup_via_signed_intent", source)
        self.assertNotIn("exact_cleanup_via_signed_intent", source)
        self.assertGreaterEqual(source.count('operation = "discard_direct_upload"'), 3)
        self.assertGreaterEqual(source.count("intent_token = $intentToken"), 4)
        self.assertIn("absence_confirmed.final_object -ne $true", source)
        self.assertIn("if (-not $cleanupConfirmed", source)
        self.assertIn("$deletedDeliveryProbe.status -ne 404", source)
        self.assertNotIn("delete-objects", source.lower())
        self.assertNotIn("list-objects", source.lower())
        self.assertNotIn("remove-r2-bucket", source.lower())

        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        command = (
            "$tokens=$null; $errors=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
            "[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
        )
        completed = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
