param(
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '02:00'
)

$ErrorActionPreference = 'Stop'
$taskName = 'AI-Assisted-Autograder Nightly Trigger'
$runScript = Join-Path $PSScriptRoot 'run-nightly-trigger.ps1'
$time = [DateTime]::Today.Add([TimeSpan]::Parse($At))

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runScript`""
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Runs the database-backed Gradescope nightly trigger at $At local time." `
    -Force | Out-Null

Write-Output "Registered '$taskName' for every day at $At local time."
Write-Output "Run the script again with -At 'HH:mm' to change the time."
