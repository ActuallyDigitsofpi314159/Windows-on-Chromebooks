param([switch]$VirtualDisplay)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'Windows Package Manager (winget) is required. Install App Installer from Microsoft, then rerun this script.'
}

$packages = @(
    'Python.Python.3.12',
    'Gyan.FFmpeg',
    'Cloudflare.cloudflared'
)
foreach ($package in $packages) {
    Write-Host "Installing or checking $package..."
    & winget install --exact --id $package --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget could not install $package (exit $LASTEXITCODE)." }
}
if ($VirtualDisplay) {
    Write-Host 'Installing or checking optional Virtual Display Driver...'
    & winget install --exact --id VirtualDrivers.Virtual-Display-Driver --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Virtual Display Driver was not installed by winget. Install it with Virtual Driver Control using the README link.'
    }
}

$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 -m venv .venv
} else {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    )
    $python312 = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $python312) {
        throw 'Python 3.12 was installed, but could not be located. Open a new PowerShell window and rerun setup_windows.ps1.'
    }
    & $python312 -m venv .venv
}
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python 3.12 virtual environment.' }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not update pip.' }
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install the Python requirements.' }

Write-Host ''
Write-Host 'Setup complete. DK Remote uses the primary Windows display by default.'
Write-Host 'Set that display to the receiver screen pixel resolution before connecting.'
if ($VirtualDisplay) {
    Write-Host 'For virtual capture, activate one MttVDD monitor in Virtual Driver Control and choose virtual in the host window.'
} else {
    Write-Host 'Virtual display is optional. To install it later, rerun setup_windows.ps1 -VirtualDisplay.'
}
Write-Host 'Run .\start_windows.ps1 to open the host. See README.md for display instructions.'
