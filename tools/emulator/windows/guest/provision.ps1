<#
Runs once, at the emulator Windows VM's first logon (unattend.xml), then
shuts it down: the disk is then the base every test run starts from.

Installs what the conversion tool needs with its own bootstrap - so that is
tested on a Windows that has none of it - and makes each later logon start
the run (start.ps1).
#>
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Start-Transcript -Path C:\S60\provision.log | Out-Null
try {
    # The portable Python in the base disk itself: each run's checkout is new,
    # and would otherwise download it again.
    [Environment]::SetEnvironmentVariable("S60_PYTHON_HOME", "C:\S60\python", "Machine")
    $env:S60_PYTHON_HOME = "C:\S60\python"
    & C:\S60\repo\tools\emulator\windows-bootstrap.ps1 -Yes
    if ($LASTEXITCODE) { throw "windows-bootstrap.ps1 failed (exit $LASTEXITCODE)" }

    # Each logon starts the run - the link and a window to run the tool in. A
    # task at logon, not the Run key: Server Core has no Explorer to read it.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\S60\start.ps1"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User s60
    $principal = New-ScheduledTaskPrincipal -UserId s60 -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName "S60 run" -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null

    Set-LocalUser -Name s60 -PasswordNeverExpires $true
    # Remote Desktop, to work in it from the Linux side (only 127.0.0.1 reaches it).
    Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" -Name fDenyTSConnections -Value 0
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
    # No updates mid-test: each run starts from this disk, so they would never stick anyway.
    $wu = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    New-Item $wu -Force | Out-Null
    Set-ItemProperty $wu -Name NoAutoUpdate -Value 1 -Type DWord

    New-Item C:\S60\provisioned -ItemType File -Force | Out-Null
    Write-Output "Provisioned."
} catch {
    Write-Output "PROVISIONING FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
    shutdown.exe /s /t 5 /c "Prepared"
}
