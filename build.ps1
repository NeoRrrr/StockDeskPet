param(
    [switch]$Package,
    [string]$Version = ""
)

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
        --onedir `
        --windowed `
        --name StockDeskPet `
        --icon 'assets\app_icon.ico' `
        --add-data 'assets;assets' `
        --collect-data futu `
        app.py
}
finally {
    Pop-Location
}

Write-Host "Build complete: $ProjectDir\dist\StockDeskPet\StockDeskPet.exe"

if ($Package) {
    if (-not $Version) {
        $Version = & $Python -c "from stock_pet import __version__; print(__version__)"
    }
    if (-not (Get-Command vpk -ErrorAction SilentlyContinue)) {
        dotnet tool install -g vpk --version 1.2.0
    }
    & vpk pack `
        --packId NeoRrrr.StockDeskPet `
        --packVersion $Version `
        --packDir (Join-Path $ProjectDir 'dist\StockDeskPet') `
        --mainExe StockDeskPet.exe `
        --packTitle '股票桌宠' `
        --packAuthors NeoRrrr `
        --outputDir (Join-Path $ProjectDir 'Releases')
    Write-Host "Velopack package complete: $ProjectDir\Releases"
}
