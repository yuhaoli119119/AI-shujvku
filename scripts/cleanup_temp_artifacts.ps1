$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$scratchDir = Join-Path $workspaceRoot "literature-ai\backend\scratch"
$outputsTmpDir = Join-Path $workspaceRoot "outputs\tmp"

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

    Get-ChildItem -LiteralPath $scratchDir -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in ".json", ".txt" } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force
            $removed.Add($_.FullName) | Out-Null
        }
}

Remove-RootTempFiles
Remove-ScratchGeneratedFiles

if (Test-Path -LiteralPath $outputsTmpDir) {
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
