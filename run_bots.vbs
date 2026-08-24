Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c cd /d """ & WshShell.CurrentDirectory & """ && pythonw main.py", 0, False