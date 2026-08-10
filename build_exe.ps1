# build_exe.ps1 - Build InstantClip.exe with PyInstaller

Set-Location $PSScriptRoot

$toolsRoot = Split-Path $PSScriptRoot -Parent
$preferred = Join-Path $toolsRoot "tensoku_rep_movier\.venv\Scripts\python.exe"

if (Test-Path $preferred) {
    $python = $preferred
} else {
    $python = Get-ChildItem $toolsRoot -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*.venv*Scripts*" } |
        Where-Object {
            $null = & $_.FullName -m PyInstaller --version 2>$null
            $LASTEXITCODE -eq 0
        } |
        Select-Object -First 1 -ExpandProperty FullName
}

if (-not $python) {
    Write-Error "PyInstaller 付き python.exe が見つかりません"
    exit 1
}

Write-Host "Python: $python"
& $python build.py
