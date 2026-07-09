@echo off
chcp 65001 >nul
echo ========================================
echo   Pipe Monitor - WeChat Bot Launcher
echo   Using Python 3.11 for wechaty compat
echo ========================================
echo.

REM Check Python 3.11
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.11 not found. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Create venv if not exists
if not exist "venv311" (
    echo Creating Python 3.11 virtual environment...
    py -3.11 -m venv venv311
)

REM Activate and install
call venv311\Scripts\activate.bat
echo Installing dependencies...
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

echo.
echo Starting application...
echo (WeChat bot will start if WECHATY_PUPPET_TOKEN is set)
echo.
python app.py

pause
