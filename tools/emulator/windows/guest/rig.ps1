<#
Layer 2, "rig": what the tests need and a person's computer never has -
Remote Desktop, no Windows Update or SConfig, and for the link to the
emulator OpenVPN's TAP driver, the "S60 emulator" adapter and a Python of
its own, all from windows-bootstrap.ps1, as a Windows user of the emulator
gets them. Runs from start.ps1, on layer 1, then shuts Windows down.
#>
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Start-Transcript -Path C:\S60\rig.log | Out-Null
try {
    # The link's Python, apart from the tool's (layer 3, or a run's own with
    # --first-run): for this script only, so the tool never finds it.
    $env:S60_PYTHON_HOME = "C:\S60\rig-python"
    & C:\S60\repo\tools\emulator\windows-bootstrap.ps1 -Yes
    if ($LASTEXITCODE) { throw "windows-bootstrap.ps1 failed (exit $LASTEXITCODE)" }

    Set-LocalUser -Name s60 -PasswordNeverExpires $true
    # No SConfig at logon: it would race the run's window for the keyboard.
    Set-SConfig -AutoLaunch $false
    # Remote Desktop, to work in it from the Linux side (only 127.0.0.1 reaches it).
    Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" -Name fDenyTSConnections -Value 0
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
    # No updates mid-test: each run starts from this disk, so they would never stick anyway.
    $wu = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    New-Item $wu -Force | Out-Null
    Set-ItemProperty $wu -Name NoAutoUpdate -Value 1 -Type DWord

    New-Item C:\S60\rig.done -ItemType File -Force | Out-Null
    Write-Output "Rig ready."
} catch {
    Write-Output "PROVISIONING FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
    shutdown.exe /s /t 5 /c "Rig ready"
}
