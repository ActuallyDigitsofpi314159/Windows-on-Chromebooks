$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
             [Environment]::GetEnvironmentVariable('Path', 'User')
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run .\setup_windows.ps1 first.' }
& $python remote_host.py
