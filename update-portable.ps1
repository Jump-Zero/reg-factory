param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir,
    [int]$ProcessId = 0,
    [string]$ResultPath = "",
    [string]$ListenHost = "127.0.0.1",
    [int]$ListenPort = 8799
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$parentDir = Split-Path -Parent $InstallDir
if ([string]::IsNullOrWhiteSpace($parentDir) -or $InstallDir -eq $parentDir) {
    throw "Refusing to update an unsafe installation path: $InstallDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "reg-factory.exe"))) {
    throw "Portable executable not found: $InstallDir"
}
Set-Location -LiteralPath $parentDir

# Never spend or depend on a registration proxy while downloading updates.
foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}
$env:NO_PROXY = $env:no_proxy = "127.0.0.1,localhost,::1,github.com,api.github.com,uploads.github.com"

function Write-UpdateResult {
    param(
        [string]$Status,
        [string]$Message,
        [string]$CurrentVersion = "",
        [string]$TargetVersion = ""
    )
    if ([string]::IsNullOrWhiteSpace($ResultPath)) { return }
    $resultDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($ResultPath))
    if (-not [string]::IsNullOrWhiteSpace($resultDir)) {
        New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
    }
    @{
        status = $Status
        message = $Message
        current_version = $CurrentVersion
        target_version = $TargetVersion
        updated_at = (Get-Date).ToString("s")
    } | ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding UTF8
}

function Get-PackageVersion {
    param([string]$PackageDir)
    foreach ($relativePath in @("VERSION", "_internal\VERSION")) {
        $path = Join-Path $PackageDir $relativePath
        if (Test-Path -LiteralPath $path) {
            return (Get-Content -Raw -LiteralPath $path).Trim().TrimStart("v")
        }
    }
    return ""
}

function Invoke-Download {
    param([string]$Uri, [string]$OutFile)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    $lastError = ""
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
        try {
            Write-Output "Downloading (attempt $attempt/4): $Uri"
            if ($null -ne $curl) {
                & $curl.Source -L --fail --silent --show-error --retry 2 --retry-delay 2 --connect-timeout 20 --output $OutFile $Uri
                if ($LASTEXITCODE -ne 0) { throw "curl exited with code $LASTEXITCODE" }
            } else {
                Invoke-WebRequest -Uri $Uri -OutFile $OutFile -TimeoutSec 900
            }
            if ((Test-Path -LiteralPath $OutFile) -and (Get-Item -LiteralPath $OutFile).Length -gt 0) {
                return
            }
            throw "downloaded file is empty"
        } catch {
            $lastError = $_.Exception.Message
            if ($attempt -lt 4) { Start-Sleep -Seconds ([Math]::Min(8, $attempt * 2)) }
        }
    }
    throw "Download failed after 4 attempts: $lastError"
}

function Get-LatestRelease {
    $lastError = ""
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            return Invoke-RestMethod -Uri "https://api.github.com/repos/tiantianGPU/reg-factory/releases/latest" -Headers @{ Accept = "application/vnd.github+json" } -TimeoutSec 30
        } catch {
            $lastError = $_.Exception.Message
            if ($attempt -lt 3) { Start-Sleep -Seconds ($attempt * 2) }
        }
    }
    throw "Unable to query the latest GitHub Release: $lastError"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("reg-factory-portable-update-" + [guid]::NewGuid())
$archivePath = Join-Path $tempRoot "latest.zip"
$extractRoot = Join-Path $tempRoot "extract"
$backupDir = Join-Path $parentDir (".reg-factory-backup-" + [guid]::NewGuid())
$movedOld = $false
$movedNew = $false
$stoppedOld = $false
$newProcess = $null
$currentVersion = Get-PackageVersion $InstallDir
$targetVersion = ""

New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
Write-UpdateResult -Status "checking" -Message "Checking the latest Release" -CurrentVersion $currentVersion
try {
    $release = Get-LatestRelease
    $targetVersion = ([string]$release.tag_name).Trim().TrimStart("v")
    if ([string]::IsNullOrWhiteSpace($targetVersion)) {
        throw "Latest GitHub Release has no version tag"
    }
    $isUpToDate = $currentVersion -eq $targetVersion
    try {
        if (-not [string]::IsNullOrWhiteSpace($currentVersion) -and
            ([version]$currentVersion -ge [version]$targetVersion)) {
            $isUpToDate = $true
        }
    } catch {}
    if ($isUpToDate) {
        $message = if ($currentVersion -eq $targetVersion) {
            "Already up to date: v$targetVersion"
        } else {
            "Current v$currentVersion is newer than latest Release v$targetVersion"
        }
        Write-UpdateResult -Status "up_to_date" -Message $message -CurrentVersion $currentVersion -TargetVersion $targetVersion
        Write-Output $message
        exit 0
    }
    $asset = @($release.assets) | Where-Object {
        $_.name -match '^reg-factory-windows-x64-.*\.zip$'
    } | Select-Object -First 1
    if ($null -eq $asset -or [string]::IsNullOrWhiteSpace($asset.browser_download_url)) {
        throw "Latest GitHub Release has no portable Windows package"
    }
    $checksumAsset = @($release.assets) | Where-Object {
        $_.name -eq ($asset.name + ".sha256.txt")
    } | Select-Object -First 1
    if ($null -eq $checksumAsset -or [string]::IsNullOrWhiteSpace($checksumAsset.browser_download_url)) {
        throw "Latest GitHub Release has no SHA-256 checksum for $($asset.name)"
    }

    Write-UpdateResult -Status "downloading" -Message "Downloading v$targetVersion" -CurrentVersion $currentVersion -TargetVersion $targetVersion
    Invoke-Download -Uri $asset.browser_download_url -OutFile $archivePath
    $checksumPath = Join-Path $tempRoot "latest.zip.sha256.txt"
    Invoke-Download -Uri $checksumAsset.browser_download_url -OutFile $checksumPath
    $expectedHash = [regex]::Match((Get-Content -Raw -LiteralPath $checksumPath), '(?i)\b[0-9a-f]{64}\b').Value.ToLowerInvariant()
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($expectedHash) -or $actualHash -ne $expectedHash) {
        throw "Downloaded package SHA-256 verification failed"
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
    $source = Get-ChildItem -LiteralPath $extractRoot -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName "reg-factory.exe")
    } | Select-Object -First 1
    if ($null -eq $source) {
        throw "Downloaded package layout is invalid"
    }
    $packageVersion = Get-PackageVersion $source.FullName
    if ($packageVersion -ne $targetVersion) {
        throw "Downloaded package version '$packageVersion' does not match Release '$targetVersion'"
    }

    Write-UpdateResult -Status "installing" -Message "Installing v$targetVersion" -CurrentVersion $currentVersion -TargetVersion $targetVersion
    if ($ProcessId -gt 0) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 30; $i++) {
            if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
        }
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
            throw "The current reg-factory process did not stop"
        }
        $stoppedOld = $true
    }

    Move-Item -LiteralPath $InstallDir -Destination $backupDir
    $movedOld = $true
    Move-Item -LiteralPath $source.FullName -Destination $InstallDir
    $movedNew = $true
    $newProcess = Start-Process -FilePath (Join-Path $InstallDir "reg-factory.exe") -ArgumentList @("--host", $ListenHost, "--port", $ListenPort) -WorkingDirectory $InstallDir -PassThru
    $statusUrl = "http://127.0.0.1:$ListenPort/api/status"
    $healthy = $false
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        try {
            $status = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 5
            if ([string]$status.version -eq $targetVersion) {
                $healthy = $true
                break
            }
        } catch {}
        if ($newProcess.HasExited) { break }
    }
    if (-not $healthy) {
        throw "Updated WebUI did not report version $targetVersion at $statusUrl"
    }
    Remove-Item -LiteralPath $backupDir -Recurse -Force
    $movedOld = $false
    $message = "Updated: v$currentVersion -> v$targetVersion"
    Write-UpdateResult -Status "completed" -Message $message -CurrentVersion $currentVersion -TargetVersion $targetVersion
    Write-Output $message
} catch {
    $failure = $_.Exception.Message
    if ($null -ne $newProcess -and -not $newProcess.HasExited) {
        Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($movedNew -and (Test-Path -LiteralPath $InstallDir)) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($movedOld -and (Test-Path -LiteralPath $backupDir) -and -not (Test-Path -LiteralPath $InstallDir)) {
        Move-Item -LiteralPath $backupDir -Destination $InstallDir -Force
    }
    if ($stoppedOld -and (Test-Path -LiteralPath (Join-Path $InstallDir "reg-factory.exe"))) {
        Start-Process -FilePath (Join-Path $InstallDir "reg-factory.exe") -ArgumentList @("--host", $ListenHost, "--port", $ListenPort) -WorkingDirectory $InstallDir
    }
    Write-UpdateResult -Status "failed" -Message $failure -CurrentVersion $currentVersion -TargetVersion $targetVersion
    throw $failure
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
