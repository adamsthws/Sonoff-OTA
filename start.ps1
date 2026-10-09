<#
Start the S60 conversion tool on Windows.

    .\start.ps1                 convert a plug (the same as: start.ps1 run)
    .\start.ps1 run --verbose   any of the tool's own commands and options

Or double-click start.cmd. It runs the tool on python.org's portable Python,
unpacked into .python\ in this folder - nothing is installed. Only if that
cannot be used does it fall back to a Python already installed, and only if
there is none does it install one, quietly, for your account; it then offers
to remove that again when the tool exits.
#>
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "tools\windows-python.ps1")

try { $python = Get-ToolPython }
catch {
    Write-Host "No Python could be found or set up: $($_.Exception.Message)"
    Write-Host "Install Python 3.10 or newer yourself (https://www.python.org/downloads/),"
    Write-Host "then run this again."
    exit 1
}

# @(...): an if-expression would unwrap a single option to a bare string,
# which then reaches Python mangled.
$arguments = @(if ($args.Count) { $args } else { "run" })
$code = 1
try {
    & $python (Join-Path $PSScriptRoot "tools\s60_autoflash.py") @arguments
    $code = $LASTEXITCODE
} finally {
    Request-PythonRemoval
}
exit $code
