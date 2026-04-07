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
set PYTHON=C:\Users\juhyu\AppData\Local\Python\pythoncore-3.14-64\python.exe
set BOT_DIR=C:\SideProject\DiscordBot

echo === Register Discord Claude Bot Service ===
echo.

"%NSSM%" status %SERVICE_NAME% >nul 2>&1
if %errorlevel% equ 0 (
    echo Removing existing service...
    "%NSSM%" stop %SERVICE_NAME% >nul 2>&1
    "%NSSM%" remove %SERVICE_NAME% confirm
)

echo Installing service...
"%NSSM%" install %SERVICE_NAME% "%PYTHON%" bot.py
"%NSSM%" set %SERVICE_NAME% AppDirectory "%BOT_DIR%"
"%NSSM%" set %SERVICE_NAME% DisplayName "Discord Claude Bot"
"%NSSM%" set %SERVICE_NAME% Description "Discord Bot for remote Claude Code CLI"
"%NSSM%" set %SERVICE_NAME% Start SERVICE_AUTO_START
"%NSSM%" set %SERVICE_NAME% AppStdout "%BOT_DIR%\service_stdout.log"
"%NSSM%" set %SERVICE_NAME% AppStderr "%BOT_DIR%\service_stderr.log"
"%NSSM%" set %SERVICE_NAME% AppStdoutCreationDisposition 4
"%NSSM%" set %SERVICE_NAME% AppStderrCreationDisposition 4
"%NSSM%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM%" set %SERVICE_NAME% AppRotateBytes 5242880
"%NSSM%" set %SERVICE_NAME% AppRestartDelay 10000

echo.
echo === Starting service... ===
"%NSSM%" start %SERVICE_NAME%

echo.
echo [DONE] Service registered and started.
echo   - Service: %SERVICE_NAME%
echo   - Check: services.msc
echo   - Unregister: unregister_service.bat
echo.
pause
