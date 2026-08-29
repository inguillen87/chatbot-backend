from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "deploy_vercel_production_guarded.ps1"
RUNBOOK = ROOT / "docs" / "VERCEL_PRODUCTION_PREDEPLOY_GUARD.md"


def test_wrapper_fails_closed_before_production_deploy() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    approval_check = source.index("if (-not $GuardOnly -and -not $PSCmdlet.ShouldProcess")
    guard_call = source.index("& vercel.cmd @guardArguments")
    guard_exit_check = source.index("if ($guardExitCode -ne 0)")
    checkout_recheck = source.index("$revisionAfterGuard =")
    deploy_call = source.rindex("& vercel.cmd deploy --prod --yes")

    assert approval_check < guard_call < guard_exit_check < checkout_recheck < deploy_call
    assert "throw \"El guard Production bloqueo el deployment.\"" in source
    assert "status --porcelain --untracked-files=normal" in source
    assert "El checkout cambio despues del guard" in source
    assert "[switch]$GuardOnly" in source
    assert "--approved-evidence-sha256" in source
    assert "-e\",\n    \"production\"" in source


def test_documentation_states_raw_cli_bypass_boundary() -> None:
    text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())

    assert "It is not a control imposed by the Vercel platform" in text
    assert "`vercel deploy --prod` executed directly can bypass it" in text
