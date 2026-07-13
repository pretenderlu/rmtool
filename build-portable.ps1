$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$required = @(
    "rmtool.py",
    "requirements.txt",
    "assets\rmtool-icon.ico",
    "web\dashboard.html",
    "translations\reMarkable_zh_CN.qm",
    "rmrl\__init__.py"
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $path))) {
        throw "Required build input is missing: $path"
    }
}

$bootstrapPython = (Get-Command python -ErrorAction Stop).Source
$pythonBits = (& $bootstrapPython -c "import struct; print(struct.calcsize('P') * 8)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $pythonBits -ne "64") {
    throw "The portable package must be built with 64-bit Python; found $pythonBits-bit Python at $bootstrapPython."
}

$portableDir = Join-Path $root "dist\rmtool"
$zipPath = Join-Path $root "dist\rmtool-windows-x64.zip"
foreach ($path in @($portableDir, $zipPath)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

$venvDir = Join-Path $root "build\.venv"
& $bootstrapPython -m venv --clear $venvDir
if ($LASTEXITCODE -ne 0) {
    throw "Could not create the isolated build environment."
}

$python = Join-Path $venvDir "Scripts\python.exe"
& $python -m pip --isolated install --disable-pip-version-check --no-input `
    --requirement (Join-Path $root "requirements.txt") "PyInstaller==6.21.0"
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the pinned build dependencies."
}
& $python -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "The isolated build environment has incompatible dependencies."
}

$pyInstallerVersion = (& $python -m PyInstaller --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not available for $python."
}
if ($pyInstallerVersion -ne "6.21.0") {
    throw "PyInstaller 6.21.0 is required; found $pyInstallerVersion."
}

$arguments = @(
    "--clean",
    "--noconfirm",
    "--onedir",
    "--windowed",
    "--contents-directory", "_internal",
    "--name", "rmtool",
    "--icon", (Join-Path $root "assets\rmtool-icon.ico"),
    "--add-data", "$(Join-Path $root 'web');web",
    "--add-data", "$(Join-Path $root 'translations\reMarkable_zh_CN.qm');translations",
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build"),
    "--specpath", (Join-Path $root "build"),
    (Join-Path $root "rmtool.py")
)

& $python -m PyInstaller @arguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$executable = Join-Path $portableDir "rmtool.exe"
$internalDir = Join-Path $portableDir "_internal"
if (-not (Test-Path -LiteralPath $executable) -or -not (Test-Path -LiteralPath $internalDir)) {
    throw "Build completed without the expected rmtool.exe and _internal directory."
}

Compress-Archive -LiteralPath $portableDir -DestinationPath $zipPath -CompressionLevel Optimal

Write-Host "Portable folder: $portableDir"
Write-Host "Portable ZIP:    $zipPath"
