<#
The emulator with Windows as the computer: run the conversion tool on Windows
against the emulated S60 (see docs/emulator.md, "On Windows").

    tools\emulator\emu.ps1 -Cloud     plug and tool online (real plug must be unplugged)

Options as emu.sh's: -Cloud -Fresh -Pcap -Rebuild -NoFirewall, -Plug <MAC> for
which backed-up plug (asked in the emulator's window when there are several),
and -Distro to pick the WSL distribution (Ubuntu-24.04 otherwise).

Opens the emulator (emu.sh --windows, under WSL) in a window of its own - the
plug's power and serial log are there - and the link that puts Windows on its
home network in another. This window is left for the conversion:

    .\start.ps1

Anything this computer is missing - WSL, Docker in it, Python, the TAP driver,
the adapter - is listed first, and installed after one question
(tools\emulator\windows-bootstrap.ps1 -LocalEmulator).
#>
param([switch]$Cloud, [switch]$Fresh, [switch]$Pcap, [switch]$Rebuild, [switch]$NoFirewall,
      [string]$Plug = "", [string]$Distro = "Ubuntu-24.04")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

& (Join-Path $PSScriptRoot "windows-bootstrap.ps1") -LocalEmulator -Distro $Distro
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }   # it has said why, or that Windows needs a restart

# Windows' own Python, for the link (start.ps1 finds the same one for the tool).
. (Join-Path $Root "tools\windows-python.ps1")
$Python = Get-ToolPython
$wsl = @("-d", $Distro)

# Closing the emulator's window stops only the Docker client: the emulator
# carries on, and emu.sh would refuse to start another.
if (& wsl.exe @wsl --exec docker ps -q --filter 'name=^s60-emu$') {
    throw ("The emulator is still running from before (its window was closed without leaving it).`n" +
           "Rejoin it with:  wsl docker attach s60-emu`n" +
           "or stop it with: wsl docker stop s60-emu")
}
$wslRoot = (& wsl.exe @wsl -- wslpath -a ($Root -replace '\\', '/')).Trim()
$flags = @("--windows")
if ($Cloud) { $flags += "--cloud" }
if ($Fresh) { $flags += "--fresh" }
if ($Pcap) { $flags += "--pcap" }
if ($Rebuild) { $flags += "--rebuild" }
if ($NoFirewall) { $flags += "--no-firewall" }
if ($Plug) {
    if ($Plug -notmatch '^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$') { throw "-Plug takes a MAC address, such as 58:cf:79:01:02:03" }
    $flags += "--plug=$Plug"
}

# One command line, not a list: Start-Process joins a list without quoting.
# If emu.sh stops, its window stays open until its reason has been read.
$emu = "tools/emulator/emu.sh $($flags -join ' ') || read -rp 'emu.sh stopped - press Enter to close this window. '"
Start-Process wsl.exe -ArgumentList ((($wsl + @("--cd", "`"$wslRoot`"", "--exec", "bash", "-c")) -join " ") + " `"$emu`"")

# Only one link can hold the adapter.
$link = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'windows_link\.py' }
if ($link) {
    Write-Host "The link is already running in its own window - it carries on with this emulator."
} else {
    Start-Process $Python -WorkingDirectory $Root -ArgumentList "tools\emulator\windows_link.py"
}

Write-Host @"

The emulator is starting in its own window$(if ($Cloud) { " - confirm the real plug is unplugged there" }),
and the link in another. Once the link says Connected, run the conversion here:

    .\start.ps1

When it asks you to switch the plug off and on, type in the emulator window:
    plug restart | plug off | plug on | plug cycle 7
Your home Wi-Fi there is PICSimLabWifi, with no password.
"@
