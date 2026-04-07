@echo off
echo Stopping Discord Bot...
taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq *bot.py*" >nul 2>&1
wmic process where "commandline like '%%bot.py%%' and name='pythonw.exe'" call terminate >nul 2>&1
echo Done.
pause
