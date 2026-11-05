# RUN ON THE WINDOWS DESKTOP (run from inside the built TPAudioAgent folder; no admin needed)
<#
Installs the agent for the current Windows user:
  - copies this folder to %LOCALAPPDATA%\TPAudioAgent\app
  - writes %LOCALAPPDATA%\TPAudioAgent\config.toml from the example if none exists
  - registers a per-user logon task "TPAudioAgent" (falls back to a Startup-folder shortcut)
  - starts the agent

Touches nothing else: no drivers, no services, no system settings, no registry beyond
the user's own scheduled task. Remove everything with uninstall.ps1.
#>
$ErrorActionPreference = "Stop"
$Home_ = Join-Path $env:LOCALAPPDATA "TPAudioAgent"
$App = Join-Path $Home_ "app"
$Cfg = Join-Path $Home_ "config.toml"
$Exe = Join-Path $App "TPAudioAgent.exe"
$TaskName = "TPAudioAgent"

Get-Process TPAudioAgent -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item -ItemType Directory -Force -Path $App | Out-Null
Copy-Item -Recurse -Force (Join-Path $PSScriptRoot "*") $App

if (-not (Test-Path $Cfg)) {
  $text = Get-Content (Join-Path $App "config.example.toml") -Raw
  $models = Get-ChildItem (Join-Path $App "models") -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($models) {
    # TOML literal string: backslashes need no escaping
    $text = $text.Replace('model = "medium.en"', "model = '$($models.FullName)'")
  }
  Set-Content -Path $Cfg -Value $text -Encoding UTF8
  Write-Host "Wrote $Cfg — edit device names to match 'TPAudioAgent.exe --list-devices'."
}

$args_ = "--config `"$Cfg`""
try {
  $action = New-ScheduledTaskAction -Execute $Exe -Argument $args_ -WorkingDirectory $App
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Registered logon task '$TaskName' and started it."
} catch {
  Write-Warning "Scheduled task failed ($_); using a Startup-folder shortcut instead."
  $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "TPAudioAgent.lnk"
  $sh = New-Object -ComObject WScript.Shell
  $s = $sh.CreateShortcut($lnk); $s.TargetPath = $Exe; $s.Arguments = $args_; $s.WorkingDirectory = $App; $s.Save()
  Start-Process -FilePath $Exe -ArgumentList $args_ -WorkingDirectory $App
}
Start-Sleep -Seconds 3
try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/health).Content | Write-Host }
catch { Write-Warning "Agent not answering yet; check $Home_\logs\agent.log" }
