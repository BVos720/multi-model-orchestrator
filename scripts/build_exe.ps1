<#
  Builds a standalone Orchest.exe (no Python install needed to run it)
  via PyInstaller. Output: dist\Orchest.exe (also copied as dist\OrchestCLI.exe)

  Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path "$root\.venv")) {
    Write-Host "No .venv found - run scripts\setup.ps1 first." -ForegroundColor Yellow
    exit 1
}

& "$root\.venv\Scripts\pip.exe" install -e "$root[build]" --quiet

& "$root\.venv\Scripts\pyinstaller.exe" `
    --onefile `
    --name Orchest `
    --paths src `
    --collect-all questionary `
    --collect-all google.genai `
    --console `
    scripts\entrypoint.py

$orchestCliPath = Join-Path $root "dist\OrchestCLI.exe"
Copy-Item "$root\dist\Orchest.exe" $orchestCliPath -Force
Write-Host "`nBuilt: $root\dist\Orchest.exe" -ForegroundColor Cyan
Write-Host "Also copied as: $orchestCliPath (identical binary, same thing, typed differently)" -ForegroundColor Cyan
Write-Host "This .exe still needs .env / .orchestrator\ next to wherever you run it from" -ForegroundColor Cyan
Write-Host "(same as the pip-installed version) - it bundles Python and the dependencies," -ForegroundColor Cyan
Write-Host "not your config. Copy .env.example -> .env next to the .exe to get started." -ForegroundColor Cyan
