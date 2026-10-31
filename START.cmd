@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title Among Us Live - Telegram + Cloudflare
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 run.py %*
) else (
    python run.py %*
)
if errorlevel 1 (
    echo.
    echo Launch failed. Read the message above.
    pause
)
