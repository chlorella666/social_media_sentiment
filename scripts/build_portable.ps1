<#
.SYNOPSIS
构建「便携版试用包」（解压即用，Windows x64）。

依据：docs/便携版试用包方案.md v0.3（唯一执行依据）。
数据安全红线：白名单打包 + 敏感扫描断言，任一命中即失败。

用法：
    powershell -ExecutionPolicy Bypass -File scripts/build_portable.ps1            # 完整构建（含回归）
    powershell -ExecutionPolicy Bypass -File scripts/build_portable.ps1 -SkipRegression   # 跳过回归（迭代用）

输出：scratch/_portable_dist/（gitignored）
    social_media_sentence_v0.1_win_x64/   # 解压目录
    social_media_sentence_v0.1_win_x64.zip
    BUILD_REPORT.txt                       # 外发校验和（zip 本体 SHA-256）等
#>

param(
    [switch]$SkipRegression,
    [switch]$AllowDirty,
    [string]$Version = "v0.1_win_x64"
)

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
$PY_VER = "3.13.15"
$PY_TAG = "20260814"
$CACHE = Join-Path $ROOT "scratch\_portable_cache"
$DIST = Join-Path $ROOT "scratch\_portable_dist"
$STAGE = Join-Path $DIST "_stage"
$PKG_DIR = "social_media_sentence_$Version"
$PKG_PATH = Join-Path $STAGE $PKG_DIR
$ZIP = Join-Path $DIST "social_media_sentence_$Version.zip"
$RUNTIME_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/cpython-$PY_VER%2B$PY_TAG-x86_64-pc-windows-msvc-install_only.tar.gz"
$SUMS_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/SHA256SUMS"
$TARBALL = Join-Path $CACHE "python-$PY_VER-install_only.tar.gz"
$SUMS = Join-Path $CACHE "SHA256SUMS"

$TOP_WHITELIST = @(
    "app", ".streamlit", "data", "runtime", "requirements.txt",
    "README.md", "LICENSE", "使用说明.txt", "已知问题.txt",
    "启动应用.bat", "退出.bat", "SHA256SUMS.txt"
)
$FORBIDDEN = @(
    "^data\\[^\\]*[^.]",            # data/ 下除 .gitkeep 外的任何内容
    "^data\\\.gitkeep$",            # 占位：仅用于反向规则，不命中
    "llm_apikey", "\\.pem$", "\\cookies\\", "app\\.db$", "worker\\.pid$"
)

function Fail([string]$msg) {
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    exit 1
}

function Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 0. 前置校验
# ---------------------------------------------------------------------------
Step "前置校验"
$status = git -C $ROOT status --porcelain
if ($status) {
    if (-not $AllowDirty) {
        Fail "工作区未提交（git status 非空）。构建前必须先收敛代码：$($status -join '; ')"
    }
    Write-Host "[warn] -AllowDirty：跳过工作区干净校验（仅限不相关文档在途时使用）" -ForegroundColor Yellow
}

if (-not $SkipRegression) {
    Write-Host "运行全量回归（38/38 + 黄金集门槛）…"
    Push-Location $ROOT
    & python tests/run_regression.py --ci
    if ($LASTEXITCODE -ne 0) {
        Pop-Location
        Fail "全量回归未通过，终止构建"
    }
    Pop-Location
} else {
    Write-Host "已跳过回归（-SkipRegression）"
}
$leakyEnv = Get-ChildItem Env: | Where-Object { $_.Name -match "API_KEY|OPENAI|DEEPSEEK|WEIBO" }
if ($leakyEnv) {
    Fail "检测到敏感环境变量：$($leakyEnv.Name -join ', ')。请在干净环境执行构建"
}
if ([Environment]::Is64BitOperatingSystem -eq $false) {
    Fail "仅支持 64 位 Windows"
}

# ---------------------------------------------------------------------------
# 1. 获取自包含运行时（python-build-standalone）
# ---------------------------------------------------------------------------
Step "获取自包含 Python 运行时（python-build-standalone $PY_VER+$PY_TAG）"
New-Item -ItemType Directory -Force -Path $CACHE, $DIST, $STAGE | Out-Null
if (-not (Test-Path $TARBALL)) {
    Write-Host "下载运行时…"
    curl.exe -L -sS --retry 2 --max-time 600 -o $TARBALL $RUNTIME_URL
    if ($LASTEXITCODE -ne 0 -or (Get-Item $TARBALL -ErrorAction SilentlyContinue).Length -lt 1MB) {
        Write-Host "GitHub 下载失败/过慢，改用镜像（gh-proxy.com）…"
        Remove-Item $TARBALL -Force -ErrorAction SilentlyContinue
        curl.exe -L -sS --retry 2 --max-time 600 -o $TARBALL "https://gh-proxy.com/$RUNTIME_URL"
        if ($LASTEXITCODE -ne 0) { Fail "运行时下载失败" }
    }
    if ($LASTEXITCODE -ne 0) { Fail "运行时下载失败" }
}
if (-not (Test-Path $SUMS)) {
    curl.exe -L -sS -o $SUMS $SUMS_URL
    if ($LASTEXITCODE -ne 0) { Fail "校验和下载失败" }
}
$expected = (Get-Content $SUMS | Where-Object { $_ -match [regex]::Escape("cpython-$PY_VER+$PY_TAG-x86_64-pc-windows-msvc-install_only.tar.gz") } | Select-Object -First 1).Split(" ")[0]
if (-not $expected) { Fail "SHA256SUMS 中未找到运行时条目" }
$actual = (Get-FileHash $TARBALL -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected.ToLower()) {
    Fail "运行时校验和不匹配：$actual"
}
Write-Host "运行时校验和 OK"

# ---------------------------------------------------------------------------
# 2. 解压运行时 + 安装依赖（锁版本）
# ---------------------------------------------------------------------------
Step "准备 runtime/ 并安装依赖"
$RUNTIME = Join-Path $PKG_PATH "runtime"
if (Test-Path (Join-Path $RUNTIME "python.exe")) {
    Write-Host "复用已有 runtime（如需全新构建，删除 scratch\_portable_dist\_stage）"
} else {
    if (Test-Path $PKG_PATH) { Remove-Item -LiteralPath $PKG_PATH -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $RUNTIME | Out-Null
    tar.exe -xzf $TARBALL -C $RUNTIME --strip-components=1
    if ($LASTEXITCODE -ne 0) { Fail "运行时解压失败" }
    $py = Join-Path $RUNTIME "python.exe"
    if (-not (Test-Path $py)) { Fail "runtime/python.exe 不存在，解压结构异常" }
    & $py --version
    & $py -m pip --version
    if ($LASTEXITCODE -ne 0) { Fail "运行时 pip 不可用" }
    Write-Host "安装依赖（--no-cache-dir）…"
    & $py -m pip install --no-cache-dir -r (Join-Path $ROOT "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败" }
    & $py -m pip freeze | Set-Content (Join-Path $CACHE "requirements.lock_$Version.txt") -Encoding UTF8
    Write-Host "依赖锁定已保存：$CACHE\requirements.lock_$Version.txt"
}

# ---------------------------------------------------------------------------
# 3. 白名单复制源码
# ---------------------------------------------------------------------------
Step "清理运行时冗余（长路径 pyc/测试目录/streamlit 技能模板）"
Get-ChildItem -Path $RUNTIME -Recurse -Force -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $RUNTIME -Recurse -Force -File -Filter "*.pyc" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path (Join-Path $RUNTIME "Lib\site-packages") -Recurse -Force -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -in @("tests", "test") } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path (Join-Path $RUNTIME "Lib\site-packages\streamlit\.agents")) { Remove-Item -LiteralPath (Join-Path $RUNTIME "Lib\site-packages\streamlit\.agents") -Recurse -Force }
$maxLen = 0; $maxPath = ""
Get-ChildItem -Path $RUNTIME -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object { if ($_.FullName.Length -gt $maxLen) { $maxLen = $_.FullName.Length; $maxPath = $_.FullName } }
Write-Host "runtime 最长路径: $maxLen ($maxPath)"
if ($maxLen -gt 230) { Fail "runtime 最长路径超过 230，Windows 资源管理器解压会报「路径太长」，请检查清理是否生效" }
Step "按白名单复制源码"
foreach ($entry in @("app", ".streamlit", "requirements.txt", "README.md", "LICENSE")) {
    $src = Join-Path $ROOT $entry
    if (-not (Test-Path $src)) { Fail "白名单条目缺失：$entry" }
    Copy-Item -LiteralPath $src -Destination (Join-Path $PKG_PATH $entry) -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $PKG_PATH "data") | Out-Null
Copy-Item -LiteralPath (Join-Path $ROOT "data\.gitkeep") -Destination (Join-Path $PKG_PATH "data\.gitkeep") -Force
Set-Content -LiteralPath (Join-Path $PKG_PATH "data\.gitkeep") -Value "portable package placeholder" -Encoding ASCII
# 清理源码中的缓存字节码
Get-ChildItem -Path (Join-Path $PKG_PATH "app") -Recurse -Force -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 4. 生成启动/退出脚本与使用说明
# ---------------------------------------------------------------------------
Step "生成启动/退出脚本与使用说明"
$launcher = @'
@echo off
rem 保持系统默认代码页（bat 为 OEM/GBK 编码，避免乱码）
cd /d "%~dp0"

rem 数据目录兜底（zip 会丢空目录）
if not exist "data\logs" mkdir "data\logs"
if not exist "data\state" mkdir "data\state"
if not exist "data\reports" mkdir "data\reports"

rem 实例检测：端口 8501 已占用则提示退出（防重复双击）
netstat -ano | findstr "LISTENING" | findstr ":8501" >nul 2>nul
if not errorlevel 1 (
    echo [提示] 检测到应用已在运行（端口 8501 已占用），请勿重复启动。
    echo        已在浏览器打开的话直接使用即可；若确实无法访问，请先运行「退出.bat」。
    pause
    exit /b 1
)

echo 正在启动后台任务进程（隐藏窗口）...
start "" "%~dp0runtime\pythonw.exe" "%~dp0app\worker.py"

echo 正在启动社交媒体情感分析器，浏览器将自动打开（首次约 5~15 秒）...
"%~dp0runtime\python.exe" -m streamlit run "%~dp0app\main.py" --server.port 8501

echo.
echo 应用已退出。后台任务进程仍在运行，请双击「退出.bat」彻底结束。
pause
'@
$quitter = @'
@echo off
setlocal enabledelayedexpansion
rem 保持系统默认代码页（bat 为 OEM/GBK 编码，避免乱码）
cd /d "%~dp0"
echo 正在结束后台任务进程...
if exist "data\worker.pid" (
    set /p WPID=<"data\worker.pid"
    if defined WPID taskkill /F /PID !WPID! >nul 2>nul
)
rem 兜底：结束监听 8501 的进程
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8501"') do (
    if not "%%p"=="0" taskkill /F /PID %%p >nul 2>nul
)
echo 已结束。可安全关闭窗口。
pause
'@
Set-Content -LiteralPath (Join-Path $PKG_PATH "启动应用.bat") -Value $launcher -Encoding OEM
Set-Content -LiteralPath (Join-Path $PKG_PATH "退出.bat") -Value $quitter -Encoding OEM

$notice = @'
【社交媒体情感分析器 · 便携版试用包 v0.1】

三步开始（无需安装任何软件）：
  1. 解压本文件夹到本地普通目录（推荐桌面，不要放 OneDrive/Dropbox 等云同步目录）；
  2. 双击「启动应用.bat」，等浏览器自动打开（首次 5~15 秒）；
  3. 首次使用请阅读并确认《使用边界》，然后先点「✨ 看演示报告」，
     再走向导：品牌名随便填 → 渠道只勾「演示数据」→ 确认运行 → 约 1 分钟出报告。

如果提示“来自其他计算机 / 已阻止”：
  对 zip 压缩包右键 → 属性 → 勾选「解除锁定」→ 确定，再重新解压双击。
  杀软若拦截，选择「仍要运行」；本包为自包含 Python + 源码，无系统级安装行为。

数据安全须知（请务必阅读）：
  - 所有数据只存在本机，关闭后自动保留在 data/ 文件夹；不开启 LLM 时零数据出境；
  - 开启 LLM 精分析后，低置信度文本会发送给所选服务商（DeepSeek/OpenAI），
    请勿输入含个人敏感信息的内容；
  - 小红书渠道（进阶）会读取你的 Chrome 登录态；首轮请只用「演示数据」和 B站；
  - 不要把本文件夹放进云同步目录（data/ 会被自动上传）；
  - 不要整体压缩转发本文件夹——内含分析数据与加密凭据；分享请用导出的 Excel/HTML 报告；
  - 报告含平台用户原文，仅供自用，勿公开传播；
  - 反馈截图只截流程界面，涉及报告原文先遮挡；日志回传前自查一眼；
  - 侧边栏「数据管理」可一键清除全部数据；测试完删除整个文件夹即可。

常见问题（FAQ）：
  Q：双击后没反应/白屏？  A：确认 data/ 下有 logs 目录；重试前先双击「退出.bat」。
  Q：端口被占用？        A：先运行「退出.bat」，再重新双击「启动应用.bat」。
  Q：Word 报告生成慢？   A：首次渲染图表图片约 30~60 秒，属正常。
  Q：想用小红书？        A：见向导③渠道页「一键安装」提示；首轮不建议。
  Q：想卸载？            A：删除整个文件夹即可（无注册表、无系统服务）。

测试完成后请按反馈表回传：卡在哪一步 / 截图（遮挡原文）/ 是否顺利出报告；
出问题时把 data\logs\app.jsonl 一并回传，便于定位。
'@
Set-Content -LiteralPath (Join-Path $PKG_PATH "使用说明.txt") -Value ($notice.Replace("便携版试用包 v0.1", "便携版试用包 $Version")) -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $ROOT "scripts\portable_known_issues.txt") -Destination (Join-Path $PKG_PATH "已知问题.txt") -Force

# ---------------------------------------------------------------------------
# 5. 敏感扫描断言
# ---------------------------------------------------------------------------
Step "敏感扫描"
$violations = @()
Get-ChildItem -Path $PKG_PATH -Recurse -Force | ForEach-Object {
    $rel = $_.FullName.Substring($PKG_PATH.Length + 1).Replace("/", "\")
    if ($_.PSIsContainer) {
        if ($rel -match "^(data)\\.*" -and $rel -ne "data") { $violations += "data 目录含内容: $rel" }
        if ($rel -match "\\cookies\\") { $violations += "cookies 目录: $rel" }
    } else {
        foreach ($pat in $FORBIDDEN) {
            if ($rel -match $pat -and $rel -notmatch "^data\\.gitkeep$") {
                $violations += "$pat 命中: $rel"
            }
        }
    }
}
$top = Get-ChildItem -Path $PKG_PATH -Force | ForEach-Object { $_.Name }
$extra = $top | Where-Object { $_ -notin $TOP_WHITELIST }
if ($extra) { $violations += "顶层白名单外条目: $($extra -join ', ')" }
if ($violations) {
    $violations | ForEach-Object { Write-Host "  [敏感] $_" -ForegroundColor Red }
    Fail "敏感扫描命中，构建终止"
}
Write-Host "敏感扫描 0 命中 OK"

# ---------------------------------------------------------------------------
# 6. 压缩 + 校验和
# ---------------------------------------------------------------------------
Step "压缩并计算校验和"
$sumLines = Get-ChildItem -Path $PKG_PATH -Recurse -File -Force | Where-Object { $_.Name -ne "SHA256SUMS.txt" } | ForEach-Object {
    $rel = $_.FullName.Substring($PKG_PATH.Length + 1)
    $h = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    "$h  $rel"
}
Set-Content -LiteralPath (Join-Path $PKG_PATH "SHA256SUMS.txt") -Value $sumLines -Encoding UTF8
if (Test-Path $ZIP) { Remove-Item -LiteralPath $ZIP -Force }
Push-Location $STAGE
$tarOut = tar.exe -a -c -f $ZIP $PKG_DIR
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Write-Host "tar 压缩失败，回退 Compress-Archive…"
    Compress-Archive -Path (Join-Path $STAGE "$PKG_DIR\*") -DestinationPath $ZIP -CompressionLevel Optimal
    Pop-Location
}
$zipHash = (Get-FileHash $ZIP -Algorithm SHA256).Hash
$report = @"
便携版试用包构建报告
======================
版本: $Version
构建时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Python 运行时: python-build-standalone $PY_VER+$PY_TAG (x86_64-pc-windows-msvc install_only)
产物: $ZIP
zip 本体 SHA-256 (外发校验和，请经聊天/邮件等包外渠道单独发送): $zipHash
解压目录顶层条目: $($top -join ', ')
"@
Set-Content -LiteralPath (Join-Path $DIST "BUILD_REPORT.txt") -Value $report -Encoding UTF8
Write-Host ""
Write-Host "构建完成 ✅" -ForegroundColor Green
Write-Host "产物: $ZIP"
Write-Host "外发 SHA-256: $zipHash"
Write-Host "构建报告: $DIST\BUILD_REPORT.txt"
