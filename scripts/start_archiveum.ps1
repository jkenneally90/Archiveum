# Start Archiveum and open web interface
# This script is called by the autostart shortcut

param(
    [int]$Port = 8000
)

$ProjectDir = $PWD.Path
$VenvDir = Join-Path $ProjectDir ".venv"
$PythonInVenv = Join-Path $VenvDir "Scripts\python.exe"

# Kill any existing Archiveum process
$existing = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*main.py*" }
if ($existing) {
    Write-Host "Killing existing Archiveum process..."
    $existing | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# Start Archiveum in background
Write-Host "Starting Archiveum..."
$process = Start-Process -FilePath $PythonInVenv -ArgumentList "main.py" -WorkingDirectory $ProjectDir -PassThru -NoNewWindow

# Wait a bit for the server to start
Start-Sleep -Seconds 5

# Check if process is still running
if (-not $process.HasExited) {
    Write-Host "Archiveum started successfully."
    # Open browser
    $url = "http://127.0.0.1:$Port/"
    Start-Process $url
} else {
    Write-Host "Failed to start Archiveum. Process exited."
}

# Keep the script running briefly to ensure browser opens
Start-Sleep -Seconds 2