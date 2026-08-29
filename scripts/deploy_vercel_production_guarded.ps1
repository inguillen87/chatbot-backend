[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)]
    [string]$IdentityEvidence,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ApprovedEvidenceSha256,

    [switch]$GuardOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedEvidence = Resolve-Path -LiteralPath $IdentityEvidence -ErrorAction Stop
if (-not (Test-Path -LiteralPath $resolvedEvidence -PathType Leaf)) {
    throw "La evidencia Neon aprobada no es un archivo regular."
}

$revision = (& git -C $projectRoot rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $revision -notmatch "^[0-9a-f]{40}$") {
    throw "No se pudo resolver la revision Git exacta."
}

# Ask for the destructive Production confirmation before the safety audit so
# there is no unbounded interactive pause between the approved guard and the
# upload. GuardOnly remains non-interactive and never deploys.
if (-not $GuardOnly -and -not $PSCmdlet.ShouldProcess(
    "chatboc-backend Production",
    "vercel deploy --prod --yes"
)) {
    return
}

$guardArguments = @(
    "env",
    "run",
    "-e",
    "production",
    "--project",
    "chatboc-backend",
    "--",
    "python",
    "-m",
    "scripts.audit_vercel_production_predeploy",
    "production",
    "--project-root",
    $projectRoot,
    "--expected-revision",
    $revision,
    "--identity-evidence",
    [string]$resolvedEvidence,
    "--approved-evidence-sha256",
    $ApprovedEvidenceSha256.ToLowerInvariant()
)

Push-Location $projectRoot
try {
    # This command emits a redacted JSON document. Environment values remain
    # inside the child process and are never printed by the audit.
    & vercel.cmd @guardArguments
    $guardExitCode = $LASTEXITCODE
    if ($guardExitCode -ne 0) {
        throw "El guard Production bloqueo el deployment."
    }

    if ($GuardOnly) {
        Write-Host "Guard Production aprobado; no se ejecuto ningun deployment."
        return
    }

    # Minimize the checkout TOCTOU window: the guard already validated the
    # exact clean revision, and we re-check both HEAD and the complete worktree
    # immediately before Vercel packages it.
    $revisionAfterGuard = (& git -C $projectRoot rev-parse HEAD).Trim().ToLowerInvariant()
    $revisionAfterGuardExitCode = $LASTEXITCODE
    $statusAfterGuard = & git -C $projectRoot status --porcelain --untracked-files=normal
    $statusAfterGuardExitCode = $LASTEXITCODE
    $statusAfterGuardText = [string]::Join("`n", @($statusAfterGuard)).Trim()
    if (
        $revisionAfterGuardExitCode -ne 0 -or
        $statusAfterGuardExitCode -ne 0 -or
        $revisionAfterGuard -ne $revision -or
        $statusAfterGuardText
    ) {
        throw "El checkout cambio despues del guard; se cancelo el deployment."
    }

    # Deliberately no arbitrary extra arguments: the audited linked project,
    # clean revision and Production environment must remain the deployed input.
    & vercel.cmd deploy --prod --yes
    if ($LASTEXITCODE -ne 0) {
        throw "Vercel Production deployment fallo despues del guard."
    }
}
finally {
    Pop-Location
}
