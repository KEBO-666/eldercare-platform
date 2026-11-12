# 比赛验收：静态检查、全量测试、覆盖率及机器可读报告。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe = 'python' }
    & $pythonExe -m ruff check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonExe -m pytest tests -q --tb=short --junitxml=reports/junit.xml --cov=app --cov-report=term --cov-report=xml:reports/coverage.xml --cov-report=html:reports/htmlcov
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonExe -m scripts.verify_demo
    exit $LASTEXITCODE
}
finally { Pop-Location }
