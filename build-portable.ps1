$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$required = @(
    "rmtool.py",
    "requirements.txt",
    "requirements-build-windows.lock",
    "assets\rmtool-icon.ico",
    "assets\fonts\NotoSansCJKsc-Regular.otf",
    "assets\fonts\NotoSansCJKsc-Common.otf",
    "assets\fonts\LICENSE",
    "assets\device_frames\paper-pro.png",
    "assets\device_frames\paper-pro-move.png",
    "assets\device_frames\paper-pure.png",
    "assets\device_frames\remarkable-1.png",
    "assets\device_frames\remarkable-2.png",
    "translations\manifest.json",
    "tap-page-turn\manifest.json",
    "fast-mono-reading\manifest.json",
    "native-chinese\manifest.json",
    "pinyin-input\manifest.json",
    "reading-enhancements\manifest.json",
    "note-enhancements\manifest.json",
    "weread-app\manifest.json",
    "weread-launcher\manifest.json",
    "qr-upload\manifest.json",
    "native-import\manifest.json",
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
$oneFilePath = Join-Path $root "dist\rmtool-windows-x64-onefile.exe"
foreach ($path in @($portableDir, $zipPath, $oneFilePath)) {
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
# Hash-locked: identical sources produce identical dependency sets per release.
& $python -m pip --isolated install --disable-pip-version-check --no-input --require-hashes `
    --requirement (Join-Path $root "requirements-build-windows.lock")
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

& $python (Join-Path $root "tools\validate_build_matrix.py")
if ($LASTEXITCODE -ne 0) {
    throw "The device/version build matrix is incomplete or inconsistent."
}

$commonArguments = @(
    "--clean",
    "--noconfirm",
    "--windowed",
    "--icon", (Join-Path $root "assets\rmtool-icon.ico"),
    "--collect-data", "certifi",
    "--distpath", (Join-Path $root "dist"),
    "--specpath", (Join-Path $root "build")
)
$entryPoint = Join-Path $root "rmtool.py"

$portableArguments = $commonArguments + @(
    "--onedir",
    "--contents-directory", "_internal",
    "--name", "rmtool",
    "--workpath", (Join-Path $root "build\onedir"),
    $entryPoint
)

& $python (Join-Path $root "tools\build_app.py") @portableArguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build the portable folder with exit code $LASTEXITCODE."
}

$executable = Join-Path $portableDir "rmtool.exe"
$internalDir = Join-Path $portableDir "_internal"
$caBundle = Join-Path $internalDir "certifi\cacert.pem"
if (-not (Test-Path -LiteralPath $executable) -or
    -not (Test-Path -LiteralPath $internalDir) -or
    -not (Test-Path -LiteralPath $caBundle -PathType Leaf)) {
    throw "Build completed without the expected rmtool.exe, _internal directory, and certifi CA bundle."
}

function Invoke-HttpsSmokeTest([string]$Path) {
    # Offline resource loading is mandatory; only network probes may be skipped.
    $checks = @("--bundle-self-test")
    if ($env:RMTOOL_SKIP_SMOKE_TEST -ne "1") { $checks += "--https-smoke-test" }
    foreach ($check in $checks) {
    # The executables use the GUI subsystem, so wait explicitly for the exit code.
    $process = Start-Process -FilePath $Path -ArgumentList $check -PassThru -WindowStyle Hidden
    # Reading Handle keeps it open; otherwise ExitCode can be empty after exit.
    $null = $process.Handle
    if (-not $process.WaitForExit(180000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "HTTPS smoke test timed out for $Path."
    }
    if ($process.ExitCode -ne 0) {
        throw "HTTPS smoke test failed for $Path with exit code $($process.ExitCode)."
    }
    Write-Host "HTTPS smoke test passed: $Path"
    }
}

Invoke-HttpsSmokeTest $executable

Compress-Archive -LiteralPath $portableDir -DestinationPath $zipPath -CompressionLevel Optimal

$oneFileArguments = $commonArguments + @(
    "--onefile",
    "--name", "rmtool-windows-x64-onefile",
    "--workpath", (Join-Path $root "build\onefile"),
    $entryPoint
)

& $python (Join-Path $root "tools\build_app.py") @oneFileArguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build the single-file executable with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $oneFilePath)) {
    throw "Build completed without the expected single-file executable."
}
Invoke-HttpsSmokeTest $oneFilePath

Write-Host "Portable folder: $portableDir"
Write-Host "Portable ZIP:    $zipPath"
Write-Host "Single-file EXE: $oneFilePath"
