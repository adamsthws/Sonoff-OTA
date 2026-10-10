<#
Layer 3, "tool": what the conversion tool's own first run leaves on a
computer - its portable Python, fetched by tools\windows-python.ps1 just as
start.ps1 fetches it. Kept in C:\S60\python (S60_PYTHON_HOME), as each run's
checkout is new. Runs from start.ps1, then shuts Windows down.
#>
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Start-Transcript -Path C:\S60\tool.log | Out-Null
try {
    [Environment]::SetEnvironmentVariable("S60_PYTHON_HOME", "C:\S60\python", "Machine")
    $env:S60_PYTHON_HOME = "C:\S60\python"
    . C:\S60\repo\tools\windows-python.ps1
    $python = Get-ToolPython
    & $python --version
    New-Item C:\S60\tool.done -ItemType File -Force | Out-Null
    Write-Output "Tool ready."
} catch {
    Write-Output "SET-UP FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
    shutdown.exe /s /t 5 /c "Tool ready"
}
