<#
Layer 3, "tool": what the conversion tool's own first run leaves on a
computer - its portable Python, fetched by tools\windows-python.ps1 just as
start.ps1 fetches it. Kept in C:\S60\python (S60_PYTHON_HOME), as each run's
checkout is new. Runs from start.ps1, then shuts Windows down.

Progress is left on, as in a person's window, and the set-up runs in a
PowerShell of its own that keeps what it reports: a progress bar there - one
a person would see - fails this layer. (A bar on screen never reaches the
transcript, so the log alone would not show it.)
#>
$ErrorActionPreference = "Stop"
Start-Transcript -Path C:\S60\tool.log | Out-Null
try {
    [Environment]::SetEnvironmentVariable("S60_PYTHON_HOME", "C:\S60\python", "Machine")
    $env:S60_PYTHON_HOME = "C:\S60\python"
    # As start.ps1 does it.
    $setup = [powershell]::Create()
    [void]$setup.AddScript('$ErrorActionPreference = "Stop"; . C:\S60\repo\tools\windows-python.ps1; Get-ToolPython')
    try { $python = $setup.Invoke() | Select-Object -Last 1 }
    finally { $setup.Streams.Information | ForEach-Object { Write-Output "$_" } }   # its Write-Host lines
    $bars = @($setup.Streams.Progress | ForEach-Object { $_.Activity } | Select-Object -Unique)
    if ($bars) { throw "setting up the tool's Python showed a progress bar: $($bars -join '; ')" }
    if (-not $python) { throw "no Python was set up" }
    & $python --version
    New-Item C:\S60\tool.done -ItemType File -Force | Out-Null
    Write-Output "Tool ready."
} catch {
    Write-Output "SET-UP FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
    shutdown.exe /s /t 5 /c "Tool ready"
}
