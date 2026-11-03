# Setup Brave with Remote Debugging for ₹0 automated publishing
param(
    [switch]$RestartBrave
)

$shortcutPath = "C:\Users\mitta\OneDrive\Desktop\Personal - Brave.lnk"
$braveExe = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

$sh = New-Object -ComObject WScript.Shell
if (Test-Path $shortcutPath) {
    $sc = $sh.CreateShortcut($shortcutPath)
    Write-Host "Current Target: $($sc.TargetPath)"
    Write-Host "Current Arguments: $($sc.Arguments)"
    
    # Add remote-debugging-port if not present
    if ($sc.Arguments -notmatch "remote-debugging-port") {
        $newArgs = ($sc.Arguments + " --remote-debugging-port=9222").Trim()
        $sc.Arguments = $newArgs
        $sc.Save()
        Write-Host "Updated shortcut with arguments: $($sc.Arguments)"
    } else {
        Write-Host "Shortcut already has remote-debugging-port configured."
    }
} else {
    Write-Host "Desktop shortcut not found at $shortcutPath"
}

if ($RestartBrave) {
    Write-Host "Gracefully restarting Brave browser to enable remote debugging on port 9222..."
    Get-Process brave -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    Start-Process $braveExe -ArgumentList "--remote-debugging-port=9222 --restore-last-session"
    Start-Sleep -Seconds 3
    Write-Host "Brave restarted. Checking CDP port 9222..."
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 3
        Write-Host "CDP connected successfully! Browser: $($resp.Browser)"
    } catch {
        Write-Warning "Could not connect to CDP: $_"
    }
}

