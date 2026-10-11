Option Explicit
Dim fso, sh, leads, pythonw, python, exe, code
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("Wscript.Shell")
leads = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonw = fso.BuildPath(leads, ".venv\Scripts\pythonw.exe")
python = fso.BuildPath(leads, ".venv\Scripts\python.exe")
If fso.FileExists(pythonw) Then
  exe = pythonw
ElseIf fso.FileExists(python) Then
  exe = python
Else
  WScript.Echo "venv python not found"
  WScript.Quit 1
End If
sh.CurrentDirectory = leads
code = sh.Run(Chr(34) & exe & Chr(34) & " -m app.supervisor run", 0, True)
If code <> 0 Then
  On Error Resume Next
  Dim logDir, logPath, ts
  logDir = fso.BuildPath(leads, "logs")
  If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
  logPath = fso.BuildPath(logDir, "supervisor.log")
  Set ts = fso.OpenTextFile(logPath, 8, True)
  ts.WriteLine "supervisor exit " & code
  ts.Close
  On Error GoTo 0
End If
WScript.Quit code
