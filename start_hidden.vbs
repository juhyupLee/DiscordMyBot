If WScript.Arguments.Named.Exists("elevated") Then
    Set WshShell = CreateObject("WScript.Shell")
    WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
    WshShell.Run "wscript run_forever.vbs", 0, False
Else
    CreateObject("Shell.Application").ShellExecute "wscript.exe", """" & WScript.ScriptFullName & """ /elevated", "", "runas", 0
End If
