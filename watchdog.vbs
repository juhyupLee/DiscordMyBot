Set objWMI = GetObject("winmgmts:\\.\root\cimv2")
Set colProcs = objWMI.ExecQuery("SELECT * FROM Win32_Process WHERE Name = 'pythonw.exe'")
If colProcs.Count = 0 Then
    Set WshShell = CreateObject("WScript.Shell")
    WshShell.CurrentDirectory = "C:\SideProject\DiscordBot"
    WshShell.Run "wscript run_forever.vbs", 0, False
End If
