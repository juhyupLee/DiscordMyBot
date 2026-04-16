Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = "C:\SideProject\DiscordBot"

Dim logPath
logPath = "C:\SideProject\DiscordBot\logs\watchdog.log"

Set f = fso.OpenTextFile(logPath, 8, True)
f.WriteLine Now & " - run_forever started"
f.Close

Do While True
    Set f = fso.OpenTextFile(logPath, 8, True)
    f.WriteLine Now & " - launching pythonw bot.py"
    f.Close

    Dim exitCode
    exitCode = WshShell.Run("""C:\Users\juhyu\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"" bot.py", 0, True)

    Set f = fso.OpenTextFile(logPath, 8, True)
    f.WriteLine Now & " - pythonw exited (code: " & exitCode & "), restarting in 5s"
    f.Close

    WScript.Sleep 5000
Loop
