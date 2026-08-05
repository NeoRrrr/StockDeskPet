$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv (Join-Path $ProjectDir '.venv')
    & $Python -m pip install --upgrade pip
}

& $Python -c "import PySide6, velopack" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install -r (Join-Path $ProjectDir 'requirements.txt')
}

& $Python (Join-Path $ProjectDir 'app.py')
