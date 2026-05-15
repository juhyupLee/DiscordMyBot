' watchdog.vbs — pythonw가 없으면 run_forever.vbs 다시 시작
Set objWMI = GetObject("winmgmts:\\.\root\cimv2")
Set colProcs = objWMI.ExecQuery("SELECT * FROM Win32_Process WHERE Name = 'pythonw.exe'")
If colProcs.Count = 0 Then
    Set WshShell = CreateObject("WScript.Shell")
    Set fso = CreateObject("Scripting.FileSystemObject")
    Dim botDir
    botDir = fso.GetParentFolderName(WScript.ScriptFullName)
    WshShell.CurrentDirectory = botDir
    WshShell.Run "wscript """ & botDir & "\run_forever.vbs""", 0, False
End If
