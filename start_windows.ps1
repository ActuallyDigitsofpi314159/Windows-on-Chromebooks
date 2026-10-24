$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
             [Environment]::GetEnvironmentVariable('Path', 'User')
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'The host environment (.venv) has not been created. setup_windows.ps1 must finish successfully before starting the host. If setup reported MSI 1625, an installation was blocked; see the Windows setup troubleshooting in README.md.'
}
& $python remote_host.py
