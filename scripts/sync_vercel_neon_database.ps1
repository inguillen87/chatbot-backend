[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Target = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Target -ne "production") {
    throw "Este script requiere el argumento posicional 'production'."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$projectConfigPath = Join-Path $projectRoot ".vercel\project.json"
if (-not (Test-Path -LiteralPath $projectConfigPath)) {
    throw "No se encontro la vinculacion local con Vercel."
}

$projectConfig = Get-Content -Raw -LiteralPath $projectConfigPath | ConvertFrom-Json
if ($projectConfig.projectName -ne "chatboc-backend") {
    throw "Proyecto Vercel rechazado: este script solo admite chatboc-backend."
}

$pooledUrl = [string]$env:NEON_DATABASE_URL
$unpooledUrl = [string]$env:NEON_DATABASE_URL_UNPOOLED
if ([string]::IsNullOrWhiteSpace($pooledUrl)) {
    throw "NEON_DATABASE_URL no esta disponible en Vercel Production."
}
if ([string]::IsNullOrWhiteSpace($unpooledUrl)) {
    throw "NEON_DATABASE_URL_UNPOOLED no esta disponible en Vercel Production."
}

$pooledUri = $null
$unpooledUri = $null
if (-not [Uri]::TryCreate($pooledUrl, [UriKind]::Absolute, [ref]$pooledUri)) {
    throw "NEON_DATABASE_URL no contiene una URL valida."
}
if (-not [Uri]::TryCreate($unpooledUrl, [UriKind]::Absolute, [ref]$unpooledUri)) {
    throw "NEON_DATABASE_URL_UNPOOLED no contiene una URL valida."
}
if ($pooledUri.Scheme -notin @("postgres", "postgresql")) {
    throw "NEON_DATABASE_URL usa un esquema inesperado."
}
if ($unpooledUri.Scheme -notin @("postgres", "postgresql")) {
    throw "NEON_DATABASE_URL_UNPOOLED usa un esquema inesperado."
}
if (-not $pooledUri.Host.EndsWith(".neon.tech", [StringComparison]::OrdinalIgnoreCase)) {
    throw "NEON_DATABASE_URL no pertenece a Neon."
}
if (-not $unpooledUri.Host.EndsWith(".neon.tech", [StringComparison]::OrdinalIgnoreCase)) {
    throw "NEON_DATABASE_URL_UNPOOLED no pertenece a Neon."
}
if ($pooledUri.Host -notmatch "-pooler\.") {
    throw "NEON_DATABASE_URL no es la conexion pooled esperada."
}
if ($unpooledUri.Host -match "-pooler\.") {
    throw "NEON_DATABASE_URL_UNPOOLED no es la conexion directa esperada."
}

function Set-SensitiveVercelEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $Value | & vercel.cmd env add `
        $Name `
        production `
        --force `
        --sensitive `
        --yes
    if ($LASTEXITCODE -ne 0) {
        throw "Vercel no pudo sincronizar $Name."
    }
}

Push-Location $projectRoot
try {
    Set-SensitiveVercelEnvironmentValue -Name "DATABASE_URL" -Value $pooledUrl
    Set-SensitiveVercelEnvironmentValue -Name "SQLALCHEMY_DATABASE_URI" -Value $pooledUrl
    Set-SensitiveVercelEnvironmentValue -Name "ALEMBIC_DB_URL" -Value $unpooledUrl
    Set-SensitiveVercelEnvironmentValue -Name "MIGRATIONS_DATABASE_URL" -Value $unpooledUrl
}
finally {
    Pop-Location
    $pooledUrl = $null
    $unpooledUrl = $null
    $pooledUri = $null
    $unpooledUri = $null
}

Write-Host "Base web y canal de migraciones sincronizados desde Neon sin exponer valores."
