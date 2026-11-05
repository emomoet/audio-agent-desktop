# RUN ON THE WINDOWS DESKTOP (build machine; needs python.org Python 3.11+, not the Store build)
<#
Builds dist\TPAudioAgent\ (PyInstaller onedir: removal = delete one folder).

  powershell -ExecutionPolicy Bypass -File scripts\build.ps1            # app only
  powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -WithModel # + bundle the Whisper model

-WithModel downloads the model once at build time so the trading desk never needs to
reach HuggingFace (desk networks often block or TLS-inspect it).
#>
param(
  [switch]$WithModel,
  [string]$Model = "medium.en"
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m venv .venv-build
& .\.venv-build\Scripts\python.exe -m pip install --upgrade pip
& .\.venv-build\Scripts\python.exe -m pip install -e ".[loopback,asr,build]"

& .\.venv-build\Scripts\pyinstaller.exe --noconfirm --clean --onedir --noconsole `
  --name TPAudioAgent `
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all soundcard `
  --collect-data sounddevice `
  scripts\agent_entry.py

Copy-Item config.example.toml dist\TPAudioAgent\config.example.toml
Copy-Item scripts\install.ps1, scripts\uninstall.ps1 dist\TPAudioAgent\

if ($WithModel) {
  $out = "dist\TPAudioAgent\models\$Model"
  & .\.venv-build\Scripts\python.exe -c "from faster_whisper import download_model; print(download_model('$Model', output_dir=r'$out'))"
  Write-Host "Model bundled at $out — set [asr] model = '<install dir>\models\$Model' (install.ps1 does this)."
}
Write-Host "Built dist\TPAudioAgent"
