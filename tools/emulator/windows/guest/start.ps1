<#
At each logon (the hook firstlogon.ps1 adds). vm-run.sh layer leaves the name
of a layer to set up in C:\Autoflash\layer.txt: its script runs, and shuts Windows
down. In a test run: wire Windows into the emulator's home network, and open
a window at the checkout to run the conversion tool in. vm-run.sh
puts the checkout in C:\Autoflash\repo, and the emulator's address and the console to
run in in C:\Autoflash\run.json: OpenConsole - Windows Terminal's console, as on
Windows 11 (Terminal itself needs XAML, which Server Core lacks) - or the
classic one, as on Windows 10.
#>
if (Test-Path C:\Autoflash\layer.txt) {
    $layer = (Get-Content C:\Autoflash\layer.txt -Raw).Trim()
    Remove-Item C:\Autoflash\layer.txt
    & "C:\Autoflash\$layer.ps1"
    exit 0
}
$run = "C:\Autoflash\run.json"
if (-not (Test-Path $run)) { exit 0 }  # a layer itself, booted by hand: nothing to start
$config = Get-Content $run -Raw | ConvertFrom-Json
# The Linux side's key for SSH (vm-run.sh puts it here; rig.ps1 runs sshd).
# autoflash is an administrator, so sshd reads administrators' one file, and only
# when no one but them and SYSTEM can change it.
if (Test-Path C:\Autoflash\authorized_keys) {
    $keys = "$env:ProgramData\ssh\administrators_authorized_keys"
    Copy-Item C:\Autoflash\authorized_keys $keys -Force
    icacls.exe $keys /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" | Out-Null
}
$repo = "C:\Autoflash\repo"
. "$repo\tools\windows-python.ps1"
# The link's own Python (rig.ps1), never the tool's: in a --first-run there is
# none yet, and the tool must find that so.
$python = Join-Path (Join-Path C:\Autoflash\rig-python $PortablePython.Folder) python.exe

Start-Process $python -ArgumentList "tools\emulator\windows_link.py", "--host", $config.link_host `
    -WorkingDirectory $repo
$welcome = @"
`$host.UI.RawUI.WindowTitle = 'Autoflash - $(if ($config.console -eq "openconsole") { "OpenConsole" } else { "classic console" })'
Write-Host ''
Write-Host 'Windows is on the emulated home Wi-Fi once the link window says Connected.'
Write-Host 'Run the conversion here, as a Windows user would:'
Write-Host ''
Write-Host '    .\start.ps1'
Write-Host ''
Write-Host 'Switch the plug off and on in the emulator, on the Linux side: plug restart'
Write-Host 'Your home Wi-Fi there is PICSimLabWifi, with no password.'
Write-Host ''
"@
$openconsole = "C:\Autoflash\openconsole\OpenConsole.exe"
if ($config.console -eq "openconsole" -and (Test-Path $openconsole)) {
    # Its command line goes to PowerShell as one string: encoded, nothing to quote.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($welcome))
    Start-Process $openconsole -WorkingDirectory $repo `
        -ArgumentList "powershell.exe -NoExit -EncodedCommand $encoded"
} else {
    Start-Process powershell -WorkingDirectory $repo -ArgumentList "-NoExit", "-Command", $welcome
}
