[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidatePattern('^http://(127\.0\.0\.1|localhost):[0-9]{1,5}/?$')]
    [string]$DriverUrl = 'http://127.0.0.1:4723',
    [switch]$StartDriver,
    [string]$DriverScript,
    [switch]$OpenBrowser,
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = (Get-Command python -CommandType Application).Source
$dashboardUrl = "http://127.0.0.1:$Port"
$driverUri = [Uri]$DriverUrl
if ($driverUri.Port -lt 1 -or $driverUri.Port -gt 65535) {
    throw 'The driver URL must use a valid local port.'
}
$DriverUrl = "http://127.0.0.1:$($driverUri.Port)"

function Get-LocalJson {
    param([string]$Url)
    try { return Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 2 -MaximumRedirection 0 }
    catch { return $null }
}

function Test-DriverReady {
    $status = Get-LocalJson "$DriverUrl/status"
    return $null -ne $status -and $status.value.ready -eq $true
}

function Test-ObserverReady {
    $status = Get-LocalJson "$dashboardUrl/api/status"
    return $null -ne $status -and $status.dry_run -eq $true -and $null -ne $status.capture_limit -and $null -ne $status.quests
}

if (Test-ObserverReady) {
    Write-Host "Mapleleaf observer is already available at $dashboardUrl"
    if ($OpenBrowser) { Start-Process -FilePath $dashboardUrl }
    return
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already used by another service. Choose a different observer port with -Port."
}
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'Python was not found. Install Python 3.12 or newer and add it to PATH.'
}

$logDirectory = Join-Path $projectRoot 'runs\launcher'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$launchStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'

if (-not (Test-DriverReady)) {
    if (-not $StartDriver) {
        throw "The local Appium driver is not ready at $DriverUrl. Start the existing driver, or run .\start-observer.ps1 -StartDriver. This launcher will not reinstall drivers."
    }
    if ($driverUri.Port -ne 4723) {
        throw 'The existing driver script starts port 4723. Start a custom driver separately before using another -DriverUrl.'
    }
    if (Get-NetTCPConnection -LocalPort 4723 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 4723 is in use but its Appium status is not ready. Inspect that service before starting another driver.'
    }
    if (-not $DriverScript -or -not (Test-Path -LiteralPath $DriverScript -PathType Leaf)) {
        throw "The existing driver script was not found: $DriverScript. Supply its current path with -DriverScript."
    }
    $resolvedDriverScript = (Resolve-Path -LiteralPath $DriverScript).Path
    $shellCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if (-not $shellCommand) { $shellCommand = Get-Command powershell.exe -ErrorAction Stop }
    $driverOut = Join-Path $logDirectory "$launchStamp-driver.stdout.log"
    $driverErr = Join-Path $logDirectory "$launchStamp-driver.stderr.log"
    $driverArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $resolvedDriverScript + '"'))
    $driverProcess = Start-Process -FilePath $shellCommand.Source -ArgumentList $driverArguments -WorkingDirectory (Split-Path -Parent $resolvedDriverScript) -WindowStyle Hidden -RedirectStandardOutput $driverOut -RedirectStandardError $driverErr -PassThru
    $driverReady = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (Test-DriverReady) { $driverReady = $true; break }
        $driverProcess.Refresh()
        if ($driverProcess.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $driverReady) {
        throw "The driver did not become ready. Inspect $driverErr and $driverOut. Its launcher process ID was $($driverProcess.Id)."
    }
    Write-Host "Existing local driver started (launcher process $($driverProcess.Id))."
}

if ($Foreground) {
    Write-Host "Mapleleaf observer: $dashboardUrl"
    Write-Host 'Use Ctrl+C in this terminal to close the observer server.'
    if ($OpenBrowser) { Start-Process -FilePath $dashboardUrl }
    & $pythonExe -m mapleleaf.cli dashboard --port $Port --server $DriverUrl
    if ($LASTEXITCODE -ne 0) { throw "The observer ended with exit code $LASTEXITCODE." }
    return
}

$observerOut = Join-Path $logDirectory "$launchStamp-observer.stdout.log"
$observerErr = Join-Path $logDirectory "$launchStamp-observer.stderr.log"
$observerArguments = @('-m', 'mapleleaf.cli', 'dashboard', '--port', [string]$Port, '--server', $DriverUrl)
$observerProcess = Start-Process -FilePath $pythonExe -ArgumentList $observerArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $observerOut -RedirectStandardError $observerErr -PassThru
$observerReady = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if (Test-ObserverReady) { $observerReady = $true; break }
    $observerProcess.Refresh()
    if ($observerProcess.HasExited) { break }
    Start-Sleep -Milliseconds 250
}
if (-not $observerReady) {
    throw "The observer did not become ready. Inspect $observerErr and $observerOut. Its process ID was $($observerProcess.Id)."
}

Write-Host "Mapleleaf observer: $dashboardUrl"
Write-Host "Background observer process: $($observerProcess.Id)"
Write-Host 'Captures are stopped until you press Start Observer. The dashboard sends no game input.'
Write-Host "Launcher logs: $logDirectory"
if ($OpenBrowser) { Start-Process -FilePath $dashboardUrl }
