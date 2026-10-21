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

$redisUrl = [string]$env:REDIS_URL
if ([string]::IsNullOrWhiteSpace($redisUrl)) {
    throw "REDIS_URL no esta disponible en el entorno Vercel Production."
}
if ($redisUrl -notmatch "^rediss?://") {
    throw "REDIS_URL usa un esquema inesperado."
}

Push-Location $projectRoot
try {
    $redisUrl | & vercel env add `
        SOCKETIO_MESSAGE_QUEUE_URL `
        production `
        --force `
        --sensitive `
        --yes
    if ($LASTEXITCODE -ne 0) {
        throw "Vercel no pudo sincronizar SOCKETIO_MESSAGE_QUEUE_URL."
    }
}
finally {
    Pop-Location
    $redisUrl = $null
}

Write-Host "SOCKETIO_MESSAGE_QUEUE_URL quedo sincronizada desde REDIS_URL sin exponer su valor."
