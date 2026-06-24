$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  语音助手 - 一键启动" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[ERROR] 未找到 Python，请先安装 Python 3.8+" -ForegroundColor Red
    Write-Host "        下载: https://www.python.org/downloads/" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}
Write-Host "[OK] Python: $($py.Source)" -ForegroundColor Green

# Check/install deps silently first
Write-Host "[*] 检查依赖..." -ForegroundColor Yellow
$pipResult = pip install -r requirements.txt --quiet 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] 正在安装依赖（首次运行需要联网）..." -ForegroundColor Yellow
    pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] 依赖安装失败" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}
Write-Host "[OK] 依赖就绪" -ForegroundColor Green

# Check config
if (-not (Test-Path "config.json")) {
    Write-Host "[ERROR] 配置文件 config.json 未找到" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  启动信息:" -ForegroundColor White
Write-Host "  - 配置页面: http://127.0.0.1:18099" -ForegroundColor Yellow
Write-Host "  - 全局热键: Ctrl+Alt+R 录音" -ForegroundColor Yellow
Write-Host "  - 关闭窗口 = 退出程序" -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

try {
    python assistant.py
} catch {
    Write-Host ""
    Write-Host "[ERROR] 运行出错: $_" -ForegroundColor Red
}

Write-Host ""
Write-Host "程序已退出，按回车关闭" -ForegroundColor Gray
Read-Host
