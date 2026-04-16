@echo off
REM Discord 봇 프로세스(pythonw.exe) 감시 - 죽었으면 재시작
tasklist /FI "IMAGENAME eq pythonw.exe" 2>NUL | find /I "pythonw.exe" >NUL
if %ERRORLEVEL% NEQ 0 (
    cd /d C:\SideProject\DiscordBot
    start "" "C:\Users\juhyu\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" bot.py
)
