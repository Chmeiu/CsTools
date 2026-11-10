$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo crear el entorno virtual (código $LASTEXITCODE)."
    }
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "No se pudo actualizar pip (código $LASTEXITCODE)."
}

& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "No se pudieron instalar las dependencias (código $LASTEXITCODE)."
}

Write-Host "Entorno CsTools listo. Ejecuta: .\.venv\Scripts\python.exe app.py"
