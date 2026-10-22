Option Explicit
Dim fso, sh, base, home, verFile, ver, py, script, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
home = sh.ExpandEnvironmentStrings("%USERPROFILE%")
verFile = home & "\.workbuddy\binaries\python\versions\current"
If Not fso.FileExists(verFile) Then
  MsgBox "WorkBuddy Python was not found. Please run the installer again.", vbExclamation, "WorkBuddy Switch"
  WScript.Quit 1
End If
ver = Trim(fso.OpenTextFile(verFile, 1).ReadAll)
py = home & "\.workbuddy\binaries\python\versions\" & ver & "\pythonw.exe"
script = base & "\wbs.py"
If Not fso.FileExists(py) Or Not fso.FileExists(script) Then
  MsgBox "WorkBuddy Python or wbs.py was not found. Please run the installer again.", vbExclamation, "WorkBuddy Switch"
  WScript.Quit 1
End If
cmd = Chr(34) & py & Chr(34) & " " & Chr(34) & script & Chr(34) & " watch --quiet"
sh.Run cmd, 0, False
