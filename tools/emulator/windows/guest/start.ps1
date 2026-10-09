<#
At each logon of a test run: wire Windows into the emulator's home network,
and open a window at the checkout to run the conversion tool in. vm-run.sh
puts the checkout in C:\S60\repo and the emulator's address in C:\S60\run.json.
#>
$run = "C:\S60\run.json"
if (-not (Test-Path $run)) { exit 0 }  # the base disk itself: nothing to start
$config = Get-Content $run -Raw | ConvertFrom-Json
$repo = "C:\S60\repo"
. "$repo\tools\windows-python.ps1"
$python = Get-ToolPython

Start-Process $python -ArgumentList "tools\emulator\windows_link.py", "--host", $config.link_host `
    -WorkingDirectory $repo
Start-Process powershell -WorkingDirectory $repo -ArgumentList "-NoExit", "-Command", @"
`$host.UI.RawUI.WindowTitle = 'S60 conversion'
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
