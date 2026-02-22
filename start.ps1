# ============================================================
#  Seamless browser remote - One-Click Launcher (PowerShell)
#  Right-click -> "Run with PowerShell" or execute from terminal
# ============================================================

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Seamless browser remote - Launching"    -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# --- Configuration ---
$BravePath   = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
$CdpPort     = 9222
$ProfileDir  = Join-Path $env:LOCALAPPDATA "BraveSoftware\Brave-Browser\User Data"
$ServerPort  = 5000
$DefaultUrl  = "https://www.youtube.com/tv"

# Projector monitor position (adjust to your setup)
# Monitor 0: 4480,143 (1920x1080)
# Monitor 1: 6400,134 (1920x1080)
# Monitor 2: 2560,143 (1920x1080)
# Monitor 3: 0,0     (2560x1440 - primary)
$MonitorX = 6400
$MonitorY = 134

# --- Launch Brave ---
Write-Host "[1/3] Launching Brave Browser..." -ForegroundColor Yellow
Write-Host "      Monitor: ${MonitorX},${MonitorY}  |  CDP Port: $CdpPort"

$braveArgs = @(
    "--remote-debugging-port=$CdpPort",
    "--user-data-dir=`"$ProfileDir`"",
    "--start-fullscreen",
    "--window-position=$MonitorX,$MonitorY",
    "--no-first-run",
    "--disable-features=TranslateUI",
    "--autoplay-policy=no-user-gesture-required",
    $DefaultUrl
)

Start-Process -FilePath $BravePath -ArgumentList $braveArgs

# --- Wait ---
Write-Host "[2/3] Waiting for browser to initialize..." -ForegroundColor Yellow
Start-Sleep -Seconds 3

# --- Start Server ---
Write-Host "[3/3] Starting Python server..." -ForegroundColor Yellow
Write-Host ""

# Get local IPs
$localIp = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "Wi-Fi*","Ethernet*" -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "169.*" } |
            Select-Object -First 1).IPAddress

Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Remote UI available at:"                   -ForegroundColor Green
Write-Host "  Local:   http://localhost:$ServerPort"      -ForegroundColor White
if ($localIp) {
    Write-Host "  Mobile:  http://${localIp}:$ServerPort" -ForegroundColor White
}
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

Set-Location $PSScriptRoot
python server.py
