$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv (Join-Path $ProjectDir '.venv')
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $ProjectDir 'requirements-build.txt')

Push-Location $ProjectDir
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name StockDeskPet `
        --add-data 'assets;assets' `
        app.py
}
finally {
    Pop-Location
}

Write-Host "Build complete: $ProjectDir\dist\StockDeskPet.exe"

