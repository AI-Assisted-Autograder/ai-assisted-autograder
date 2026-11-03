
BeforeAll {
    $scriptPath = Join-Path $PSScriptRoot '../../../scripts/register-nightly-trigger.ps1'
    $scriptPath = (Resolve-Path $scriptPath).Path

    $testRoot = Join-Path $TestDrive 'project'
    $scriptsDir = Join-Path $testRoot 'scripts'

    New-Item -ItemType Directory -Path $scriptsDir -Force |
        Out-Null

    Copy-Item $scriptPath (Join-Path $scriptsDir 'register-nightly-trigger.ps1')

    $scriptUnderTest = Join-Path $scriptsDir 'register-nightly-trigger.ps1'

    # Define Windows-only commands for macOS.
    function global:New-ScheduledTaskAction {}
    function global:New-ScheduledTaskTrigger {}
    function global:New-ScheduledTaskSettingsSet {}
    function global:Register-ScheduledTask {}
}

Describe 'register-nightly-trigger.ps1' {

    BeforeEach {
        $global:scheduledAction = $null
        $global:scheduledTrigger = $null
        $global:scheduledSettings = $null
        $global:registeredTask = $null

        Mock New-ScheduledTaskAction {
            param(
                [string]$Execute,
                [string]$Argument
            )

            $global:scheduledAction = @{
                Execute = $Execute
                Argument = $Argument
            }

            return 'MockAction'
        }

        Mock New-ScheduledTaskTrigger {
            param(
                [switch]$Daily,
                [datetime]$At
            )

            $global:scheduledTrigger = @{
                Daily = $Daily.IsPresent
                At = $At
            }

            return 'MockTrigger'
        }

        Mock New-ScheduledTaskSettingsSet {
            param(
                [switch]$StartWhenAvailable,
                [TimeSpan]$ExecutionTimeLimit
            )

            $global:scheduledSettings = @{
                StartWhenAvailable = $StartWhenAvailable.IsPresent
                ExecutionTimeLimit = $ExecutionTimeLimit
            }

            return 'MockSettings'
        }

        Mock Register-ScheduledTask {
            param(
                [string]$TaskName,
                $Action,
                $Trigger,
                $Settings,
                [string]$Description,
                [switch]$Force
            )

            $global:registeredTask = @{
                TaskName = $TaskName
                Action = $Action
                Trigger = $Trigger
                Settings = $Settings
                Description = $Description
                Force = $Force.IsPresent
            }
        }

        Mock Write-Output {}
    }

    It 'uses 02:00 as the default scheduled time' {
        & $scriptUnderTest

        $global:scheduledTrigger.At.Hour | Should -Be 2
        $global:scheduledTrigger.At.Minute | Should -Be 0
    }

    It 'accepts a custom scheduled time' {
        & $scriptUnderTest -At '04:30'

        $global:scheduledTrigger.At.Hour | Should -Be 4
        $global:scheduledTrigger.At.Minute | Should -Be 30
    }

    It 'rejects an invalid time format' {
        { & $scriptUnderTest -At '25:90' } |
            Should -Throw
    }

    It 'rejects a time without leading zeros' {
        { & $scriptUnderTest -At '2:00' } |
            Should -Throw
    }

    It 'registers the expected scheduled task name' {
        & $scriptUnderTest

        $global:registeredTask.TaskName |
            Should -Be 'AI-Assisted-Autograder Nightly Trigger'
    }

    It 'creates a daily scheduled task' {
        & $scriptUnderTest

        $global:scheduledTrigger.Daily |
            Should -BeTrue
    }

    It 'sets a two-hour execution limit' {
        & $scriptUnderTest

        $global:scheduledSettings.ExecutionTimeLimit |
            Should -Be ([TimeSpan]::FromHours(2))
    }

    It 'enables StartWhenAvailable' {
        & $scriptUnderTest

        $global:scheduledSettings.StartWhenAvailable |
            Should -BeTrue
    }

    It 'registers the task with force enabled' {
        & $scriptUnderTest

        $global:registeredTask.Force |
            Should -BeTrue
    }

    It 'uses the run-nightly-trigger.ps1 script as the scheduled action' {
        & $scriptUnderTest

        $global:scheduledAction.Execute |
            Should -Be 'powershell.exe'

        $global:scheduledAction.Argument |
            Should -Match 'run-nightly-trigger\.ps1'
    }

    It 'sets the task description' {
        & $scriptUnderTest

        $global:registeredTask.Description |
            Should -Match 'database-backed Gradescope nightly trigger'
    }
}

AfterAll {
    Remove-Item Function:\global:New-ScheduledTaskAction -ErrorAction SilentlyContinue
    Remove-Item Function:\global:New-ScheduledTaskTrigger -ErrorAction SilentlyContinue
    Remove-Item Function:\global:New-ScheduledTaskSettingsSet -ErrorAction SilentlyContinue
    Remove-Item Function:\global:Register-ScheduledTask -ErrorAction SilentlyContinue
}


