# 本机 Ollama 服务控制器 (Tiger.M.M 自建)
# 用法:
#   powershell -NoProfile -File <此脚本> start    启动服务(后台, 幂等)
#   powershell -NoProfile -File <此脚本> stop     停止服务
#   powershell -NoProfile -File <此脚本> status   查看状态
#   powershell -NoProfile -File <此脚本> list     列出已装模型

param([Parameter(Position=0)][string]$Action = "status")

# ★ 2026-09-26: 原来这里写死 `C:\Users\<作者>\AppData\Local\...` —— 本脚本随分发包
#   出门 (tools/ 整个目录都进包), 写死作者的用户名既是个人数据 (verify_no_personal_data
#   会红), 也让别人机器上必然找不到 ollama.exe。
#   改成按环境变量推导: 先看用户级安装 (LOCALAPPDATA), 再看机器级 (ProgramFiles)。
$OllamaDir = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
if (-not (Test-Path (Join-Path $OllamaDir "ollama.exe"))) {
    $OllamaDir = Join-Path $env:ProgramFiles "Ollama"
}
$OllamaExe = Join-Path $OllamaDir "ollama.exe"
$ApiBase   = "http://127.0.0.1:11434"

function Test-Api {
    try {
        $r = Invoke-WebRequest -Uri "$ApiBase/api/tags" -TimeoutSec 3 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Get-ServeProc {
    Get-CimInstance Win32_Process -Filter "Name='ollama.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'serve' }
}

switch ($Action.ToLower()) {
    "start" {
        if (Test-Api) { Write-Output "ALREADY_RUNNING"; break }
        if (-not (Test-Path $OllamaExe)) { Write-Output "ERR: ollama.exe not found at $OllamaExe"; exit 1 }
        Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WorkingDirectory $OllamaDir -WindowStyle Hidden
        for ($i=0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-Api) { Write-Output "STARTED"; break }
        }
        if (-not (Test-Api)) { Write-Output "ERR: started but API not responding"; exit 1 }
    }
    "stop" {
        $p = Get-ServeProc
        if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Output "STOPPED" }
        else { Write-Output "NOT_RUNNING" }
    }
    "status" {
        if (Test-Api) { Write-Output "RUNNING" } else { Write-Output "DOWN" }
    }
    "list" {
        if (-not (Test-Api)) { Write-Output "DOWN: start it first"; exit 1 }
        & $OllamaExe list
    }
    default { Write-Output "usage: [start|stop|status|list]" }
}
