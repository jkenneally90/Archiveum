param(
    [switch]$EnableVoice,
    [switch]$LaunchAfterInstall,
    [switch]$EnableAutostart,
    [switch]$DisableAutostart,
    [switch]$DesktopStartShortcut,
    [switch]$DesktopStopShortcut,
    [int]$Port = 8000,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

if ($DisableAutostart) {
    $EnableAutostart = $false
} elseif (-not $PSBoundParameters.ContainsKey('EnableAutostart')) {
    $EnableAutostart = $true
}

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $ProjectDir ".venv"
$SettingsPath = Join-Path $ProjectDir "archiveum_settings.json"
$PythonInVenv = Join-Path $VenvDir "Scripts\python.exe"
$PipInVenv = Join-Path $VenvDir "Scripts\pip.exe"

function Write-Section([string]$Message) {
    Write-Host ""
    Write-Host "[Archiveum] $Message"
}

function Require-Command([string]$CommandName) {
    if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $CommandName"
    }
}

function Ensure-Venv {
    Write-Section "Creating virtual environment"
    & $PythonExe -m venv $VenvDir

    Write-Section "Upgrading pip tooling"
    & $PythonInVenv -m pip install --upgrade pip setuptools wheel

    Write-Section "Installing Python requirements"
    & $PipInVenv install -r (Join-Path $ProjectDir "requirements.txt")
}

function Resolve-PiperModelPath {
    $Candidates = @(
        (Join-Path $ProjectDir "piper-voices\en\en_GB\jenny_dioco\medium\en_GB-jenny_dioco-medium.onnx"),
        (Join-Path $ProjectDir "piper-voices\en\en_GB\northern_english_male\medium\en_GB-northern_english_male-medium.onnx"),
        (Join-Path $ProjectDir "models\piper\en_GB-northern_english_male-medium.onnx")
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            return $Candidate
        }
    }

    return $Candidates[0]
}

function Ensure-PiperBinary {
    # Check if piper is already on PATH
    $PiperOnPath = Get-Command "piper.exe" -ErrorAction SilentlyContinue
    if ($PiperOnPath) {
        Write-Host "[Archiveum] Piper found on PATH: $($PiperOnPath.Source)"
        return $PiperOnPath.Source
    }

    # Check if piper is in project tools directory (flat or in piper/ subdir)
    $ToolsDir = Join-Path $ProjectDir "tools"
    $LocalPiper = Join-Path $ToolsDir "piper.exe"
    $NestedPiper = Join-Path $ToolsDir "piper\piper.exe"

    if (Test-Path -LiteralPath $LocalPiper) {
        Write-Host "[Archiveum] Piper found locally at: $LocalPiper"
        return $LocalPiper
    }

    if (Test-Path -LiteralPath $NestedPiper) {
        Write-Host "[Archiveum] Piper found locally at: $NestedPiper"
        return $NestedPiper
    }

    # Download Piper for Windows
    Write-Section "Downloading Piper TTS for Windows"

    $PiperVersion = "1.2.0"
    $PiperZipName = "piper_windows_amd64.zip"
    $PiperUrl = "https://github.com/rhasspy/piper/releases/download/v$PiperVersion/$PiperZipName"
    $PiperZipPath = Join-Path $env:TEMP $PiperZipName

    try {
        Write-Host "[Archiveum] Downloading Piper v$PiperVersion from GitHub..."
        Invoke-WebRequest -Uri $PiperUrl -OutFile $PiperZipPath -UseBasicParsing

        Write-Host "[Archiveum] Extracting Piper to tools directory..."
        New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
        Expand-Archive -Path $PiperZipPath -DestinationPath $ToolsDir -Force

        # Clean up zip file
        Remove-Item -Path $PiperZipPath -Force -ErrorAction SilentlyContinue

        # Verify extraction
        $ExtractedPiper = Join-Path $ToolsDir "piper.exe"
        if (Test-Path -LiteralPath $ExtractedPiper) {
            Write-Host "[Archiveum] Piper installed successfully at: $ExtractedPiper"
            return $ExtractedPiper
        } else {
            # Try looking in a subdirectory if piper extracted to a subfolder
            $Subdirs = Get-ChildItem -Path $ToolsDir -Directory -Filter "piper*" | Select-Object -First 1
            if ($Subdirs) {
                $NestedPiper = Join-Path $Subdirs.FullName "piper.exe"
                if (Test-Path -LiteralPath $NestedPiper) {
                    # Move piper.exe up to tools dir
                    Move-Item -Path $NestedPiper -Destination $LocalPiper -Force
                    # Also move any DLLs
                    Get-ChildItem -Path $Subdirs.FullName -Filter "*.dll" | ForEach-Object {
                        Move-Item -Path $_.FullName -Destination $ToolsDir -Force -ErrorAction SilentlyContinue
                    }
                    Remove-Item -Path $Subdirs.FullName -Recurse -Force -ErrorAction SilentlyContinue
                    Write-Host "[Archiveum] Piper installed successfully at: $LocalPiper"
                    return $LocalPiper
                }
            }
            throw "Piper executable not found after extraction"
        }
    } catch {
        Write-Warning "Failed to download Piper automatically. You will need to install Piper manually."
        Write-Host "[Archiveum] Manual Piper installation:"
        Write-Host "  1. Download from: https://github.com/rhasspy/piper/releases"
        Write-Host "  2. Extract to: $ToolsDir"
        Write-Host "  3. Or add piper.exe to your system PATH"
        return "piper"
    }
}

function Resolve-SttModelPath {
    $Candidates = @(
        (Join-Path $ProjectDir "models\faster-whisper\base.en"),
        (Join-Path $ProjectDir "models\stt\base.en"),
        (Join-Path $ProjectDir "models\faster-whisper\small.en"),
        (Join-Path $ProjectDir "models\stt\small.en"),
        (Join-Path $ProjectDir "models\faster-whisper\tiny.en"),
        (Join-Path $ProjectDir "models\stt\tiny.en")
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            return $Candidate
        }
    }

    return (Join-Path $ProjectDir "models\faster-whisper\base.en")
}

function Ensure-LocalSttModel {
    if (-not $EnableVoice) {
        return
    }

    $PreferredModelPath = Resolve-SttModelPath
    if (Test-Path -LiteralPath $PreferredModelPath) {
        Write-Host "[Archiveum] Local STT model already present at: $PreferredModelPath"
        return
    }

    Write-Section "Downloading local speech-to-text model for Windows"
    $TargetRoot = Join-Path $ProjectDir "models\faster-whisper"
    $TargetModelDir = Join-Path $TargetRoot "base.en"
    New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null

    @'
from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import snapshot_download

target_dir = Path(os.environ["ARCHIVEUM_STT_TARGET"]).resolve()
target_dir.mkdir(parents=True, exist_ok=True)

snapshot_download(
    repo_id="Systran/faster-whisper-base.en",
    local_dir=str(target_dir),
    local_dir_use_symlinks=False,
)

print(f"[Archiveum] Speech model saved to {target_dir}")
'@ | ForEach-Object {
        $env:ARCHIVEUM_STT_TARGET = $TargetModelDir
        $_ | & $PythonInVenv -
    }
}

function Update-Settings {
    param([string]$PiperPath = "piper")

    Write-Section "Patching archiveum_settings.json for Windows"

    if (Test-Path -LiteralPath $SettingsPath) {
        $Settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json
    } else {
        $Settings = [pscustomobject]@{}
    }

    $Settings | Add-Member -NotePropertyName enable_voice -NotePropertyValue ([bool]$EnableVoice) -Force
    $Settings | Add-Member -NotePropertyName speak_responses -NotePropertyValue ([bool]$EnableVoice) -Force
    $Settings | Add-Member -NotePropertyName piper_command -NotePropertyValue $PiperPath -Force
    $Settings | Add-Member -NotePropertyName piper_model_path -NotePropertyValue (Resolve-PiperModelPath) -Force
    $Settings | Add-Member -NotePropertyName piper_device -NotePropertyValue "windows-default" -Force
    $Settings | Add-Member -NotePropertyName stt_model -NotePropertyValue (Resolve-SttModelPath) -Force
    $Settings | Add-Member -NotePropertyName port -NotePropertyValue $Port -Force

    $Settings | ConvertTo-Json -Depth 8 | Set-Content -Path $SettingsPath -Encoding UTF8
}

function Run-SelfTest {
    Write-Section "Running Archiveum self-test"
    & $PythonInVenv (Join-Path $ProjectDir "scripts\archiveum_self_test.py")
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Self-test reported warnings. The web UI may still work, but check the output above."
    }
}

function Maybe-Launch {
    if (-not $LaunchAfterInstall) {
        return
    }

    Write-Section "Launching Archiveum web app"
    $env:ARCHIVEUM_ENABLE_VOICE = if ($EnableVoice) { "1" } else { "0" }
    $env:ARCHIVEUM_PORT = "$Port"
    Start-Process -FilePath $PythonInVenv -ArgumentList "main.py" -WorkingDirectory $ProjectDir
    Write-Host "[Archiveum] Started in a new process. Open http://127.0.0.1:$Port/"
}

function Enable-Autostart {
    if (-not $EnableAutostart) {
        return
    }

    Write-Section "Setting up autostart on Windows"

    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = Join-Path $StartupDir "Archiveum.lnk"

    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = "powershell.exe"
    $Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\scripts\start_archiveum.ps1`""
    $Shortcut.WorkingDirectory = $ProjectDir
    $Shortcut.Description = "Start Archiveum and open web interface"
    $Shortcut.Save()

    Write-Host "[Archiveum] Created autostart shortcut at: $ShortcutPath"
}

function New-DesktopShortcuts {
    param(
        [switch]$CreateStartShortcut,
        [switch]$CreateStopShortcut
    )

    if (-not $CreateStartShortcut -and -not $CreateStopShortcut) {
        return
    }

    Write-Section "Creating desktop shortcuts"

    $DesktopDir = [Environment]::GetFolderPath("Desktop")
    $WshShell = New-Object -ComObject WScript.Shell

    if ($CreateStartShortcut) {
        $StartShortcutPath = Join-Path $DesktopDir "Start Archiveum.lnk"
        $StartShortcut = $WshShell.CreateShortcut($StartShortcutPath)
        $StartShortcut.TargetPath = "powershell.exe"
        $StartShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\scripts\start_archiveum.ps1`""
        $StartShortcut.WorkingDirectory = $ProjectDir
        $StartShortcut.Description = "Start Archiveum and open web interface"
        $StartShortcut.IconLocation = "%SystemRoot%\System32\shell32.dll,14"
        $StartShortcut.Save()
        Write-Host "[Archiveum] Created desktop shortcut: Start Archiveum"
    }

    if ($CreateStopShortcut) {
        $StopShortcutPath = Join-Path $DesktopDir "Stop Archiveum.lnk"
        $StopShortcut = $WshShell.CreateShortcut($StopShortcutPath)
        $StopShortcut.TargetPath = "powershell.exe"
        $StopShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\scripts\stop_archiveum.ps1`""
        $StopShortcut.WorkingDirectory = $ProjectDir
        $StopShortcut.Description = "Stop running Archiveum"
        $StopShortcut.IconLocation = "%SystemRoot%\System32\shell32.dll,28"
        $StopShortcut.Save()
        Write-Host "[Archiveum] Created desktop shortcut: Stop Archiveum"
    }
}

Require-Command $PythonExe

Write-Section "Project directory: $ProjectDir"
Write-Host "[Archiveum] Using Python: $PythonExe"
Write-Host "[Archiveum] Autostart enabled: $EnableAutostart"

Ensure-Venv
Ensure-LocalSttModel
$PiperPath = Ensure-PiperBinary
Update-Settings -PiperPath $PiperPath
Run-SelfTest
Enable-Autostart

# Desktop shortcuts
if ($DesktopStartShortcut -or $DesktopStopShortcut) {
    New-DesktopShortcuts -CreateStartShortcut:$DesktopStartShortcut -CreateStopShortcut:$DesktopStopShortcut
} else {
    Write-Host ""
    Write-Host "[Archiveum] Create desktop shortcuts for starting/stopping Archiveum? [y/N] " -NoNewline
    $desktopShortcuts = Read-Host
    if ($desktopShortcuts -match "^[Yy]$") {
        Write-Host "[Archiveum] Create Start Archiveum shortcut? [y/N] " -NoNewline
        $startShortcut = Read-Host
        Write-Host "[Archiveum] Create Stop Archiveum shortcut? [y/N] " -NoNewline
        $stopShortcut = Read-Host
        New-DesktopShortcuts -CreateStartShortcut:($startShortcut -match "^[Yy]$") -CreateStopShortcut:($stopShortcut -match "^[Yy]$")
    }
}

Maybe-Launch

Write-Section "Done"
Write-Host "[Archiveum] Settings file: $SettingsPath"
Write-Host "[Archiveum] Start manually with: $PythonInVenv main.py"
