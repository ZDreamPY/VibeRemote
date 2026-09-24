# Stop VibeRemote: match only this project's processes (server.py or packaged VibeRemote.exe),
# never touch unrelated services like the workbench tools/server.py
$targets = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'viberemote' -and $_.CommandLine -match 'server\.py') -or
    $_.Name -match '^VibeRemote\.exe$'
}
if ($targets) {
    foreach ($t in $targets) {
        Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host ("Stopped PID " + $t.ProcessId)
    }
} else {
    Write-Host "No running VibeRemote service found"
}
