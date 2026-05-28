# update_map.ps1  -  Sovereign Architecture Map Generator (Python/Widget Blueprint, v2026.05)
# Output: ARCHITECTURE_MAP.json
# Usage: .\update_map.ps1

$mapFile = "ARCHITECTURE_MAP.json"

# 1. Pinned (Cache Anchor) files -- prioritizing core system guardrails first
$pinned = @(
    "models.py",
    "CODING_STANDARDS.md",
    ".github/copilot-instructions.md"
)

# 2. Production directories to completely exclude from structural mapping
$exclude = @('.git', '.github', '__pycache__', '.pytest_cache', '.venv', 'node_modules', 'dist')

# 3. Target standard file extensions used across the chatbot infrastructure
$includeExtensions = @("*.py", "*.js", "*.html", "*.json", "*.md")

# 4. Ingest and filter file structures
$cwd = (Get-Location).Path
$allFiles = Get-ChildItem -Recurse -Include $includeExtensions | Where-Object {
    $fp  = $_.FullName
    $hit = $false
    foreach ($d in $exclude) { if ($fp -like "*\$d\*") { $hit = $true; break } }
    # Do not parse the architecture map itself
    if ($_.Name -eq $mapFile) { $hit = $true }
    -not $hit
}

# 5. Arrange files: Pinned anchors execute first, followed alphabetically by pathway
$sortedFiles = $allFiles | Sort-Object {
    if ($pinned -contains $_.Name) { return 0 }
    return 1
}, FullName

# 6. Extract architecture metadata for Python, JavaScript, and Configurations
$fileEntries = foreach ($file in $sortedFiles) {
    $rel     = $file.FullName.Replace("$cwd\", '').Replace('\', '/')
    $lines   = Get-Content $file.FullName -ErrorAction SilentlyContinue
    
    # Structural categories matching our pythonic stack
    $classes   = [System.Collections.Generic.List[string]]::new()
    $functions = [System.Collections.Generic.List[string]]::new()
    $imports   = [System.Collections.Generic.List[string]]::new()
    $routes    = [System.Collections.Generic.List[string]]::new()

    if ($file.Extension -eq '.py') {
        foreach ($line in $lines) {
            $t = $line.Trim()
            # Catch Pydantic or native Python Class blocks
            if ($t -match '^class\s+(\w+)(\(([^)]+)\))?:') {
                $classes.Add($t)
            }
            # Catch strictly typed function layouts
            elseif ($t -match '^def\s+(\w+)\s*\((.*)\)\s*(->\s*[^:]+)?\s*:') {
                # Flag if it's an API router pathway (FastAPI/Vercel standard)
                if ($t -match 'async\s+def' -or $t -match 'request' -or $rel -like "api/*") {
                    $routes.Add($t)
                } else {
                    $functions.Add($t)
                }
            }
            # Track structural imports to watch dependencies
            elseif ($t -match '^(import\s+\w+|from\s+\w+\s+import)') {
                $imports.Add($t)
            }
        }
    }
    elseif ($file.Extension -eq '.js') {
        foreach ($line in $lines) {
            $t = $line.Trim()
            # Capture ES6/Vanilla UI DOM functions or click events
            if ($t -match '^(const|let|var)?\s*(\w+)\s*=\s*(\(.*?\)|[^=]+)\s*=>' -or $t -match '^function\s+(\w+)') {
                $functions.Add($t)
            }
            elseif ($t -match 'fetch\(' -or $t -match '\.addEventListener\(') {
                $routes.Add($t)
            }
        }
    }

    [PSCustomObject]@{
        path       = $rel
        isPinned   = ($pinned -contains $file.Name)
        extension  = $file.Extension
        classes    = $classes.ToArray()
        functions  = $functions.ToArray()
        imports    = $imports.ToArray()
        routes     = $routes.ToArray()
    }
}

# 7. Compile map meta details
$map = [PSCustomObject]@{
    generated   = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK')
    tool        = 'update_map.ps1 v2026.05 (Python/Widget Edition)'
    pinnedFiles = $pinned
    excludeDirs = $exclude
    fileCount   = @($fileEntries).Count
    files       = $fileEntries
}

# 8. Serialize target out cleanly as explicit UTF8 JSON
$json = $map | ConvertTo-Json -Depth 10 -Compress:$false
[System.IO.File]::WriteAllText(
    (Join-Path $cwd $mapFile),
    $json,
    [System.Text.Encoding]::UTF8)

# 9. Log generation report
$sizekb     = [math]::Round((Get-Item $mapFile).Length / 1KB, 2)
$pinnedList = $pinned -join ", "
Write-Host "[SOVEREIGN REWRITE]: $mapFile generated successfully." -ForegroundColor Green
Write-Host "Tracked Files : $(@($fileEntries).Count)" -ForegroundColor Cyan
Write-Host "Map Size      : $sizekb KB" -ForegroundColor Cyan
Write-Host "[ANCHOR] AI Models will now lock onto: $pinnedList" -ForegroundColor Yellow