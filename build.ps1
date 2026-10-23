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
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed' }
& $Python -m pip install -r (Join-Path $ProjectDir 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed' }

# PyInstaller also searches PATH for DLLs. Tools such as Poppler ship an
# incompatible icuuc.dll with the same name as Windows' ICU used by Qt.
# Let Qt's hooks resolve its own libraries; keep unrelated tool DLLs out.
$BuildOriginalPath = $env:PATH
Push-Location $ProjectDir
try {
    $env:PATH = @(
        (Split-Path -Parent $Python),
        (Join-Path $env:SystemRoot 'System32'),
        $env:SystemRoot
    ) -join ';'
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
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed; no package was produced' }
}
finally {
    $env:PATH = $BuildOriginalPath
    Pop-Location
}

Write-Host "Build complete: $ProjectDir\dist\StockDeskPet\StockDeskPet.exe"

if ($Package) {
    if (-not $Version) {
        $Version = & $Python -c "from stock_pet import __version__; print(__version__)"
    }
    if (-not (Get-Command vpk -ErrorAction SilentlyContinue)) {
        dotnet tool install -g vpk --version 1.2.0
        if ($LASTEXITCODE -ne 0) { throw 'Velopack installation failed' }
    }
    & vpk pack `
        --packId NeoRrrr.StockDeskPet `
        --packVersion $Version `
        --packDir (Join-Path $ProjectDir 'dist\StockDeskPet') `
        --mainExe StockDeskPet.exe `
        --packTitle '股票桌宠' `
        --packAuthors NeoRrrr `
        --outputDir (Join-Path $ProjectDir 'Releases')
    if ($LASTEXITCODE -ne 0) { throw 'Velopack packaging failed' }
    Write-Host "Velopack package complete: $ProjectDir\Releases"
}
