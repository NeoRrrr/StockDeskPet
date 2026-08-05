$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv (Join-Path $ProjectDir '.venv')
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r (Join-Path $ProjectDir 'requirements.txt')
}

& $Python (Join-Path $ProjectDir 'app.py')

