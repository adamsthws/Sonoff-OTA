<#
Gets a Windows computer ready to run the conversion tool against the emulated
S60 (docs/emulator.md, "On Windows"). Checks everything first, lists what is
missing, and installs it all after one question and, if any of it needs
administrator rights, one elevation prompt.

    tools\emulator\windows-bootstrap.ps1                  Windows wired to an emulator elsewhere
    tools\emulator\windows-bootstrap.ps1 -LocalEmulator   the emulator on this computer too, in WSL

Always: Python 3.10 or newer, OpenVPN's TAP-Windows6 driver and the "S60
emulator" network adapter. With -LocalEmulator also WSL 2, a Linux
distribution, and in it Docker and the NVS generator - installing WSL needs
one restart, after which running this again carries on without asking.

Downloads are pinned: each installer's SHA-256 is checked before it is run.
-Yes answers the question itself (for the emulator's own Windows VM).
#>
param(
    [switch]$Yes,
    [switch]$LocalEmulator,
    [string]$Distro = "Ubuntu-24.04",
    # Every Python found, and whether it will do; changes nothing.
    [switch]$ListPython,
    # Internal: the elevated half, and where it writes what it did.
    [string]$AdminItems = "",
    [string]$Log = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is many times slower with it
$env:WSL_UTF8 = "1"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Adapter = "S60 emulator"
$Address = "192.168.60.10"
$State = Join-Path $env:LOCALAPPDATA "s60-emu"
$Approved = Join-Path $State "bootstrap-approved"

$OpenVpn = @{
    Url = "https://swupdate.openvpn.org/community/releases/OpenVPN-2.7.7-I001-amd64.msi"
    Sha256 = "9069a48397c4fd4135fb9c9baea4863c74a465651f67e55a423a433e503100bb"
}
$Tapctl = Join-Path $env:ProgramFiles "OpenVPN\bin\tapctl.exe"
# Finding and installing Python, and pinned downloads: shared with start.ps1.
. (Join-Path $Root "tools\windows-python.ps1")

# -- what is there ---------------------------------------------------------------

function Test-Tap {
    if (-not (Test-Path $Tapctl)) { return $false }
    return [bool]((pnputil /enum-drivers) -match "TAP-Windows Provider V9")
}

function Test-AdapterReady {
    $nic = Get-NetAdapter -Name $Adapter -ErrorAction SilentlyContinue
    if (-not $nic) { return $false }
    $tcpip = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$($nic.InterfaceGuid)"
    $config = Get-ItemProperty $tcpip -ErrorAction SilentlyContinue
    return $config -and $config.EnableDHCP -eq 0 -and @($config.IPAddress) -contains $Address
}

function Test-Wsl {
    if (-not (Test-Path (Join-Path $env:ProgramFiles "WSL\wsl.exe"))) { return $false }
    $feature = Get-CimInstance Win32_OptionalFeature -Filter "Name='VirtualMachinePlatform'" -ErrorAction SilentlyContinue
    return $feature -and $feature.InstallState -eq 1
}

function Test-Distro { return [bool]((wsl.exe --list --quiet 2>$null) -contains $Distro) }

function Test-DistroReady {
    wsl.exe -d $Distro --exec sh -c 'command -v docker >/dev/null && command -v rsync >/dev/null && test -x "$HOME/.local/share/s60-emu/venv/bin/python" && docker info >/dev/null 2>&1' 2>$null | Out-Null
    return $LASTEXITCODE -eq 0   # wsl.exe's complaints go to stdout: kept out of the answer above
}

# -- installing it -----------------------------------------------------------------

function Install-Tap {
    $msi = Get-Pinned "OpenVPN (for its TAP-Windows6 driver)" $OpenVpn
    Write-Output "Installing the TAP-Windows6 driver and tapctl (OpenVPN's installer, nothing else from it)..."
    $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList `
        "/i", "`"$msi`"", "ADDLOCAL=OpenVPN,Drivers,Drivers.TAPWindows6", "/qn", "/norestart"
    if ($p.ExitCode -notin 0, 3010) { throw "the OpenVPN installer failed (exit $($p.ExitCode))" }
}

function Install-Adapter {
    if (-not (Get-NetAdapter -Name $Adapter -ErrorAction SilentlyContinue)) {
        & $Tapctl create --hwid root\tap0901 --name $Adapter | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "tapctl could not make the adapter (exit $LASTEXITCODE)" }
    }
    # Its address on the emulated home network, with no gateway: Windows' own
    # internet stays where it is. Written where Windows keeps it, not with
    # New-NetIPAddress or netsh: the adapter is disconnected until the link
    # runs, and both leave DHCP on then. Windows applies it when it connects.
    $guid = (Get-NetAdapter -Name $Adapter).InterfaceGuid
    $tcpip = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$guid"
    Set-ItemProperty $tcpip -Name EnableDHCP -Value 0 -Type DWord
    Set-ItemProperty $tcpip -Name IPAddress -Value @($Address) -Type MultiString
    Set-ItemProperty $tcpip -Name SubnetMask -Value @("255.255.255.0") -Type MultiString
    Set-ItemProperty $tcpip -Name DefaultGateway -Value @() -Type MultiString
    # The lowest metric, so the tool's mDNS goes out on it - as on a computer
    # whose only network is the plug's Wi-Fi.
    netsh interface ipv4 set interface interface="$Adapter" metric=1 | Out-Null
    Write-Output "The '$Adapter' adapter is $Address/24, with no gateway."
}

function Install-Wsl {
    Write-Output "Installing WSL..."
    wsl.exe --install --no-distribution
    if ($LASTEXITCODE -ne 0) { throw "wsl --install failed (exit $LASTEXITCODE)" }
}

function Install-Distro {
    Write-Output "Installing $Distro in WSL..."
    wsl.exe --install $Distro --no-launch
    if ($LASTEXITCODE -ne 0) { throw "installing $Distro failed (exit $LASTEXITCODE)" }
}

function Install-DistroSetup {
    $user = ($env:USERNAME.ToLower() -replace '[^a-z0-9_-]', '-')
    if ($user -notmatch '^[a-z_]') { $user = "u$user" }
    $script = (wsl.exe -d $Distro --exec wslpath -a (Join-Path $PSScriptRoot "wsl-setup.sh").Replace('\', '/')).Trim()
    Write-Output "Setting up $Distro for the emulator (Docker, the NVS generator)..."
    wsl.exe -d $Distro -u root --exec bash $script $user
    if ($LASTEXITCODE -ne 0) { throw "setting up $Distro failed (exit $LASTEXITCODE)" }
    wsl.exe --terminate $Distro | Out-Null   # its wsl.conf - systemd, the default user - applies from the next start
}

if ($ListPython) { Show-PythonCandidates; exit 0 }

$Items = @(
    @{ Key = "python";  Admin = $false; Text = "$($PortablePython.Name), into .python\ - nothing installed"
       Ready = { Test-Path (Join-Path (Join-Path $PythonHome $PortablePython.Folder) "python.exe") }
       Install = { Get-ToolPython | Out-Null } }
    @{ Key = "tap";     Admin = $true;  Text = "OpenVPN's TAP-Windows6 network driver";          Ready = { Test-Tap };              Install = { Install-Tap } }
    @{ Key = "adapter"; Admin = $true;  Text = "The '$Adapter' network adapter ($Address)";     Ready = { Test-AdapterReady };     Install = { Install-Adapter } }
)
if ($LocalEmulator) {
    $Items += @(
        @{ Key = "wsl";    Admin = $true;  Text = "WSL 2 (needs one restart)";                  Ready = { Test-Wsl };          Install = { Install-Wsl } }
        @{ Key = "distro"; Admin = $false; Text = "$Distro in WSL";                             Ready = { Test-Distro };       Install = { Install-Distro } }
        @{ Key = "setup";  Admin = $false; Text = "Docker and the NVS generator in $Distro";    Ready = { Test-DistroReady };  Install = { Install-DistroSetup } }
    )
}

# -- the elevated half ---------------------------------------------------------------

if ($AdminItems) {
    if ($Log) { Start-Transcript -Path $Log | Out-Null }
    try {
        foreach ($item in $Items | Where-Object { $AdminItems.Split(",") -contains $_.Key }) { & $item.Install }
        exit 0
    } catch {
        Write-Output "FAILED: $_"
        exit 1
    } finally {
        if ($Log) { Stop-Transcript | Out-Null }
    }
}

# -- checking, asking once, installing ------------------------------------------------

$missing = @($Items | Where-Object { -not (& $_.Ready) })
if (-not $missing) {
    Write-Host "Everything the Windows emulation needs is in place."
    Remove-Item $Approved -ErrorAction SilentlyContinue
    exit 0
}
Write-Host "To run the conversion tool against the emulator, this computer needs:"
$missing | ForEach-Object { Write-Host "    - $($_.Text)" }
if (@($missing | Where-Object Admin)) {
    Write-Host "Some of it needs administrator rights: Windows asks once, for all of it."
}
if (-not ($Yes -or (Test-Path $Approved))) {
    # A string, always: with no keyboard Read-Host gives $null, and
    # "$null -notmatch" is no match at all - which would read as yes.
    $answer = "$(Read-Host "Install all of it now? [y/N]")"
    if ($answer.Trim() -notmatch '^(y|yes)$') { Write-Host "Nothing was changed."; exit 1 }
}
New-Item -ItemType Directory -Force $State | Out-Null
New-Item -ItemType File -Force $Approved | Out-Null   # a restart in between does not ask again

$admin = @($missing | Where-Object Admin)
if ($admin) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $keys = ($admin | ForEach-Object Key) -join ","
    $log = Join-Path $State "bootstrap-admin.log"
    Remove-Item $log -ErrorAction SilentlyContinue
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                   "-AdminItems", $keys, "-Log", "`"$log`"", "-Distro", $Distro)
    if ($LocalEmulator) { $arguments += "-LocalEmulator" }
    try {
        $p = if ($isAdmin) { Start-Process powershell -Wait -PassThru -NoNewWindow -ArgumentList $arguments }
             else { Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList $arguments }
    } catch {
        Write-Host "Administrator rights were not given, so nothing that needs them was installed."
        exit 1
    }
    if (Test-Path $log) {
        Get-Content $log | Where-Object { $_ -notmatch '^\*{5,}|^(Windows PowerShell transcript|Start time|End time|Username|RunAs User|Configuration Name|Machine|Host Application|Process ID|PSVersion|PSEdition|PSCompatibleVersions|BuildVersion|CLRVersion|WSManStackVersion|PSRemotingProtocolVersion|SerializationVersion)' } |
            ForEach-Object { Write-Host "    $_" }
    }
    if ($p.ExitCode -ne 0) { Write-Host "Installing failed - see above."; exit 1 }
    Update-SessionPath
}

if (@($missing | Where-Object Key -eq "wsl") -and -not (Test-Distro)) {
    Write-Host ""
    Write-Host "WSL is installed. Restart Windows, then run this again: it carries on"
    Write-Host "with the rest without asking."
    exit 3010
}

foreach ($item in $missing | Where-Object { -not $_.Admin }) {
    if (& $item.Ready) { continue }
    try { & $item.Install | ForEach-Object { Write-Host "    $_" } }
    catch { Write-Host "Installing failed: $_"; exit 1 }
}

$still = @($Items | Where-Object { -not (& $_.Ready) })
if ($still) {
    Write-Host "Still missing:"
    $still | ForEach-Object { Write-Host "    - $($_.Text)" }
    exit 1
}
Remove-Item $Approved -ErrorAction SilentlyContinue
Write-Host "Everything the Windows emulation needs is in place."
exit 0
