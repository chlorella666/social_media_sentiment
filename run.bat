@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH
    pause
    exit /b 1
)

python -c "import streamlit" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖（约 1-2 分钟）...
    pip install -r requirements.txt
)

echo 正在启动社交媒体情感分析器...
python -m streamlit run app/main.py
pause
