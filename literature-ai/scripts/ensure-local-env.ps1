param(
    [Parameter(Mandatory = $true)]
    [string]$EnvPath,

    [Parameter(Mandatory = $true)]
    [string]$TemplatePath
)

$ErrorActionPreference = "Stop"

function New-UrlSafeSecret {
    param([int]$ByteCount = 32)

    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }

    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Get-EnvValue {
    param(
        [string]$Content,
        [string]$Key
    )

    $pattern = "(?m)^" + [regex]::Escape($Key) + "=(.*)$"
    $match = [regex]::Match($Content, $pattern)
    if (-not $match.Success) {
        return $null
    }

    return $match.Groups[1].Value.Trim()
}

function Set-EnvValue {
    param(
        [string]$Content,
        [string]$Key,
        [string]$Value
    )

    $pattern = "(?m)^" + [regex]::Escape($Key) + "=.*$"
    $replacement = $Key + "=" + $Value
    if ([regex]::IsMatch($Content, $pattern)) {
        return [regex]::Replace($Content, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $replacement })
    }

    if ($Content.Length -gt 0 -and -not $Content.EndsWith("`n")) {
        $Content += [Environment]::NewLine
    }
    return $Content + $replacement + [Environment]::NewLine
}

function Test-NeedsValue {
    param([AllowNull()][string]$Value)

    return [string]::IsNullOrWhiteSpace($Value) -or $Value.StartsWith("replace-with-")
}

if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) {
    throw "Environment template not found: $TemplatePath"
}

$created = -not (Test-Path -LiteralPath $EnvPath -PathType Leaf)
if ($created) {
    $content = [IO.File]::ReadAllText($TemplatePath, [Text.Encoding]::UTF8)
}
else {
    $content = [IO.File]::ReadAllText($EnvPath, [Text.Encoding]::UTF8)
}

$postgresPassword = Get-EnvValue -Content $content -Key "LITAI_POSTGRES_PASSWORD"
if (Test-NeedsValue $postgresPassword) {
    # Preserve the password used by older installations so an existing Docker
    # volume remains accessible after upgrading to the hardened Compose file.
    $databaseUrl = Get-EnvValue -Content $content -Key "LITAI_DATABASE_URL"
    if ($databaseUrl -match '^postgresql(?:\+[^:]+)?://[^:]+:([^@]+)@') {
        $candidate = $Matches[1]
        if (-not (Test-NeedsValue $candidate)) {
            $postgresPassword = $candidate
        }
    }

    if (Test-NeedsValue $postgresPassword) {
        $postgresPassword = New-UrlSafeSecret
    }
    $content = Set-EnvValue -Content $content -Key "LITAI_POSTGRES_PASSWORD" -Value $postgresPassword
}

$requiredSecrets = @(
    "LITAI_MINIO_ACCESS_KEY",
    "LITAI_MINIO_SECRET_KEY",
    "LITAI_OWNER_API_TOKEN"
)

foreach ($key in $requiredSecrets) {
    $value = Get-EnvValue -Content $content -Key $key
    if (Test-NeedsValue $value) {
        $content = Set-EnvValue -Content $content -Key $key -Value (New-UrlSafeSecret)
    }
}

# Keep a template-created host URL consistent with its generated password.
$databaseUrl = Get-EnvValue -Content $content -Key "LITAI_DATABASE_URL"
if ($databaseUrl -and $databaseUrl.Contains("replace-with-a-long-random-password")) {
    $content = $content.Replace("replace-with-a-long-random-password", $postgresPassword)
}

$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($EnvPath, $content, $utf8WithoutBom)

if ($created) {
    Write-Host "Created .env and generated required local secrets."
}
else {
    Write-Host "Checked .env and added any missing required local secrets."
}
