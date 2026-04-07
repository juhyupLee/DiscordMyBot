@echo off
chcp 65001 >nul 2>&1
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Run as Administrator.
    pause
    exit /b 1
)

set SERVICE_NAME=DiscordClaudeBot
set NSSM=C:\SideProject\DiscordBot\tools\nssm.exe

echo === Unregister Discord Claude Bot Service ===
echo.

"%NSSM%" stop %SERVICE_NAME% >nul 2>&1
"%NSSM%" remove %SERVICE_NAME% confirm

echo.
echo [DONE] Service removed.
echo.
pause
