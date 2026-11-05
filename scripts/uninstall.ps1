# RUN ON THE WINDOWS DESKTOP
<#
Removes the agent completely: stops it, removes the logon task / Startup shortcut, deletes
%LOCALAPPDATA%\TPAudioAgent (app, config, logs). Use -KeepConfig to keep config.toml.
Nothing else on the machine was changed by install.ps1.
#>
param([switch]$KeepConfig)
$Home_ = Join-Path $env:LOCALAPPDATA "TPAudioAgent"

Get-Process TPAudioAgent -ErrorAction SilentlyContinue | Stop-Process -Force
Unregister-ScheduledTask -TaskName "TPAudioAgent" -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("Startup")) "TPAudioAgent.lnk") -ErrorAction SilentlyContinue

if ($KeepConfig) {
  Get-ChildItem $Home_ -Exclude config.toml -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
} else {
  Remove-Item -Recurse -Force $Home_ -ErrorAction SilentlyContinue
}
Write-Host "TPAudioAgent removed."
