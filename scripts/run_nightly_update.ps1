param(
    [string]$PythonExe = "python",
    [string]$ConfigPath = "config/strategy.yaml",
    [switch]$IncludeMissing,
    [switch]$SkipIndex,
    [switch]$SkipPhase1,
    [switch]$SkipNonTradingDay = $true
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "nightly-update-$Timestamp.log"
$ScriptPath = Join-Path $PSScriptRoot "update_daily_bars.py"

$Arguments = @("-u", $ScriptPath, "--config", $ConfigPath)
if ($IncludeMissing) {
    $Arguments += "--include-missing"
}
if ($SkipIndex) {
    $Arguments += "--skip-index"
}
if ($SkipPhase1) {
    $Arguments += "--skip-phase1"
}
if ($SkipNonTradingDay) {
    $Arguments += "--skip-non-trading-day"
}

$StartedAt = Get-Date
"[$StartedAt] Starting nightly update..." | Tee-Object -FilePath $LogPath -Append
"Command: $PythonExe $($Arguments -join ' ')" | Tee-Object -FilePath $LogPath -Append

& $PythonExe @Arguments 2>&1 | Tee-Object -FilePath $LogPath -Append
$ExitCode = $LASTEXITCODE

$FinishedAt = Get-Date
$Duration = New-TimeSpan -Start $StartedAt -End $FinishedAt
"[$FinishedAt] Finished nightly update. ExitCode=$ExitCode Duration=$($Duration.ToString())" | Tee-Object -FilePath $LogPath -Append

exit $ExitCode
