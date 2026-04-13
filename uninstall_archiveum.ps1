param(
    [switch]$RemovePiper,
    [switch]$RemoveOllama,
    [switch]$KeepUploads,
    [switch]$KeepSettings,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $ProjectDir ".venv"
$DataDir = Join-Path $ProjectDir "archiveum_data"
$SettingsPath = Join-Path $ProjectDir "archiveum_settings.json"
$SttModelDir = Join-Path $ProjectDir "models\faster-whisper"
$PiperInstallDir = Join-Path $env:LOCALAPPDATA "Programs\Piper"
$OllamaInstallDir = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
$OllamaUserDir = Join-Path $env:USERPROFILE ".ollama"

function Write-Section([string]$Message) {
    Write-Host ""
    Write-Host "[Archiveum] $Message"
}

function Confirm-Step([string]$Prompt) {
    if ($Yes) {
        return $true
    }

    $answer = Read-Host "$Prompt [y/N]"
    return $answer -match "^[Yy]$"
}

function Remove-IfExists([string]$PathToRemove, [string]$Label) {
    if (-not (Test-Path -LiteralPath $PathToRemove)) {
        Write-Host "[Archiveum] Skipping $Label, not present."
        return
    }

    Write-Host "[Archiveum] Removing $Label"
    Remove-Item -LiteralPath $PathToRemove -Recurse -Force
}

function Stop-ArchiveumPython {
    $projectName = Split-Path -Leaf $ProjectDir
    $candidates = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue
    foreach ($proc in ($candidates | Where-Object { $_.CommandLine -and $_.CommandLine -like "*$projectName*" })) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Host "[Archiveum] Stopped running Archiveum Python process $($proc.ProcessId)."
        } catch {
            Write-Warning "Could not stop process $($proc.ProcessId): $($_.Exception.Message)"
        }
    }
}

function Remove-AutostartShortcut {
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = Join-Path $StartupDir "Archiveum.lnk"
    Remove-IfExists $ShortcutPath "autostart shortcut"
}

function Reset-DataDir {
    if (-not (Test-Path -LiteralPath $DataDir)) {
        return
    }

    if ($KeepUploads) {
        $entries = Get-ChildItem -LiteralPath $DataDir -Force -ErrorAction SilentlyContinue
        foreach ($entry in $entries) {
            if ($entry.Name -ieq "uploads") {
                continue
            }
            Remove-Item -LiteralPath $entry.FullName -Recurse -Force
        }
        Write-Host "[Archiveum] Kept uploaded files and removed the rest of archiveum_data."
        return
    }

    Remove-IfExists $DataDir "Archiveum data"
}

Write-Section "Uninstalling Archiveum from $ProjectDir"
Write-Host "[Archiveum] This removes Archiveum's local environment, cached data, helpers, and local speech model."
if ($RemovePiper) {
    Write-Host "[Archiveum] Piper removal is enabled."
}
if ($RemoveOllama) {
    Write-Host "[Archiveum] Ollama removal is enabled."
}

if (-not (Confirm-Step "Continue with Archiveum uninstall?")) {
    Write-Section "Cancelled"
    exit 0
}

Stop-ArchiveumPython
Remove-AutostartShortcut
Remove-IfExists $VenvDir "virtual environment"
Reset-DataDir
Remove-IfExists $SttModelDir "local speech model cache"

if (-not $KeepSettings) {
    Remove-IfExists $SettingsPath "settings file"
} else {
    Write-Host "[Archiveum] Keeping archiveum_settings.json"
}

if ($RemovePiper) {
    Remove-IfExists $PiperInstallDir "Piper install directory"
}

if ($RemoveOllama) {
    Remove-IfExists $OllamaInstallDir "Ollama install directory"
    Remove-IfExists $OllamaUserDir "Ollama user data"
}

Write-Section "Archiveum uninstall complete"
Write-Host "[Archiveum] To start fresh, run install_archiveum.ps1 again."
