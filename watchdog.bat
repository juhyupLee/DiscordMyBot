@echo off
REM Discord 봇 프로세스(pythonw.exe) 감시 - 죽었으면 재시작
REM 작업 디렉토리는 이 스크립트가 있는 폴더로 자동 설정됨
tasklist /FI "IMAGENAME eq pythonw.exe" 2>NUL | find /I "pythonw.exe" >NUL
if %ERRORLEVEL% NEQ 0 (
    cd /d "%~dp0"
    if defined PYTHONW_PATH (
        start "" "%PYTHONW_PATH%" bot.py
    ) else (
        start "" pythonw bot.py
    )
)
