@echo off
title QuoteBot - Zoho Invoice Automation

echo.
echo  ==========================================
echo    QuoteBot - Zoho Invoice Automation
echo  ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not installed.
    echo  Get it from https://python.org
    pause
    exit /b
)

echo  Installing packages...
pip install -r requirements.txt --quiet

echo  Starting QuoteBot...
echo.

REM Load credentials from credentials.env if it exists
if exist credentials.env (
    for /f "tokens=1,2 delims==" %%a in (credentials.env) do set %%a=%%b
)

python app.py
pause
