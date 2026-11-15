# One-time setup for Browser Helper on Windows.
# Right-click this file > "Run with PowerShell"  (or: powershell -ExecutionPolicy Bypass -File install.ps1)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "== Browser Helper setup =="

$py = $null
foreach ($c in @(@("py", "-3"), @("python"))) {
  try {
    & $c[0] $c[1..9] -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
  } catch {}
}
if (-not $py) {
  Write-Host "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/"
  Write-Host "(tick 'Add python.exe to PATH' in the installer), then run this again."
  Read-Host "Press Enter to close"; exit 1
}

if (-not (Test-Path ".venv")) { & $py[0] $py[1..9] -m venv .venv }
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $vpy -m pip install -q --upgrade pip
Write-Host "Installing (this takes a few minutes the first time)..."
& $vpy -m pip install -q -e ".[home]"
& $vpy -m playwright install chromium

$envFile = Join-Path $HOME ".browser_engine\home.env"
if (-not ((Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^GOOGLE_API_KEY=' -Quiet))) {
  & (Join-Path $PSScriptRoot ".venv\Scripts\browser-engine-home.exe") --set-key
}

# Free local models: layer 1 (reads pages) and layer 2 (looks at screenshots)
Write-Host ""
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  $a = Read-Host "Download the free local AI models for both layers (about 11 GB, one time)? [y/N]"
  if ($a -match '^(y|yes)$') { ollama pull qwen2.5:7b; ollama pull qwen2.5vl:7b }
  else { Write-Host "Skipped. Later: ollama pull qwen2.5:7b ; ollama pull qwen2.5vl:7b" }
} else {
  Write-Host "Tip: install Ollama (https://ollama.com) and run 'ollama pull qwen2.5:7b' and 'ollama pull qwen2.5vl:7b'"
  Write-Host "     so the helper works on your computer for free and can read websites from screenshots."
}

$desk = [Environment]::GetFolderPath("Desktop")
$bat = Join-Path $desk "Browser Helper.bat"
"@echo off`r`ncd /d `"$PSScriptRoot`"`r`n.venv\Scripts\browser-engine-home.exe`r`npause" | Set-Content -Encoding ASCII $bat

Write-Host ""
Write-Host "Done. To start: double-click 'Browser Helper' on your Desktop. A chat page opens in your browser."
Read-Host "Press Enter to close"
