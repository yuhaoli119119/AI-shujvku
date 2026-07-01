$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$scratchDir = Join-Path $workspaceRoot "literature-ai\backend\scratch"
$outputsTmpDirs = @(
    (Join-Path $workspaceRoot "literature-ai\outputs\tmp"),
    (Join-Path $workspaceRoot "outputs\tmp")
)
$pdfRegressionRunDirs = @(
    (Join-Path $workspaceRoot "local\test-runs\pdf-regression"),
    (Join-Path $workspaceRoot "test-artifacts\pdf-regression")
)
$cacheDirs = @(
    (Join-Path $workspaceRoot "literature-ai\.pytest_cache"),
    (Join-Path $workspaceRoot "literature-ai\backend\.pytest_cache"),
    (Join-Path $workspaceRoot "literature-ai\frontend\test-results")
)
$localSnapshotPaths = @(
    (Join-Path $workspaceRoot "literature-ai\.codex-artifacts"),
    (Join-Path $workspaceRoot "literature-ai\ctx.json"),
    (Join-Path $workspaceRoot "literature-ai\paper.json"),
    (Join-Path $workspaceRoot "local\test-runs\review_center_live.png"),
    (Join-Path $workspaceRoot "local\test-runs\tmp-figure-layout-check"),
    (Join-Path $workspaceRoot "test-artifacts\review_center_live.png"),
    (Join-Path $workspaceRoot "test-artifacts\tmp-figure-layout-check")
)
$pycacheRoots = @(
    (Join-Path $workspaceRoot "literature-ai\backend\app"),
    (Join-Path $workspaceRoot "literature-ai\backend\findpapers"),
    (Join-Path $workspaceRoot "literature-ai\backend\scripts"),
    (Join-Path $workspaceRoot "literature-ai\backend\tests"),
    (Join-Path $workspaceRoot "literature-ai\backend\tools"),
    (Join-Path $workspaceRoot "literature-ai\scripts")
)

$removed = New-Object System.Collections.Generic.List[string]

function Remove-IfExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    if (Test-Path -LiteralPath $LiteralPath) {
        Remove-Item -LiteralPath $LiteralPath -Force -Recurse
        $removed.Add($LiteralPath) | Out-Null
    }
}

function Remove-RootTempFiles {
    $patterns = @(
        "_*.png",
        "_*.jpg",
        "_*.jpeg",
        "_*.webp",
        "_*.gif",
        "_*.bmp",
        "_*.svg",
        "tmp_*",
        "tmp*.json",
        "tmp*.js",
        "page*_analysis.txt",
        "page*_blocks.txt",
        "page*_blocks.json",
        "page*.json",
        "paper_detail.json",
        "figs_analysis.json",
        "reasonix.toml",
        "a????_*.json",
        "b????_*.json",
        "C*temp_page*_analysis.txt"
    )

    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $workspaceRoot -File -Filter $pattern -ErrorAction SilentlyContinue |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Force
                $removed.Add($_.FullName) | Out-Null
            }
    }
}

function Remove-ScratchGeneratedFiles {
    if (-not (Test-Path -LiteralPath $scratchDir)) {
        return
    }

    Get-ChildItem -LiteralPath $scratchDir -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notin ".gitkeep", "pytest-tmp" } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force -Recurse
            $removed.Add($_.FullName) | Out-Null
        }
}

function Remove-LocalCacheDirectories {
    foreach ($dir in $cacheDirs) {
        Remove-IfExists -LiteralPath $dir
    }
}

function Remove-PythonCaches {
    foreach ($root in $pycacheRoots) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }

        Get-ChildItem -LiteralPath $root -Directory -Filter "__pycache__" -Recurse -ErrorAction SilentlyContinue |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Force -Recurse
                $removed.Add($_.FullName) | Out-Null
            }
    }
}

function Remove-LocalSnapshots {
    foreach ($path in $localSnapshotPaths) {
        Remove-IfExists -LiteralPath $path
    }
}

function Remove-TestRegressionOutputs {
    foreach ($dir in $pdfRegressionRunDirs) {
        if (-not (Test-Path -LiteralPath $dir)) {
            continue
        }

        Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.PSIsContainer -and $_.Name -match '^(existing(_fixed2?)?_\d+|new_real_\d+|rerun_[a-z_]+_\d+)$'
            } |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Force -Recurse
                $removed.Add($_.FullName) | Out-Null
            }
    }
}

Remove-RootTempFiles
Remove-ScratchGeneratedFiles
Remove-LocalCacheDirectories
Remove-PythonCaches
Remove-LocalSnapshots
Remove-TestRegressionOutputs

foreach ($outputsTmpDir in $outputsTmpDirs) {
    if (-not (Test-Path -LiteralPath $outputsTmpDir)) {
        continue
    }

    Get-ChildItem -LiteralPath $outputsTmpDir -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne ".gitkeep" } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force -Recurse
            $removed.Add($_.FullName) | Out-Null
        }
}

[PSCustomObject]@{
    RemovedCount = $removed.Count
    RemovedPaths = $removed
} | ConvertTo-Json -Depth 4
