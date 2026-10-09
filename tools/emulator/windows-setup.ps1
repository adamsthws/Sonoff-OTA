<#
One-time set-up of Windows' wire into the emulator (see docs/emulator.md,
"From Windows"). Asks for administrator rights.

Makes a TAP network adapter called "S60 emulator" (OpenVPN's TAP-Windows6
driver), at 192.168.60.10/24 on the emulated home network, with no gateway:
Windows' own internet stays on its usual connection. Its interface metric is
the lowest, so the conversion tool's mDNS discovery goes out on it, as it
would on a computer whose only network is the plug's Wi-Fi. While the
emulator is not running the adapter shows as disconnected and does nothing.

Running it again checks and repairs the set-up. To remove the adapter:
    & "C:\Program Files\OpenVPN\bin\tapctl.exe" delete "S60 emulator"
#>
param([string]$Log = "")
$ErrorActionPreference = "Stop"
$Name = "S60 emulator"
$Address = "192.168.60.10"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    # Elevated in a window of its own; what it says comes back through a log.
    $Log = Join-Path $env:TEMP "s60-emulator-setup.log"
    Remove-Item $Log -ErrorAction SilentlyContinue
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Log", "`"$Log`"")
    if (Test-Path $Log) { Get-Content $Log }
    exit $p.ExitCode
}
if ($Log) { Start-Transcript -Path $Log | Out-Null }

try {
    $tapctl = Join-Path $env:ProgramFiles "OpenVPN\bin\tapctl.exe"
    if (-not (Get-NetAdapter -Name $Name -ErrorAction SilentlyContinue)) {
        if (-not (Test-Path $tapctl)) {
            throw "The TAP-Windows6 driver comes with OpenVPN: winget install OpenVPNTechnologies.OpenVPN"
        }
        & $tapctl create --hwid root\tap0901 --name $Name | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "tapctl could not make the adapter (exit $LASTEXITCODE). Is OpenVPN's TAP-Windows6 driver installed?"
        }
        Write-Output "Made the network adapter '$Name'."
    }

    # Written where Windows keeps it, not with New-NetIPAddress or netsh: the
    # adapter is disconnected until the link runs, and both leave DHCP on then.
    # Windows applies it when the link connects the adapter.
    $guid = (Get-NetAdapter -Name $Name).InterfaceGuid
    $tcpip = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$guid"
    Set-ItemProperty $tcpip -Name EnableDHCP -Value 0 -Type DWord
    Set-ItemProperty $tcpip -Name IPAddress -Value @($Address) -Type MultiString
    Set-ItemProperty $tcpip -Name SubnetMask -Value @("255.255.255.0") -Type MultiString
    Set-ItemProperty $tcpip -Name DefaultGateway -Value @() -Type MultiString
    netsh interface ipv4 set interface interface="$Name" metric=1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "netsh could not set the metric (exit $LASTEXITCODE)" }
    Set-DnsClient -InterfaceAlias $Name -RegisterThisConnectionsAddress $false
    Write-Output "'$Name' is set up: $Address/24, no gateway, interface metric 1."
    exit 0
} catch {
    Write-Output "Set-up failed: $_"
    exit 1
} finally {
    if ($Log) { Stop-Transcript | Out-Null }
}
