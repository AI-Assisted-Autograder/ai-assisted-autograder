
BeforeAll {
    $scriptPath = Join-Path $PSScriptRoot '../../../scripts/run-nightly-trigger.ps1'
    $scriptPath = (Resolve-Path $scriptPath).Path

    $testRoot = Join-Path $TestDrive 'project'
    $scriptsDir = Join-Path $testRoot 'scripts'

    New-Item -ItemType Directory -Path $scriptsDir -Force |
        Out-Null

    Copy-Item $scriptPath (Join-Path $scriptsDir 'run-nightly-trigger.ps1')

    $scriptUnderTest = Join-Path $scriptsDir 'run-nightly-trigger.ps1'
}

Describe 'run-nightly-trigger.ps1' {

    BeforeEach {
        $envFile = Join-Path $testRoot '.env.local'
        $logDir = Join-Path $testRoot 'logs'

        Remove-Item $envFile -Force -ErrorAction SilentlyContinue
        Remove-Item $logDir -Recurse -Force -ErrorAction SilentlyContinue

        Remove-Item Env:\TEST_VAR -ErrorAction SilentlyContinue
        Remove-Item Env:\NIGHTLY_TEST_VAR -ErrorAction SilentlyContinue

        Mock Start-Transcript {}
        Mock Stop-Transcript {}
        Mock Push-Location {}
        Mock Pop-Location {}

        # Define npm.cmd as a mock command for macOS.
        function global:npm.cmd {
            $global:LASTEXITCODE = 0
        }
    }

    AfterEach {
        Remove-Item Function:\global:npm.cmd -ErrorAction SilentlyContinue
    }

    It 'throws an error when .env.local is missing' {
        { & $scriptUnderTest } |
            Should -Throw '*Missing*'
    }

    It 'creates the logs directory when .env.local exists' {
        Set-Content $envFile 'TEST_VAR=hello'

        & $scriptUnderTest

        Test-Path $logDir |
            Should -BeTrue
    }

    It 'throws an error when an environment line has no equals sign' {
        Set-Content $envFile 'INVALID_LINE'

        { & $scriptUnderTest } |
            Should -Throw '*Invalid environment line*'
    }

    It 'loads environment variables from .env.local' {
        Set-Content $envFile 'NIGHTLY_TEST_VAR=hello'

        & $scriptUnderTest

        $env:NIGHTLY_TEST_VAR |
            Should -Be 'hello'
    }

    It 'throws when npm exits with a non-zero exit code' {
        Set-Content $envFile 'TEST_VAR=hello'

        function global:npm.cmd {
            $global:LASTEXITCODE = 1
        }

        { & $scriptUnderTest } |
            Should -Throw '*exited with code 1*'
    }

    It 'removes surrounding quotes from environment values' {
        Set-Content $envFile 'NIGHTLY_TEST_VAR="hello world"'

        & $scriptUnderTest

        $env:NIGHTLY_TEST_VAR |
            Should -Be 'hello world'
    }

    It 'ignores comments and blank lines in .env.local' {
        @(
            '# This is a comment'
            ''
            'NIGHTLY_TEST_VAR=hello'
        ) | Set-Content $envFile

        & $scriptUnderTest

        $env:NIGHTLY_TEST_VAR |
            Should -Be 'hello'
    }
}
