# 社区智慧养老服务与健康监测平台 · 开发启动脚本
# 用法（在项目根目录或任意位置）：powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1
#
# 说明：
# 1) 演示环境将 TEMP/TMP 指向项目内 data\tmp，避免系统临时目录权限/路径问题；
# 2) 首次运行会自动创建虚拟环境（若不存在则提示手工创建）与演示数据；
# 3) 服务地址：http://127.0.0.1:8000  接口文档：http://127.0.0.1:8000/docs

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# ---- 环境变量 ----
$TempDir = Join-Path $Root 'data\tmp'
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
$env:TEMP = $TempDir
$env:TMP = $TempDir
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = $Root

Write-Host '=== 社区智慧养老服务与健康监测平台 · 开发服务器 ===' -ForegroundColor Cyan
Write-Host "项目目录：$Root"
Write-Host "临时目录：$TempDir"

# ---- 解释器 ----
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Host '未找到虚拟环境 .venv，正在创建 …' -ForegroundColor Yellow
    python -m venv .venv
    if (-not (Test-Path $Python)) {
        Write-Error '创建虚拟环境失败，请手工执行：python -m venv .venv'
    }
    & $Python -m pip install -r requirements.txt
}

# ---- 数据库与演示数据 ----
$DbFile = Join-Path $Root 'data\app.db'
if (-not (Test-Path $DbFile)) {
    Write-Host '未检测到数据库，正在初始化表结构与演示数据 …' -ForegroundColor Yellow
    & $Python -m scripts.seed
}

# ---- 启动服务 ----
Write-Host '启动 uvicorn（--reload）…' -ForegroundColor Green
Write-Host '页面入口 http://127.0.0.1:8000   默认密码 Admin@123' -ForegroundColor Green
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
