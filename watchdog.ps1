$appDir  = "e:\Compagnies\Dumoulin Solutions\Sonoria\Developpement\Combine Leadlists"
$python  = "C:\Python314\python.exe"
$pidFile = Join-Path $appDir "server.pid"
$log     = Join-Path $appDir "watchdog_log.txt"

function Write-Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content $log $line
}

Write-Log "Watchdog started"

while ($true) {
    $listening = netstat -ano | Select-String "127.0.0.1:5000.*LISTENING"
    if (-not $listening) {
        Write-Log "App down - restarting..."
        Start-Process -FilePath $python -ArgumentList "-m waitress --host=127.0.0.1 --port=5000 --threads=8 app:app" -WorkingDirectory $appDir -WindowStyle Hidden
        Start-Sleep -Seconds 5
        $found = netstat -ano | Select-String "127.0.0.1:5000.*LISTEN"
        if ($found) {
            $newpid = ($found -split "\s+")[-1].Trim()
            Set-Content $pidFile $newpid
            Write-Log "App restarted - PID: $newpid"
        } else {
            Write-Log "ERROR: app failed to start"
        }
    }
    Start-Sleep -Seconds 30
}
