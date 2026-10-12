# Keeps the web app (port 8010) AND the ComfyUI backend running. If either stops answering, start
# it again. Started hidden by start_app.bat; safe to run twice (the second copy exits).
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "app_server.log"
$lockFile = Join-Path $root "app_watchdog.lock"

# ComfyUI: the backend python that has torch, its models in the Comfy Desktop shared folder
# (extra_model_paths.yaml in the ComfyUI folder), and the shared input/output folders the app uses.
$comfyDir = if ($env:COMFYUI_DIR) { $env:COMFYUI_DIR } else { Join-Path $root "ComfyUI (1)\ComfyUI" }
$comfyPy = Join-Path $comfyDir ".venv\Scripts\python.exe"
$shared = Join-Path $env:LOCALAPPDATA "Comfy-Desktop\ComfyUI-Shared"
$desktopExe = "C:\Program Files\Comfy Desktop\Comfy Desktop.exe"

# one watchdog at a time
if (Test-Path $lockFile) {
    $old = Get-Content $lockFile
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) { exit }
}
Set-Content $lockFile $PID

function Test-Url($url) {
    try { (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 }
    catch { $false }
}

function Test-Comfy {
    foreach ($port in 8188, 8189, 8190, 8191) {
        if (Test-Url "http://127.0.0.1:$port/queue") { return $true }
    }
    return $false
}

# Ollama must run gemma on the graphics card. On 2026-09-22 an Ollama auto-update started its server
# while the installer was still copying files; it found no GPU and ran gemma on the CPU all day (every
# line / translation 30s+, runs stuck at "대기"). If its log says the compute device is the CPU, restart it.
$ollamaLog = Join-Path $env:LOCALAPPDATA "Ollama\server.log"
$ollamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
$lastOllamaFix = [datetime]::MinValue
function Test-OllamaOnCpu {
    if (-not (Test-Path $ollamaLog)) { return $false }
    $line = Select-String -Path $ollamaLog -Pattern 'msg="inference compute"' | Select-Object -Last 1
    return ($line -and $line.Line -match 'library=cpu')
}

$comfyMisses = 0
while ($true) {
    if ((Test-OllamaOnCpu) -and ((Get-Date) - $lastOllamaFix).TotalMinutes -gt 10 -and (Test-Path $ollamaApp)) {
        Add-Content $log "[watchdog $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Ollama is running on the CPU, restarting it"
        Get-Process -Name "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 2
        Start-Process -FilePath $ollamaApp -WindowStyle Hidden
        $lastOllamaFix = Get-Date
        Start-Sleep -Seconds 15
    }
    # 2026-09-23: a Claude app update closed Ollama along with everything else; start it if it is gone
    if (-not (Get-Process -Name "ollama" -ErrorAction SilentlyContinue) -and (Test-Path $ollamaApp) -and
        ((Get-Date) - $lastOllamaFix).TotalMinutes -gt 2) {
        Add-Content $log "[watchdog $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Ollama not running, starting it"
        Start-Process -FilePath $ollamaApp -WindowStyle Hidden
        $lastOllamaFix = Get-Date
        Start-Sleep -Seconds 15
    }
    if (-not (Test-Url "http://127.0.0.1:8010/")) {
        Add-Content $log "[watchdog $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] app not answering, starting it"
        Start-Process -FilePath $py -ArgumentList @("-m", "uvicorn", "app.main:app", "--port", "8010", "--host", "127.0.0.1") `
            -WorkingDirectory $root -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $root "app_server.out.log") `
            -RedirectStandardError (Join-Path $root "app_server.err.log")
        Start-Sleep -Seconds 20
    }

    # ComfyUI takes ~1 minute to boot, so only act after several misses in a row
    if (Test-Comfy) { $comfyMisses = 0 } else { $comfyMisses++ }
    # ComfyUI runs as the Comfy Desktop app (user's choice, 2026-09-22). Only if the app is up but its
    # server still has not answered after several more minutes, start the same backend by hand.
    if ($comfyMisses -ge 4) {
        $running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*ComfyUI*main.py*" }
        $desktop = Get-Process -Name "Comfy Desktop" -ErrorAction SilentlyContinue
        if (-not $running -and -not $desktop -and (Test-Path $desktopExe)) {
            Add-Content $log "[watchdog $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ComfyUI not answering, opening Comfy Desktop"
            Start-Process -FilePath $desktopExe
            $comfyMisses = 0
            Start-Sleep -Seconds 90
        } elseif (-not $running -and $comfyMisses -ge 16 -and (Test-Path $comfyPy)) {
            Add-Content $log "[watchdog $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Comfy Desktop has no server, starting the backend directly"
            Start-Process -FilePath $comfyPy -ArgumentList @("-s", "main.py", "--enable-manager", "--listen", "127.0.0.1", "--port", "8188",
                "--input-directory", (Join-Path $shared "input"), "--output-directory", (Join-Path $shared "output")) `
                -WorkingDirectory $comfyDir -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $root "comfy_manual.log") `
                -RedirectStandardError (Join-Path $root "comfy_manual.log.err")
            $comfyMisses = 0
            Start-Sleep -Seconds 90
        } elseif ($running) {
            $comfyMisses = 0
        }
    }
    Start-Sleep -Seconds 15
}
