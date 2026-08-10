@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ and enable "Add to PATH".
    pause
    exit /b 1
)

python -c "import streamlit" >nul 2>nul
if errorlevel 1 (
    echo [INFO] First run: installing dependencies, this may take 1-2 minutes...
    pip install -r requirements.txt
)

echo Starting background task worker (hidden window)...
start "" pythonw "%~dp0app\worker.py"
echo Starting Social Media Sentiment Analyzer...
python -m streamlit run app/main.py
pause
