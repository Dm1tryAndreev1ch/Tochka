$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $python) {
    & $python scripts/launch.py @args
    exit $LASTEXITCODE
}

$py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host 'Install Python 3.12 or 3.13 with the py launcher, or create .venv\Scripts\python.exe. See docs/LAUNCHERS.md.'
    exit 1
}

# Suppress install-on-demand in both current and legacy Python launchers.
$launcherSettings = @{}
foreach ($name in @('PYTHON_MANAGER_AUTOMATIC_INSTALL', 'PYLAUNCHER_ALLOW_INSTALL', 'PYLAUNCHER_ALWAYS_INSTALL')) {
    $launcherSettings[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$exitCode = 1
try {
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'false'
    Remove-Item Env:PYLAUNCHER_ALLOW_INSTALL -ErrorAction SilentlyContinue
    Remove-Item Env:PYLAUNCHER_ALWAYS_INSTALL -ErrorAction SilentlyContinue
    $selectedVersion = $null
    foreach ($version in @('-3.12', '-3.13')) {
        try {
            & $py.Source $version --version *> $null
            if ($LASTEXITCODE -eq 0) {
                $selectedVersion = $version
                break
            }
        } catch {
            # A missing version may raise with native-command error handling enabled.
        }
    }
    if ($selectedVersion) {
        & $py.Source $selectedVersion scripts/launch.py @args
        $exitCode = $LASTEXITCODE
    } else {
        Write-Host 'No installed Python 3.12 or 3.13 found. Install one, then prepare .venv using docs/LAUNCHERS.md.'
    }
} finally {
    foreach ($name in $launcherSettings.Keys) {
        [Environment]::SetEnvironmentVariable($name, $launcherSettings[$name], 'Process')
    }
}
exit $exitCode
