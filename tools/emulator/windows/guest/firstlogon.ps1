<#
Layer 1, "installed": runs once, at Windows' first logon after Setup
(unattend.xml), then shuts it down. Windows is then as Setup left it, with
only a hook added that runs start.ps1 at each logon - which sets up the layers
above (rig.ps1, tool.ps1) and starts the test runs.
#>
$ErrorActionPreference = "Stop"
Start-Transcript -Path C:\Autoflash\installed.log | Out-Null
try {
    # A task at logon, not the Run key: Server Core has no Explorer to read it.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Autoflash\start.ps1"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User autoflash
    $principal = New-ScheduledTaskPrincipal -UserId autoflash -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName "Autoflash logon" -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
    New-Item C:\Autoflash\installed.done -ItemType File -Force | Out-Null
    Write-Output "Installed."
} catch {
    Write-Output "SET-UP FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
    shutdown.exe /s /t 5 /c "Installed"
}
