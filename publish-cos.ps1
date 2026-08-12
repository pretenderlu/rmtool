[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command python -ErrorAction Stop
$downloadDir = Join-Path ([IO.Path]::GetTempPath()) ("rmtool-cos-" + [guid]::NewGuid())

try {
    New-Item -ItemType Directory -Path $downloadDir | Out-Null
    & $python.Source (Join-Path $repoRoot "tools\publish_resources.py") publish `
        --download-dir $downloadDir `
        --env-file (Join-Path $repoRoot ".env")
    if ($LASTEXITCODE -ne 0) {
        throw "Resource publishing failed with exit code $LASTEXITCODE."
    }
}
finally {
    if (Test-Path -LiteralPath $downloadDir) {
        Remove-Item -LiteralPath $downloadDir -Recurse -Force
    }
}
