param(
    [string]$Deployment = "chatboc-backend-r2-preview.vercel.app",
    [string]$Origin = "https://chatboc-r2-preview.vercel.app",
    [string]$FilePath = "tests/test_files/dummy.png",
    [string]$TenantSlug = "junin",
    [switch]$AllowDemoAuthMutation
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
$fileSha256 = (Get-FileHash -LiteralPath $resolvedFile -Algorithm SHA256).Hash.ToLowerInvariant()
if ($fileSha256 -ne "0814f352fb86bdaf7e38beefd1272a41090e4e700dcdcbcb41898cdf1f6ee37c") {
    throw "Smoke input must be the approved non-personal canary fixture."
}
$runId = [guid]::NewGuid().ToString("N")
$canaryPrefix = "r2-smoke-canary-$runId"
$canaryFilename = "$canaryPrefix.png"
$sessionId = "r2-smoke-$runId"

$jwt = [string]$env:CHATBOC_SMOKE_BEARER_TOKEN
$tenantSlug = $TenantSlug.Trim().ToLowerInvariant()
$demoLoginUsed = $false
if ([string]::IsNullOrWhiteSpace($jwt)) {
    if (-not $AllowDemoAuthMutation) {
        throw "Set CHATBOC_SMOKE_BEARER_TOKEN or explicitly allow demo auth mutation on an isolated disposable database."
    }
    $login = Invoke-VercelJson -Path "/api/auth/demo" -Body @{
        tenant_slug = $tenantSlug
        sector = "gobierno"
        tipo_chat = "municipio"
    }
    $jwt = [string]$login.token
    $tenantSlug = [string]$login.tenant_slug
    $demoLoginUsed = $true
}
if ([string]::IsNullOrWhiteSpace($jwt) -or [string]::IsNullOrWhiteSpace($tenantSlug)) {
    throw "A scoped smoke identity is required."
}

$authHeaders = @{
    Authorization = "Bearer $jwt"
    "X-Chat-Session-Id" = $sessionId
    "X-Tenant-Slug" = $tenantSlug
    "X-Request-Id" = "$canaryPrefix-request"
}
$intentToken = $null
$cleanupConfirmed = $false
$result = $null
try {
$prepared = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
    operation = "prepare_direct_upload"
    filename = $canaryFilename
    mime_type = "image/png"
    size_bytes = $fileSize
}
$intentToken = [string]$prepared.intent_token
if (
    $prepared.ok -ne $true -or
    $prepared.operation -ne "prepare_direct_upload" -or
    $prepared.upload.method -ne "PUT" -or
    $prepared.constraints.exact_size_required -ne $true -or
    [string]::IsNullOrWhiteSpace($intentToken)
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
    intent_token = $intentToken
}
if (
    $completed.ok -ne $true -or
    $completed.operation -ne "complete_direct_upload" -or
    $completed.attachmentInfo.storage_provider -ne "cloudflare_r2" -or
    [string]$completed.attachmentInfo.name -ne $canaryFilename
) {
    throw "Direct upload completion failed with code: $($completed.code)"
}
$replayed = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
    operation = "complete_direct_upload"
    intent_token = $intentToken
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

$discarded = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
    operation = "discard_direct_upload"
    intent_token = $intentToken
}
if (
    $discarded.ok -ne $true -or
    $discarded.operation -ne "discard_direct_upload" -or
    $discarded.idempotent -ne $false -or
    $discarded.absence_confirmed.database -ne $true -or
    $discarded.absence_confirmed.temporary_object -ne $true -or
    $discarded.absence_confirmed.final_object -ne $true
) {
    throw "Exact canary cleanup was not confirmed."
}
$cleanupConfirmed = $true

$deletedDeliveryProbe = Invoke-HeaderOnlyRequest -Method "GET" -Url $signedDeliveryUrl
if ($deletedDeliveryProbe.status -ne 404) {
    throw "Deleted canary remained readable through its signed URL."
}
$discardReplay = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
    operation = "discard_direct_upload"
    intent_token = $intentToken
}
if (
    $discardReplay.ok -ne $true -or
    $discardReplay.operation -ne "discard_direct_upload" -or
    $discardReplay.idempotent -ne $true -or
    $discardReplay.absence_confirmed.database -ne $true -or
    $discardReplay.absence_confirmed.temporary_object -ne $true -or
    $discardReplay.absence_confirmed.final_object -ne $true
) {
    throw "Canary cleanup replay was not idempotent and absent."
}

$corsOk = @($corsEvidence | Where-Object {
    $_.status -notin @(200, 204) -or
    -not $_.allow_origin_exact -or
    -not $_.allow_put
}).Count -eq 0
$putCorsOriginExact = $put.headers["access-control-allow-origin"] -eq $Origin
$putExposeEtag = [string]$put.headers["access-control-expose-headers"] -match '(^|,\s*)ETag(\s*,|$)'

$result = [pscustomobject]@{
    contract_version = "qa.vercel_r2_direct_upload.v2"
    tenant_slug = $tenantSlug
    authentication_mode = if ($demoLoginUsed) { "isolated_demo_mutation" } else { "existing_scoped_token" }
    canary_prefix = $canaryPrefix
    canary_non_personal = $true
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
    delete_operation = [string]$discarded.operation
    delete_absence_confirmed = $cleanupConfirmed
    deleted_signed_get_status = $deletedDeliveryProbe.status
    delete_replay_idempotent = $discardReplay.idempotent -eq $true
    attachment_cleanup_confirmed = $true
    attachment_cleanup_via_signed_intent = $true
}

if (
    -not $corsOk -or
    $blockedOriginAllowed -or
    -not $putCorsOriginExact -or
    -not $putExposeEtag -or
    $replayed.idempotent -ne $true -or
    $signedDeliveryStatus -ne 200 -or
    $privacyBlocked -ne $true -or
    -not $cleanupConfirmed -or
    $deletedDeliveryProbe.status -ne 404 -or
    $discardReplay.idempotent -ne $true
) {
    throw "R2 direct upload smoke gate failed."
}
} finally {
    if (-not $cleanupConfirmed -and -not [string]::IsNullOrWhiteSpace($intentToken)) {
        $emergencyDiscard = Invoke-VercelJson -Path "/archivos/upload/chat_attachment" -Headers $authHeaders -Body @{
            operation = "discard_direct_upload"
            intent_token = $intentToken
        }
        if (
            $emergencyDiscard.ok -ne $true -or
            $emergencyDiscard.operation -ne "discard_direct_upload" -or
            $emergencyDiscard.absence_confirmed.database -ne $true -or
            $emergencyDiscard.absence_confirmed.temporary_object -ne $true -or
            $emergencyDiscard.absence_confirmed.final_object -ne $true
        ) {
            throw "R2 smoke failed and exact canary cleanup could not be confirmed."
        }
        $cleanupConfirmed = $true
    }
}

$result | ConvertTo-Json -Depth 8
