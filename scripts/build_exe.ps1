<#
  Builds a standalone orchestrator.exe (no Python install needed to run it)
  via PyInstaller. Output: dist\orchestrator.exe

  Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path "$root\.venv")) {
    Write-Host "No .venv found - run scripts\setup.ps1 first." -ForegroundColor Yellow
    exit 1
}

& "$root\.venv\Scripts\pip.exe" install pyinstaller --quiet

& "$root\.venv\Scripts\pyinstaller.exe" `
    --onefile `
    --name orchestrator `
    --paths src `
    --collect-all questionary `
    --collect-all google.genai `
    --console `
    scripts\entrypoint.py

Write-Host "`nBuilt: $root\dist\orchestrator.exe" -ForegroundColor Cyan
Write-Host "This .exe still needs .env / .orchestrator\ next to wherever you run it from" -ForegroundColor Cyan
Write-Host "(same as the pip-installed version) - it bundles Python and the dependencies," -ForegroundColor Cyan
Write-Host "not your config. Copy .env.example -> .env next to the .exe to get started." -ForegroundColor Cyan
