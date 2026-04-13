# Stop Archiveum
# This script kills any running Archiveum processes

$ProjectDir = $PWD.Path

Write-Host "Stopping Archiveum..."

# Kill any Archiveum python processes
$existing = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*main.py*" }
if ($existing) {
    $existing | Stop-Process -Force
    Write-Host "Archiveum stopped."
} else {
    Write-Host "No running Archiveum process found."
}

# Also check for any process on the default port
$portProcess = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
if ($portProcess) {
    $process = Get-Process -Id $portProcess -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -eq "python") {
        Stop-Process -Id $portProcess -Force
        Write-Host "Stopped process on port 8000."
    }
}

Start-Sleep -Seconds 1
