param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\LiveCaptionEveryTab",
    [string]$Distro = "Ubuntu-22.04",
    [switch]$SkipChromeInstall
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run install-windows-oneclick.bat as administrator."
    }
}

function Get-ChromePath {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    $appPath = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    if (Test-Path $appPath) {
        $value = (Get-ItemProperty $appPath)."(default)"
        if ($value -and (Test-Path $value)) { return $value }
    }
    return $null
}

function Install-Chrome {
    if (Get-ChromePath) { return }
    if ($SkipChromeInstall) { throw "Chrome is not installed and -SkipChromeInstall was passed." }

    Write-Host "Chrome not found; installing Chrome..."
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id Google.Chrome --exact --silent --accept-package-agreements --accept-source-agreements
        if (Get-ChromePath) { return }
    }

    $installer = Join-Path $env:TEMP "ChromeSetup.exe"
    Invoke-WebRequest -Uri "https://dl.google.com/chrome/install/latest/chrome_installer.exe" -OutFile $installer -UseBasicParsing
    $proc = Start-Process -FilePath $installer -ArgumentList "/silent", "/install" -Wait -PassThru
    if ($proc.ExitCode -ne 0) { throw "Chrome installer failed with exit code $($proc.ExitCode)." }
    if (-not (Get-ChromePath)) { throw "Chrome install completed but chrome.exe was not found." }
}

function Ensure-Wsl {
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) { throw "wsl.exe was not found. This installer requires Windows 10/11 with WSL2 support." }

    $distros = (& wsl.exe -l -q 2>$null) -replace "`0","" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    if ($distros -contains $Distro) { return }

    Write-Host "Installing WSL distro $Distro..."
    & wsl.exe --install -d $Distro --no-launch
    if ($LASTEXITCODE -ne 0) {
        throw "WSL install failed. Reboot Windows if WSL asks for it, then run this installer again."
    }
}

function Invoke-WslRoot([string]$Command) {
    & wsl.exe -d $Distro -u root -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) { throw "WSL command failed: $Command" }
}

function To-WslPath([string]$Path) {
    $out = & wsl.exe -d $Distro -u root -- wslpath -a "$Path"
    if ($LASTEXITCODE -ne 0) { throw "wslpath failed for $Path" }
    return ($out | Select-Object -First 1).Trim()
}

function Copy-Extension([string]$SourceRoot, [string]$DestinationRoot) {
    if (Test-Path $DestinationRoot) {
        $backup = "$DestinationRoot.bak-$(Get-Date -Format yyyyMMddHHmmss)"
        Move-Item -Path $DestinationRoot -Destination $backup -Force
        Write-Host "Previous extension install moved to: $backup" -ForegroundColor Yellow
    }
    New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
    $robocopyArgs = @(
        (Join-Path $SourceRoot "extension"),
        $DestinationRoot,
        "/MIR",
        "/XD", "__pycache__",
        "/XF", "*.pyc", "*.pem", ".DS_Store"
    )
    & robocopy @robocopyArgs | Out-Host
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE." }
}

function Write-Launcher([string]$ChromePath, [string]$ExtensionDir, [string]$InstallRoot) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $launcher = Join-Path $desktop "Live Caption Chrome.cmd"
    $profile = Join-Path $InstallRoot "ChromeProfile"
    $body = @"
@echo off
set "CHROME=$ChromePath"
set "EXTENSION=$ExtensionDir"
set "PROFILE=$profile"
if not exist "%CHROME%" set "CHROME=chrome.exe"
start "" "%CHROME%" --user-data-dir="%PROFILE%" --no-first-run --disable-extensions-except="%EXTENSION%" --load-extension="%EXTENSION%" "https://www.youtube.com/"
"@
    Set-Content -Path $launcher -Value $body -Encoding ASCII
    Write-Host "Launcher: $launcher" -ForegroundColor Green
}

Assert-Admin

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageRoot = Resolve-Path (Join-Path $scriptDir "..")
$extensionDir = Join-Path $InstallRoot "extension"

Write-Step "Install Chrome"
Install-Chrome
$chromePath = Get-ChromePath
Write-Host "Chrome: $chromePath" -ForegroundColor Green

Write-Step "Ensure WSL2"
Ensure-Wsl
Invoke-WslRoot "true"

Write-Step "Copy Live Caption into WSL"
$sourceWsl = To-WslPath $packageRoot
Invoke-WslRoot "set -e; mkdir -p /root; if [ -e /root/LiveCaptionEveryTab ]; then mv /root/LiveCaptionEveryTab /root/LiveCaptionEveryTab.bak-\$(date +%Y%m%d%H%M%S); fi; cp -a '$sourceWsl' /root/LiveCaptionEveryTab"
Write-Host "WSL project: /root/LiveCaptionEveryTab" -ForegroundColor Green

Write-Step "Install CUDA stack inside WSL"
Invoke-WslRoot "set -e; cd /root/LiveCaptionEveryTab; bash bridge/cuda/install_cuda_wsl.sh"

Write-Step "Register Windows native host"
Invoke-WslRoot "set -e; cd /root/LiveCaptionEveryTab; LCC_WSL_DISTRO='$Distro' LCC_WSL_USER=root LCC_ROOT=/root/LiveCaptionEveryTab LCC_CUDA_STACK_CMD=/root/LiveCaptionEveryTab/bridge/cuda/lcc_cuda_stack.sh LCC_NATIVE_PYTHON=/root/.venvs/lcc-asr/bin/python bash extension/native-host/install-host-windows-wsl.sh"

Write-Step "Copy extension and create launcher"
Copy-Extension -SourceRoot $packageRoot -DestinationRoot $extensionDir
Write-Launcher -ChromePath $chromePath -ExtensionDir $extensionDir -InstallRoot $InstallRoot

Write-Host ""
Write-Host "Live Caption is installed." -ForegroundColor Green
Write-Host "Use the desktop launcher, then the popup Bridge Start/Stop buttons." -ForegroundColor Green
Write-Host "If Gemma asks for Hugging Face access, set HF_TOKEN and rerun this installer." -ForegroundColor Yellow
