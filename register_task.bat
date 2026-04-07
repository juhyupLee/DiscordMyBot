@echo off
set TASK_NAME=DiscordClaudeBot
set PYTHON=C:\Users\juhyu\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe
set BOT_DIR=C:\SideProject\DiscordBot
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
schtasks /create /tn "%TASK_NAME%" /tr "\"%PYTHON%\" \"%BOT_DIR%\bot.py\"" /sc onlogon /rl highest /f
echo Done. Starting now...
schtasks /run /tn "%TASK_NAME%"
pause
