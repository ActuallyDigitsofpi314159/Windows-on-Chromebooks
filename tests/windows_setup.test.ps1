$ErrorActionPreference = 'Stop'
$setupPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'setup_windows.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($setupPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors) { throw ($parseErrors | Out-String) }
$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-WingetPackage' }, $false)
. ([scriptblock]::Create($definition.Extent.Text))

# Simulate WinGet's outcomes; these checks never install any software.
function winget {
    $script:receivedArgs = @($args)
    $global:LASTEXITCODE = $script:fakeExit
}
function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

foreach ($code in @(0, -1978335189)) {
    $script:fakeExit = $code
    Install-WingetPackage 'Test.Package'
}
foreach ($code in @(-1978334961, 1625, -2147023271)) {
    $script:fakeExit = $code
    $message = ''
    try { Install-WingetPackage 'Test.Package' } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'Windows Installer policy blocked Test.Package') "Policy error $code was not explained."
}
$script:fakeExit = 123
$message = ''
try { Install-WingetPackage 'Test.Package' } catch { $message = $_.Exception.Message }
Assert-True ($message -match 'exit 123') 'Unexpected installer failures must stop setup.'

$script:fakeExit = 0
$installerArgs = '/passive /norestart InstallAllUsers=0 Include_launcher=0 Include_pip=1 Include_tcltk=1 PrependPath=1'
Install-WingetPackage 'Python.Python.3.12' $installerArgs
Assert-True ($script:receivedArgs -contains '--override') 'Python installer options were not forwarded.'
Assert-True ($script:receivedArgs -contains $installerArgs) 'The installer argument string must be passed intact.'
Assert-True ($script:receivedArgs -contains '--scope') 'Python should use per-user installation scope.'
Write-Host 'Windows setup checks passed (success, already current, policy failures, unexpected failure, installer arguments).'
