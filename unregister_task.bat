@echo off
set TASK_NAME=DiscordClaudeBot
taskkill /f /im pythonw.exe >nul 2>&1
schtasks /delete /tn "%TASK_NAME%" /f
echo Done.
pause
