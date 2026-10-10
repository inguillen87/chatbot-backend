[CmdletBinding()]
param(
    [ValidateRange(0.1, 240.0)]
    [double]$DurationMinutes = 120,

    [ValidateRange(10, 25)]
    [int]$IntervalSeconds = 20,

    [ValidateRange(5, 30)]
    [int]$RequestTimeoutSeconds = 20,

    [ValidateRange(0, 100000)]
    [int]$MaxRequests = 0,

    [string]$Uri = "https://api-preview.chatboc.ar/api/health"
)

$ErrorActionPreference = "Stop"

function Assert-PreviewHealthUri {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    $parsed = $null
    if (-not [System.Uri]::TryCreate($Candidate, [System.UriKind]::Absolute, [ref]$parsed)) {
        throw "La URL de calentamiento no es valida."
    }

    $allowedHosts = @("api-preview.chatboc.ar")
    if ($parsed.Scheme -ne "https") {
        throw "El calentamiento solo admite HTTPS."
    }
    if ($allowedHosts -notcontains $parsed.DnsSafeHost.ToLowerInvariant()) {
        throw "Host rechazado: este script solo puede llamar al backend Preview autorizado."
    }
    if ($parsed.AbsolutePath.TrimEnd("/") -ne "/api/health" -or $parsed.Query -or $parsed.Fragment) {
        throw "Ruta rechazada: solo se admite GET /api/health sin query ni fragmento."
    }

    return $parsed
}

$target = Assert-PreviewHealthUri -Candidate $Uri
$startedAt = [System.DateTimeOffset]::UtcNow
$deadline = $startedAt.AddMinutes($DurationMinutes)
$attempts = 0
$successes = 0
$failures = 0
$durationsMs = [System.Collections.Generic.List[double]]::new()

Write-Host (
    "Calentamiento Preview iniciado: {0} durante {1:N1} minutos, cada {2}s. Solo GET; Produccion queda fuera de alcance." -f `
        $target.AbsoluteUri,
        $DurationMinutes,
        $IntervalSeconds
)

while ([System.DateTimeOffset]::UtcNow -lt $deadline) {
    if ($MaxRequests -gt 0 -and $attempts -ge $MaxRequests) {
        break
    }

    $attempts += 1
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-WebRequest `
            -Uri $target.AbsoluteUri `
            -Method Get `
            -UseBasicParsing `
            -TimeoutSec $RequestTimeoutSeconds
        $stopwatch.Stop()

        if ([int]$response.StatusCode -ne 200) {
            throw "HTTP $([int]$response.StatusCode)"
        }

        $successes += 1
        $durationsMs.Add($stopwatch.Elapsed.TotalMilliseconds)
        Write-Host (
            "[{0}] OK {1}ms (solicitud {2})" -f `
                [System.DateTimeOffset]::Now.ToString("HH:mm:ss"),
                [Math]::Round($stopwatch.Elapsed.TotalMilliseconds),
                $attempts
        )
    }
    catch {
        $stopwatch.Stop()
        $failures += 1
        Write-Warning (
            "[{0}] fallo de calentamiento tras {1}ms: {2}" -f `
                [System.DateTimeOffset]::Now.ToString("HH:mm:ss"),
                [Math]::Round($stopwatch.Elapsed.TotalMilliseconds),
                $_.Exception.Message
        )
    }

    if ([System.DateTimeOffset]::UtcNow -ge $deadline) {
        break
    }
    if ($MaxRequests -gt 0 -and $attempts -ge $MaxRequests) {
        break
    }

    Start-Sleep -Seconds $IntervalSeconds
}

$averageMs = 0
if ($durationsMs.Count -gt 0) {
    $averageMs = [Math]::Round(($durationsMs | Measure-Object -Average).Average)
}

Write-Host (
    "Resumen: {0} solicitudes, {1} exitosas, {2} fallidas, promedio exitoso {3}ms." -f `
        $attempts,
        $successes,
        $failures,
        $averageMs
)

if ($successes -eq 0) {
    throw "El backend Preview no respondio correctamente durante el calentamiento."
}
