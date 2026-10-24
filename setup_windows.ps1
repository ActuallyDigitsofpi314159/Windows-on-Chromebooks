param([switch]$VirtualDisplay, [string]$PythonPath)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Test-Python312([string]$Executable, [string[]]$PrefixArgs = @()) {
    try {
        $result = & $Executable @PrefixArgs -I -c 'import struct,sys; sys.exit(1) if sys.version_info[:2] != (3,12) or struct.calcsize(chr(80)) != 8 else print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) {
            $resolved = ([string]($result | Select-Object -Last 1)).Trim()
            if (Test-Path -LiteralPath $resolved -PathType Leaf) { return $resolved }
        }
    } catch {
        # A launcher can exist without a runtime, or point to a removed runtime.
    }
    return $null
}

function Find-Python312 {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $runtime = Test-Python312 $candidate
            if ($runtime) { return $runtime }
        }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $runtime = Test-Python312 'py' @('-3.12')
        if ($runtime) { return $runtime }
    }
    return $null
}

function Install-WingetPackage([string]$Package, [string]$InstallerArguments) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is required to install $Package. Install App Installer from Microsoft, or have the dependency installed before rerunning setup."
    }
    Write-Host "Installing or checking $Package..."
    $installArgs = @('install', '--exact', '--id', $Package, '--accept-package-agreements', '--accept-source-agreements')
    if ($InstallerArguments) { $installArgs += @('--scope', 'user', '--override', $InstallerArguments) }
    & winget @installArgs
    $installExit = $LASTEXITCODE
    # WinGet reports "no applicable update" when the package is already current.
    if ($installExit -eq 0 -or $installExit -eq -1978335189) { return }
    if ($installExit -eq -1978334961 -or $installExit -eq 1625 -or $installExit -eq -2147023271) {
        throw "Windows Installer policy blocked $Package (exit $installExit / MSI 1625). Setup has stopped; .venv is not ready. Check the installer log with the PC administrator. If Python 3.12 (64-bit) is already installed, use -PythonPath with its python.exe."
    }
    throw "winget could not install $Package (exit $installExit). Setup has stopped; rerun it after resolving this installation failure."
}

$env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')
if ($PythonPath) {
    $python312 = Test-Python312 $PythonPath
    if (-not $python312) { throw '-PythonPath must point to a working Python 3.12 (64-bit) python.exe.' }
} else {
    $python312 = Find-Python312
    if (-not $python312) {
        # DK Remote invokes python.exe directly. Do not reconfigure the optional
        # launcher: an existing system launcher may reject a per-user MSI update.
        Install-WingetPackage 'Python.Python.3.12' '/passive /norestart InstallAllUsers=0 Include_launcher=0 Include_pip=1 Include_tcltk=1 PrependPath=1'
        $python312 = Find-Python312
        if (-not $python312) {
            throw 'No working Python 3.12 (64-bit) runtime was found after installation. Open a new PowerShell window, or rerun setup with -PythonPath pointing to its python.exe.'
        }
    }
}
Write-Host "Using Python: $python312"
& $python312 -I -c 'import ensurepip, tkinter, venv'
if ($LASTEXITCODE -ne 0) { throw 'Python needs pip, venv, and Tcl/Tk. Modify the Python installation to include these components, then rerun setup.' }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Install-WingetPackage 'Gyan.FFmpeg' }
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) { Install-WingetPackage 'Cloudflare.cloudflared' }
if ($VirtualDisplay) {
    try { Install-WingetPackage 'VirtualDrivers.Virtual-Display-Driver' } catch {
        Write-Warning "Optional Virtual Display Driver was not installed. $($_.Exception.Message)"
    }
}
$env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
             [Environment]::GetEnvironmentVariable('Path', 'User')

& $python312 -m venv .venv
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
