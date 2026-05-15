' run_forever.vbs — pythonw 봇이 죽으면 5초 후 재시작 (무한 루프)
' 스크립트 위치 기반으로 작업 디렉토리 동적 설정 (어디서든 클론하면 바로 실행됨)
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

Dim botDir
botDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = botDir

' 로그 디렉토리 보장
Dim logDir
logDir = botDir & "\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

Dim logPath
logPath = logDir & "\watchdog.log"

' pythonw 경로 — 환경변수 PYTHONW_PATH가 있으면 그걸 쓰고, 없으면 PATH의 pythonw 사용
Dim pyExe
pyExe = WshShell.ExpandEnvironmentStrings("%PYTHONW_PATH%")
If pyExe = "%PYTHONW_PATH%" Or pyExe = "" Then
    pyExe = "pythonw"
End If

Set f = fso.OpenTextFile(logPath, 8, True)
f.WriteLine Now & " - run_forever started (cwd=" & botDir & ", py=" & pyExe & ")"
f.Close

Do While True
    Set f = fso.OpenTextFile(logPath, 8, True)
    f.WriteLine Now & " - launching " & pyExe & " bot.py"
    f.Close

    Dim exitCode
    exitCode = WshShell.Run("""" & pyExe & """ bot.py", 0, True)

    Set f = fso.OpenTextFile(logPath, 8, True)
    f.WriteLine Now & " - pythonw exited (code: " & exitCode & "), restarting in 5s"
    f.Close

    WScript.Sleep 5000
Loop
