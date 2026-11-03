$wsh = New-Object -ComObject WScript.Shell
$sc = $wsh.CreateShortcut("C:\Users\mitta\OneDrive\Desktop\Start X Agent.lnk")
$sc.TargetPath = "C:\Users\mitta\OneDrive\Desktop\X agent\start.bat"
$sc.WorkingDirectory = "C:\Users\mitta\OneDrive\Desktop\X agent"
$sc.IconLocation = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe,0"
$sc.Save()
Write-Host "Created Desktop shortcut: Start X Agent.lnk"

