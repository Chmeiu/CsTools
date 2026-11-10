# CsTools

Aplicación de escritorio para Windows con interfaz HTML y procesamiento local mediante Python.

## Requisitos

- Windows 10/11 con Microsoft Edge WebView2 Runtime.
- Python 3.10 o posterior para ejecución desde el código fuente.
- FFmpeg, FFprobe, yt-dlp y Node están incluidos en `core\` y la raíz del proyecto.

## Preparar el entorno

Desde PowerShell, en la carpeta `CsTools`:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

El script crea `.venv` e instala las dependencias declaradas en `requirements.txt`.

## Ejecutar

```powershell
.\.venv\Scripts\python.exe app.py
```

La app conserva su configuración en `%LOCALAPPDATA%\CsTools\config.json`; los logs están en `%LOCALAPPDATA%\CsTools\logs\app.log`.

## Funciones

- Interfaz oscura técnica con un ligero aumento de escala e iconografía SVG propia.
- Análisis automático de enlaces con vista inmediata de carga, detección de playlists y collage de sus primeras cuatro portadas.
- Las playlists se analizan de forma ligera: solo se consultan sus primeros cuatro elementos, sin cargar los metadatos de toda la lista.
- Primer intento con el método clásico de yt-dlp y reintento automático con Node.js y el solucionador EJS si falla.
- Extracción de audio MP3, incluidas listas de reproducción.
- Transcodificación H.264/AAC, compresión, perfil de animación, reducción de ruido, nitidez, interpolación FPS, motion blur y normalización de audio.
- Optimización de video con arrastre de archivos, vista previa en el panel y opción para quitar el archivo seleccionado.
- Comparación local de video lado a lado con reproducción sincronizada, búsqueda, miniatura de salida difuminada y modo de pantalla completa sin recargar los videos.
- Las previsualizaciones de video se transmiten desde un servidor local temporal, limitado a los archivos seleccionados y compatible con solicitudes por rangos.
- Actualizador manual conectado a GitHub Releases público: consulta la versión, muestra notas, descarga el instalador y valida SHA-256 antes de ejecutarlo. El repositorio se configura en `update_config.json`.

Los controles operan sobre archivos locales o URLs que el usuario introduce. CsTools no descifra DRM.

## Generar la distribución

```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

Genera `dist\CsTools`. Si Inno Setup 6 está instalado en su ruta predeterminada, también genera `release\installer\Instalar_CsTools.exe`.
