<#
Layer 2, "rig": what the tests need and a person's computer never has -
Remote Desktop, SSH, the clipboard over VNC, no Windows Update or SConfig, and for
the link to the emulator OpenVPN's TAP driver, the "Autoflash emulator" adapter and
a Python of its own, all from windows-bootstrap.ps1, as a Windows user of the
emulator gets them. Runs from start.ps1, on layer 1, then shuts Windows down.
#>
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Start-Transcript -Path C:\Autoflash\rig.log | Out-Null
try {
    # The link's Python, apart from the tool's (layer 3, or a run's own with
    # --first-run): for this script only, so the tool never finds it.
    $env:AUTOFLASH_PYTHON_HOME = "C:\Autoflash\rig-python"
    & C:\Autoflash\repo\tools\emulator\windows-bootstrap.ps1 -Yes
    if ($LASTEXITCODE) { throw "windows-bootstrap.ps1 failed (exit $LASTEXITCODE)" }

    Set-LocalUser -Name autoflash -PasswordNeverExpires $true
    # No SConfig at logon: it would race the run's window for the keyboard.
    Set-SConfig -AutoLaunch $false
    # Remote Desktop, to work in it from the Linux side (only 127.0.0.1 reaches it).
    Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" -Name fDenyTSConnections -Value 0
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
    # SSH, to drive a run and read its log from the Linux side (windows-ssh.sh;
    # only 127.0.0.1 reaches it). Server 2025 has OpenSSH's server, but off.
    if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
    }
    Set-Service sshd -StartupType Automatic
    Start-Service sshd  # its host keys made now, so each run's are the same
    New-Item HKLM:\SOFTWARE\OpenSSH -Force | Out-Null
    Set-ItemProperty HKLM:\SOFTWARE\OpenSSH -Name DefaultShell `
        -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Get-NetFirewallRule -Name OpenSSH-Server-In-TCP -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name OpenSSH-Server-In-TCP -DisplayName "OpenSSH Server (sshd)" `
            -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow | Out-Null
    }
    Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -Enabled True -Profile Any
    # The clipboard, in the browser (noVNC) and on VNC: the virtio-serial
    # driver and SPICE's agent, which carry it to and from QEMU's vdagent
    # channel. windows-test.sh fetched and checked them; vm-run.sh put them here.
    $p = Start-Process C:\Autoflash\virtio-win-guest-tools.exe -Wait -PassThru -ArgumentList "/install", "/quiet", "/norestart"
    if ($p.ExitCode -notin 0, 3010) { throw "virtio-win's guest tools failed (exit $($p.ExitCode))" }
    Remove-Item C:\Autoflash\virtio-win-guest-tools.exe
    # No updates mid-test: each run starts from this disk, so they would never stick anyway.
    $wu = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    New-Item $wu -Force | Out-Null
    Set-ItemProperty $wu -Name NoAutoUpdate -Value 1 -Type DWord

    New-Item C:\Autoflash\rig.done -ItemType File -Force | Out-Null
    Write-Output "Rig ready."
} catch {
    Write-Output "PROVISIONING FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
    shutdown.exe /s /t 5 /c "Rig ready"
}
