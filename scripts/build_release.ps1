param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,
    [switch]$SkipTests,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$DistRoot = Join-Path $RepoRoot "dist"
$BuildRoot = Join-Path $RepoRoot "build"
$PackageName = "reg-factory-windows-x64-$Version"
$PyInstallerOutput = Join-Path $DistRoot "reg-factory"
$PackageRoot = Join-Path $DistRoot $PackageName
$ZipPath = Join-Path $DistRoot "$PackageName.zip"
$ChecksumPath = "$ZipPath.sha256.txt"

Set-Location $RepoRoot

$DeclaredVersion = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
if ($DeclaredVersion -ne $Version) {
    throw "VERSION is $DeclaredVersion, but the requested release is $Version."
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment not found. Run install.bat first."
}

if (-not $SkipInstall) {
    & $Python -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }
}

if (-not $SkipTests) {
    & $Python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }
    & node --check webui/static/app.js
    if ($LASTEXITCODE -ne 0) { throw "WebUI JavaScript check failed." }
}

foreach ($target in @($BuildRoot, $PyInstallerOutput, $PackageRoot, $ZipPath, $ChecksumPath)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

& $Python -m PyInstaller --noconfirm --clean --distpath $DistRoot --workpath $BuildRoot packaging/reg-factory.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

Move-Item -LiteralPath $PyInstallerOutput -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $RepoRoot "README.md") -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $RepoRoot "CHANGELOG.md") -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $RepoRoot ".env.example") -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $RepoRoot "VERSION") -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $RepoRoot "docs") -Destination $PackageRoot -Recurse

Get-ChildItem -LiteralPath $PackageRoot -Recurse -Directory -Filter "__pycache__" |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force

$PackagePrefix = $PackageRoot.TrimEnd('\') + '\'
$forbidden = Get-ChildItem -LiteralPath $PackageRoot -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($PackagePrefix.Length).Replace('\', '/')
    $_.Name -eq ".env" -or
    $_.Name -eq "emails.txt" -or
    $_.Name -match '^emails_(used|error)' -or
    $_.Name -in @("registration_queue.json", "fingerprint_profiles.json") -or
    $_.Extension -eq ".log" -or
    $relative -match '^(_internal/)?(cookies|tokens|runtime|outlook_accounts|unlock_results|codex_k12)/'
}
if ($forbidden) {
    $names = ($forbidden | Select-Object -ExpandProperty FullName) -join [Environment]::NewLine
    throw "Sensitive or runtime files entered the package:$([Environment]::NewLine)$names"
}

Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $ChecksumPath -Value "$hash  $PackageName.zip" -Encoding ascii

Get-Item -LiteralPath $ZipPath, $ChecksumPath | Select-Object FullName, Length, LastWriteTime
