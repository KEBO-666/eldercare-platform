# Development/demo launcher. Existing .env and database contents are preserved.
# powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_dev.ps1 -Port 8000
param(
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [switch]$NoReload,
    [string]$BasePython = 'python'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

function Invoke-CheckedNative {
    param([string]$Executable, [string[]]$Arguments, [string]$Step)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit $LASTEXITCODE). Resolve the error above before retrying."
    }
}

$Listener = $null
try {
    $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    $Listener.Start()
} catch {
    throw "Port $Port is unavailable. Stop your own server or use -Port 8001."
} finally {
    if ($null -ne $Listener) { $Listener.Stop() }
}

$TempDir = Join-Path $Root 'data\tmp'
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
$env:TEMP = $TempDir
$env:TMP = $TempDir
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = $Root

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $SystemPython = Get-Command $BasePython -ErrorAction Stop
    Invoke-CheckedNative -Executable $SystemPython.Source -Arguments @('-c', "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'") -Step 'Python version check'
    Invoke-CheckedNative -Executable $SystemPython.Source -Arguments @('-m', 'venv', '.venv') -Step 'Virtual environment creation'
    Invoke-CheckedNative -Executable $Python -Arguments @('-m', 'pip', 'install', '-r', 'requirements.txt') -Step 'Dependency installation'
}

# A failed first installation may leave a venv directory. Check it explicitly.
& $Python -c 'import fastapi, sqlalchemy, uvicorn, jinja2, cryptography, bcrypt, PIL, httpx, apscheduler, openpyxl, multipart, pydantic_settings, jwt'
if ($LASTEXITCODE -ne 0) {
    throw 'Dependencies are incomplete. Run: .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Created .env from the demo template; existing configuration is never overwritten.'
}

# Resolve the configured SQLite location rather than assuming data/app.db.
$NeedSeed = & $Python -c "from pathlib import Path; from sqlalchemy.engine import make_url; from app.core.config import settings; u = make_url(settings.db_url); print('yes' if u.get_backend_name() == 'sqlite' and (not u.database or u.database == ':memory:' or not Path(u.database).is_file()) else 'no')"
if ($LASTEXITCODE -ne 0) { throw 'Cannot load database configuration. Check .env and environment variables.' }
if (($NeedSeed | Select-Object -Last 1) -eq 'yes') {
    Invoke-CheckedNative -Executable $Python -Arguments @('-m', 'scripts.seed') -Step 'Demo database initialization'
}

Write-Host "Demo: http://127.0.0.1:$Port   API: http://127.0.0.1:$Port/docs"
Write-Host 'Demo accounts: grid1 / nurse1 / elder1; password: Admin@123'
$ServerArguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port")
if (-not $NoReload) { $ServerArguments += '--reload' }
Invoke-CheckedNative -Executable $Python -Arguments $ServerArguments -Step 'Application server'
