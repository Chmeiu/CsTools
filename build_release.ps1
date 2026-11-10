$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'No se encontró el entorno virtual. Ejecuta primero .\setup.ps1.'
}

& $python -m PyInstaller --noconfirm --clean --onedir --windowed --name CsTools --icon logo.ico `
    --add-data "index.html;." `
    --add-data "update_config.json;." `
    --add-data "core;core" `
    --add-data "node.exe;." `
    --collect-all webview `
    --collect-all yt_dlp `
    --collect-all yt_dlp_ejs `
    app.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller terminó con el código $LASTEXITCODE."
}

$version = (& $python -c "from version import APP_VERSION; print(APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') {
    throw 'No se pudo leer una versión válida desde version.py.'
}
@("#define AppVersion `"$version`"") | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'version.iss') -Encoding ASCII

$iscc = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) {
    Write-Host "Distribución creada en $PSScriptRoot\dist\CsTools."
    Write-Host 'Para generar el instalador, instala Inno Setup 6 y vuelve a ejecutar este script.'
    exit 0
}

& $iscc (Join-Path $PSScriptRoot 'CsTools_SetupInstall.iss')
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup terminó con el código $LASTEXITCODE."
}

Write-Host "Instalador creado en $PSScriptRoot\release\installer\Instalar_CsTools.exe"