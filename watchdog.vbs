' watchdog.vbs — 봇의 단일 인스턴스 락(포트 39517)이 없으면 봇 다시 시작
' 다른 pythonw.exe와 구분하기 위해 프로세스 이름 대신 락 포트로 판단한다
Set objWMI = GetObject("winmgmts:\\.\root\StandardCimv2")
Set colLocks = objWMI.ExecQuery("SELECT OwningProcess FROM MSFT_NetTCPConnection WHERE LocalAddress = '127.0.0.1' AND LocalPort = 39517")
If colLocks.Count = 0 Then
    Set WshShell = CreateObject("WScript.Shell")
    Set fso = CreateObject("Scripting.FileSystemObject")
    Dim botDir
    botDir = fso.GetParentFolderName(WScript.ScriptFullName)
    WshShell.CurrentDirectory = botDir
    ' 등록된 작업이 있으면 관리자 권한을 유지하도록 작업 스케줄러로 실행
    If WshShell.Run("schtasks /run /tn DiscordClaudeBot", 0, True) <> 0 Then
        WshShell.Run "wscript """ & botDir & "\run_forever.vbs""", 0, False
    End If
End If
