# 停止 VibeRemote：仅精确匹配 viberemote 目录下的 server.py（不影响工作台 tools/server.py）
$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^pythonw?\.exe$' -and
    $_.CommandLine -match 'viberemote' -and
    $_.CommandLine -match 'server\.py'
}
if ($targets) {
    foreach ($t in $targets) {
        Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host ("已停止 PID " + $t.ProcessId)
    }
} else {
    Write-Host "未发现运行中的 VibeRemote 服务"
}