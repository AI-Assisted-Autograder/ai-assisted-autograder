$ErrorActionPreference = 'Stop'
$taskName = 'AI-Assisted-Autograder Nightly Trigger'

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Output "Removed '$taskName' if it existed."
