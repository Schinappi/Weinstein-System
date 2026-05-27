param(
    [string]$TaskName = "WinstanNightlyUpdate",
    [string]$PythonExe = "python",
    [string]$ConfigPath = "config/strategy.yaml",
    [string]$Time = "17:00",
    [switch]$IncludeMissing,
    [switch]$SkipIndex,
    [switch]$SkipPhase1,
    [switch]$SkipNonTradingDay = $true
)

$ErrorActionPreference = "Stop"

$RunnerPath = Join-Path $PSScriptRoot "run_nightly_update.ps1"
if (-not (Test-Path $RunnerPath)) {
    throw "Cannot find run_nightly_update.ps1 at $RunnerPath"
}

$ArgumentParts = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"' + $RunnerPath + '"'),
    "-PythonExe", ('"' + $PythonExe + '"'),
    "-ConfigPath", ('"' + $ConfigPath + '"')
)
if ($IncludeMissing) {
    $ArgumentParts += "-IncludeMissing"
}
if ($SkipIndex) {
    $ArgumentParts += "-SkipIndex"
}
if ($SkipPhase1) {
    $ArgumentParts += "-SkipPhase1"
}
if ($SkipNonTradingDay) {
    $ArgumentParts += "-SkipNonTradingDay"
}
$TaskArguments = $ArgumentParts -join ' '

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $TaskArguments -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At ([datetime]::Parse($Time))
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Nightly incremental market data update for 温斯坦 project" -Force | Out-Null
Write-Host "Scheduled task '$TaskName' registered at $Time on weekdays."
if ($SkipNonTradingDay) {
    Write-Host "The runner will skip execution automatically on non-trading days."
}
Write-Host "To run immediately: Start-ScheduledTask -TaskName '$TaskName'"
