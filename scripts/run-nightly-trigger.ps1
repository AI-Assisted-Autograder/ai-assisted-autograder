$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repoRoot '.env.local'
$logDirectory = Join-Path $repoRoot 'logs'
$logFile = Join-Path $logDirectory 'nightly-trigger.log'

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing $envFile. Copy .env.local.example to .env.local and fill in the local secrets."
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

Get-Content -LiteralPath $envFile | ForEach-Object {
    $line = $_.Trim()
    if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#')) {
        return
    }

    $parts = $line -split '=', 2
    if ($parts.Count -ne 2) {
        throw "Invalid environment line in $envFile: $line"
    }

    $name = $parts[0].Trim()
    $value = $parts[1].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}

Start-Transcript -LiteralPath $logFile -Append | Out-Null
try {
    Push-Location $repoRoot
    & npm.cmd run nightly:trigger
    if ($LASTEXITCODE -ne 0) {
        throw "nightly:trigger exited with code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
    Stop-Transcript | Out-Null
}
