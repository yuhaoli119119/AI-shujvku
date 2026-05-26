$ErrorActionPreference = "Stop"

Write-Host "Checking if npm is available in PATH..."
$npmPath = Get-Command "npm" -ErrorAction SilentlyContinue
if ($npmPath) {
    Write-Host "Warning: npm is available at $($npmPath.Source)."
    Write-Host "Please use standard command: 'npm test -- --project=chromium' instead of this fallback."
    exit 0
}

Write-Host "npm not found in PATH. Proceeding with bundled Node + Playwright fallback..."

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$frontendDir = Join-Path (Split-Path $baseDir -Parent) "frontend"

$nodePath = "C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
$playwrightCli = "C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright\cli.js"

if (-not (Test-Path $nodePath) -or -not (Test-Path $playwrightCli)) {
    Write-Error "Bundled Node or Playwright CLI not found at the expected paths."
}

# Create a temporary shim directory
$shimDir = Join-Path $env:TEMP "d3-test-shim-$(Get-Random)"
$playwrightShim = Join-Path $shimDir "@playwright"
$playwrightTestShim = Join-Path $playwrightShim "test"
New-Item -ItemType Directory -Force -Path $playwrightTestShim | Out-Null

# Write the JS module redirection
"module.exports = require('playwright/test');" | Out-File -Encoding UTF8 -FilePath (Join-Path $playwrightTestShim "index.js")
"{`"name`":`"@playwright/test`",`"main`":`"index.js`"}" | Out-File -Encoding UTF8 -FilePath (Join-Path $playwrightTestShim "package.json")

# Write fake npm.cmd to bypass the Playwright webServer failing when it tries to start npm
# We start the python server via py.
"@echo off`r`npy -m http.server 8000" | Out-File -Encoding ASCII -FilePath (Join-Path $shimDir "npm.cmd")

$env:PATH = "$shimDir;$env:PATH"
$env:NODE_PATH = "$shimDir;C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\.pnpm\node_modules;C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"

Write-Host "Temporary shim created at $shimDir"
Write-Host "Running Playwright CLI from $frontendDir..."

Set-Location $frontendDir
& $nodePath $playwrightCli test --project=chromium

$exitCode = $LASTEXITCODE

Write-Host "Tests completed. Cleaning up shim..."
Remove-Item -Recurse -Force $shimDir

exit $exitCode
