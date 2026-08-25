param(
    [string]$Deployment = "chatboc-backend-r2-preview.vercel.app",
    [string]$Origin = "https://chatboc-r2-preview.vercel.app",
    [string]$FilePath = "tests/test_files/dummy.png"
)

$ErrorActionPreference = "Stop"

function Invoke-VercelJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Body,
        [hashtable]$Headers = @{}
    )

    $arguments = @(
        "curl",
        $Path,
        "--deployment",
        $Deployment,
        "--",
        "--silent",
        "--show-error",
        "--request",
        "POST",
        "--header",
        "Content-Type: application/json",
        "--data-binary",
        "@-"
    )
    foreach ($entry in $Headers.GetEnumerator()) {
        $arguments += @("--header", "$($entry.Key): $($entry.Value)")
    }

    $requestBody = $Body | ConvertTo-Json -Compress -Depth 8
    $previousErrorActionPreference = $ErrorActionPreference
    $commandExitCode = 1
    try {
        $ErrorActionPreference = "Continue"
        $raw = $requestBody | & vercel.cmd @arguments 2>$null
        $commandExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($commandExitCode -ne 0) {
        throw "Vercel request failed for $Path with exit code $commandExitCode."
    }
    $jsonText = (@($raw) -join "`n").Trim()
    if (-not $jsonText.StartsWith("{")) {
        throw "Vercel request did not return JSON for $Path."
    }
    return $jsonText | ConvertFrom-Json
}

function Invoke-HeaderOnlyRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Url,
        [string[]]$Headers = @(),
        [string]$UploadFile
    )

    $arguments = @(
        "--silent",
        "--show-error",
        "--request",
        $Method,
        "--dump-header",
        "-",
        "--output",
        "NUL"
    )
    foreach ($header in $Headers) {
        $arguments += @("--header", $header)
    }
    if ($UploadFile) {
        $arguments += @("--upload-file", $UploadFile)
    }
    $arguments += $Url

    $raw = & curl.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "HTTP $Method failed before a response was received."
    }
    $lines = @($raw)
    $statusLine = $lines | Where-Object { $_ -match '^HTTP/\S+\s+\d{3}' } | Select-Object -Last 1
    if (-not $statusLine) {
        throw "HTTP $Method did not return a status line."
    }
    $status = [int]([regex]::Match($statusLine, '\s(?<status>\d{3})\s').Groups['status'].Value)
    $responseHeaders = @{}
    foreach ($line in $lines) {
        if ($line -match '^(?<name>[A-Za-z0-9-]+):\s*(?<value>.*)$') {
            $responseHeaders[$matches['name'].ToLowerInvariant()] = $matches['value'].Trim()
        }
    }
    return [pscustomobject]@{
        status = $status
        headers = $responseHeaders
    }
}

if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    throw "Smoke file is missing."
}
$resolvedFile = (Resolve-Path -LiteralPath $FilePath).Path
$fileSize = (Get-Item -LiteralPath $resolvedFile).Length
$sessionId = [guid]::NewGuid().ToString()

$login = Invoke-VercelJson -Path "/api/auth/demo" -Body @{
    tenant_slug = "junin"
    sector = "gobierno"
    tipo_chat = "municipio"
}
$jwt = [string]$login.token
$tenantSlug = [string]$login.tenant_slug
if ([string]::IsNullOrWhiteSpace($jwt) -or [string]::IsNullOrWhiteSpace($tenantSlug)) {
    throw "Demo login did not return a scoped session."
}

$authHeaders = @{
    Authorization = "Bearer $jwt"
    "X-Chat-Session-Id" = $sessionId
    "X-Tenant-Slug" = $tenantSlug
    "X-Request-Id" = "r2-preview-smoke"
}
$prepared = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
    operation = "prepare_direct_upload"
    filename = "r2-preview-smoke.png"
    mime_type = "image/png"
    size_bytes = $fileSize
}
if (
    $prepared.ok -ne $true -or
    $prepared.operation -ne "prepare_direct_upload" -or
    $prepared.upload.method -ne "PUT" -or
    $prepared.constraints.exact_size_required -ne $true
) {
    throw "Direct upload preparation failed with code: $($prepared.code)"
}
$uploadUrl = [string]$prepared.upload.url
if (-not $uploadUrl.StartsWith("https://")) {
    throw "Direct upload preparation did not return an HTTPS URL."
}

$allowedOrigins = @(
    "https://chatboc.ar",
    "https://www.chatboc.ar",
    $Origin
)
$corsEvidence = @()
foreach ($allowedOrigin in $allowedOrigins) {
    $preflight = Invoke-HeaderOnlyRequest -Method "OPTIONS" -Url $uploadUrl -Headers @(
        "Origin: $allowedOrigin",
        "Access-Control-Request-Method: PUT",
        "Access-Control-Request-Headers: content-type"
    )
    $corsEvidence += [pscustomobject]@{
        origin = $allowedOrigin
        status = $preflight.status
        allow_origin_exact = $preflight.headers["access-control-allow-origin"] -eq $allowedOrigin
        allow_put = [string]$preflight.headers["access-control-allow-methods"] -match '(^|,\s*)PUT(\s*,|$)'
    }
}
$blockedOrigin = "https://malicious.invalid"
$blockedPreflight = Invoke-HeaderOnlyRequest -Method "OPTIONS" -Url $uploadUrl -Headers @(
    "Origin: $blockedOrigin",
    "Access-Control-Request-Method: PUT",
    "Access-Control-Request-Headers: content-type"
)
$blockedOriginAllowed = $blockedPreflight.headers["access-control-allow-origin"] -eq $blockedOrigin

$put = Invoke-HeaderOnlyRequest -Method "PUT" -Url $uploadUrl -UploadFile $resolvedFile -Headers @(
    "Origin: $Origin",
    "Content-Type: image/png"
)
if ($put.status -notin @(200, 201, 204)) {
    throw "R2 PUT failed with HTTP $($put.status)."
}

$completed = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
    operation = "complete_direct_upload"
    intent_token = [string]$prepared.intent_token
}
if (
    $completed.ok -ne $true -or
    $completed.operation -ne "complete_direct_upload" -or
    $completed.attachmentInfo.storage_provider -ne "cloudflare_r2"
) {
    throw "Direct upload completion failed with code: $($completed.code)"
}
$replayed = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
    operation = "complete_direct_upload"
    intent_token = [string]$prepared.intent_token
}

$storageKey = [string]$completed.attachmentInfo.storage_url
$signedDeliveryUrl = [string]$completed.attachmentInfo.url
$signedDeliveryStatus = $null
if ($signedDeliveryUrl.StartsWith("https://")) {
    $signedDeliveryProbe = Invoke-HeaderOnlyRequest -Method "GET" -Url $signedDeliveryUrl
    $signedDeliveryStatus = $signedDeliveryProbe.status
}
$privacyStatus = $null
$privacyBlocked = $null
if ($storageKey -and $storageKey -notmatch '^https?://') {
    $rawPublicUrl = "https://cdn.chatboc.ar/$($storageKey.TrimStart('/'))"
    $privacyProbe = Invoke-HeaderOnlyRequest -Method "GET" -Url $rawPublicUrl
    $privacyStatus = $privacyProbe.status
    $privacyBlocked = $privacyProbe.status -in @(401, 403, 404)
}

$corsOk = @($corsEvidence | Where-Object {
    $_.status -notin @(200, 204) -or
    -not $_.allow_origin_exact -or
    -not $_.allow_put
}).Count -eq 0
$putCorsOriginExact = $put.headers["access-control-allow-origin"] -eq $Origin
$putExposeEtag = [string]$put.headers["access-control-expose-headers"] -match '(^|,\s*)ETag(\s*,|$)'

[pscustomobject]@{
    contract_version = "qa.vercel_r2_direct_upload.v1"
    tenant_slug = $tenantSlug
    file_size = $fileSize
    prepare_ok = $prepared.ok -eq $true
    cors_allowed_origins = $corsEvidence
    cors_allowed_origins_verified = $corsOk
    malicious_origin_status = $blockedPreflight.status
    malicious_origin_allowed = $blockedOriginAllowed
    put_status = $put.status
    put_etag_present = -not [string]::IsNullOrWhiteSpace([string]$put.headers["etag"])
    put_cors_origin_exact = $putCorsOriginExact
    put_expose_etag = $putExposeEtag
    complete_ok = $completed.ok -eq $true
    storage_provider = [string]$completed.attachmentInfo.storage_provider
    storage_access = [string]$completed.attachmentInfo.storage_access
    is_private = $completed.attachmentInfo.is_private -eq $true
    idempotent_replay = $replayed.idempotent -eq $true
    signed_delivery_status = $signedDeliveryStatus
    raw_public_probe_status = $privacyStatus
    raw_public_access_blocked = $privacyBlocked
} | ConvertTo-Json -Depth 8

if (
    -not $corsOk -or
    $blockedOriginAllowed -or
    -not $putCorsOriginExact -or
    -not $putExposeEtag -or
    $replayed.idempotent -ne $true -or
    $signedDeliveryStatus -ne 200 -or
    $privacyBlocked -ne $true
) {
    exit 2
}
