<#
  Automated setup for multi-model-orchestrator (Windows).
  Installs/checks everything needed and gets you to a working `orchestrator status`.

  Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  Flags:  -SkipOllama   (skip installing Ollama / pulling a model)
          -OllamaModel  (override the model to pull, default qwen2.5-coder:32b)
#>

param(
    [switch]$SkipOllama,
    [string]$OllamaModel = "qwen2.5-coder:32b"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Section($title) { Write-Host "`n== $title ==" -ForegroundColor Cyan }
function Ok($msg)        { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Warn($msg)      { Write-Host "  [!]  $msg" -ForegroundColor Yellow }

# --- 0. Disk space sanity check -------------------------------------------
Section "Disk space"
$drive = Get-PSDrive -Name ($root.Substring(0,1)) -ErrorAction SilentlyContinue
if ($drive) {
    $freeGB = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGB -lt 5) {
        Warn "Only ${freeGB}GB free on $($drive.Name):. Ollama + a coding model need real room"
        Warn "(qwen2.5-coder:32b alone is ~20GB). Free up space before continuing, or re-run"
        Warn "with -OllamaModel qwen2.5-coder:7b (~5GB) / -SkipOllama."
        $answer = Read-Host "Continue anyway? (y/N)"
        if ($answer -ne "y") { exit 1 }
    } else {
        Ok "${freeGB}GB free"
    }
}

# --- 1. Python ---------------------------------------------------------
Section "Python"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python -or -not (& python --version 2>$null)) {
    Warn "Python not found - installing via winget"
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
} else {
    Ok (& python --version)
}

# --- 2. Virtualenv + package install ------------------------------------
Section "Python environment"
Set-Location $root
if (-not (Test-Path "$root\.venv")) {
    python -m venv .venv
    Ok "created .venv"
}
& "$root\.venv\Scripts\pip.exe" install -e . --quiet
Ok "installed orchestrator + dependencies into .venv"

# --- 3. Claude Code CLI ---------------------------------------------------
Section "Claude Code CLI"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    Ok "claude CLI found ($($claude.Source))"
} else {
    Warn "claude CLI not found on PATH."
    Warn "Install it yourself: npm install -g @anthropic-ai/claude-code, then run 'claude' once to log in."
}

# --- 4. .env -----------------------------------------------------------
Section ".env"
if (-not (Test-Path "$root\.env")) {
    Copy-Item "$root\.env.example" "$root\.env"
    Ok "created .env from .env.example"
    Warn "Add your GEMINI_API_KEY to .env (free key: https://aistudio.google.com/apikey)"
} else {
    Ok ".env already exists, leaving it alone"
}

# --- 5. Ollama (local models) --------------------------------------------
if (-not $SkipOllama) {
    Section "Ollama"
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Warn "Ollama not found - installing via winget"
        winget install -e --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements
        Warn "You may need to restart this terminal for `ollama` to be on PATH."
    } else {
        Ok "ollama found ($($ollama.Source))"
    }

    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollama) {
        Section "Pulling local model: $OllamaModel"
        & ollama pull $OllamaModel
        Ok "pulled $OllamaModel"
    }
} else {
    Section "Ollama"
    Warn "Skipped (-SkipOllama). The swarm will run cloud-only until you set it up."
}

# --- 6. Final status -------------------------------------------------------
Section "Status"
& "$root\.venv\Scripts\orchestrator.exe" status
Write-Host "`nSetup done. Try:  .\.venv\Scripts\orchestrator run `"write a fizzbuzz function`"" -ForegroundColor Cyan
