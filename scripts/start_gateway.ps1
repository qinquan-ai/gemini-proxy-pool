param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "venv\Scripts\python.exe"
$healthUrl = "http://${HostAddress}:${Port}/healthz"

try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    if ($health.status -in @("ok", "degraded")) {
        exit 0
    }
} catch {
    # The gateway is offline; continue with startup.
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "StudioKey virtual environment was not found: $python"
}

$logDirectory = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stdoutLog = Join-Path $logDirectory "gateway.stdout.log"
$stderrLog = Join-Path $logDirectory "gateway.stderr.log"

Set-Location $projectRoot
$env:PYTHONUNBUFFERED = "1"
$process = Start-Process `
    -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", $HostAddress, "--port", $Port) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
exit $process.ExitCode
