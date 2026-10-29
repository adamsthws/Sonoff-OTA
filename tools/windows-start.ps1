<#
Start Autoflash, the conversion tool, on Windows - what start.cmd in the
repository's root runs, from a double-click or a terminal:

    .\start.cmd                 convert a plug (the same as: start.cmd run)
    .\start.cmd run --verbose   any of the tool's own commands and options

It runs the tool on python.org's portable Python, unpacked into .python\ in
the repository - nothing is installed. Only if that cannot be used does it
fall back to a Python already installed, and only if there is none does it
install one, quietly, for your account; it then offers to remove that again
when the tool exits.
#>
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "windows-python.ps1")
$Root = Split-Path $PSScriptRoot

# Opened by a double-click (Explorer started the cmd that started this), the
# window would close before the last words are read, so wait for a key first.
# From a terminal the words stay on screen, and a key would only be in the way.
function Test-DoubleClicked {
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
        $launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $($me.ParentProcessId)"
        $opener = Get-CimInstance Win32_Process -Filter "ProcessId = $($launcher.ParentProcessId)"
        return $opener.Name -eq "explorer.exe"
    } catch { return $false }
}
function Stop-Tool([int] $Code) {
    if (Test-DoubleClicked) {
        Write-Host ""
        Write-Host " Press any key to continue . . ."   # set in, as the tool's words are
        [void][Console]::ReadKey($true)
    }
    exit $Code
}

try { $python = Get-ToolPython }
catch {
    Write-Host " No Python could be found or set up: $($_.Exception.Message)"
    Write-Host " Install Python 3.10 or newer yourself (https://www.python.org/downloads/),"
    Write-Host " then run this again."
    Stop-Tool 1
}

# @(...): an if-expression would unwrap a single option to a bare string,
# which then reaches Python mangled.
$arguments = @(if ($args.Count) { $args } else { "run" })
$code = 1
try {
    & $python (Join-Path $Root "tools\autoflash\cli.py") @arguments
    $code = $LASTEXITCODE
} finally {
    Request-PythonRemoval
}
Stop-Tool $code
