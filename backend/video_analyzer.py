import subprocess
import os
import json
import re

class VideoAnalyzer:
    """
    Motor de análisis instantáneo usando FFprobe y FFmpeg.
    """
    def __init__(self, ffprobe_path="core\\ffprobe.exe", ffmpeg_path="core\\ffmpeg.exe"):
        self.ffprobe_path = os.path.abspath(ffprobe_path)
        self.ffmpeg_path = os.path.abspath(ffmpeg_path)

    def obtener_duracion(self, ruta_video):
        """Devuelve la duración del video en segundos (float)."""
        try:
            comando = [
                self.ffprobe_path, 
                "-v", "error", 
                "-show_entries", "format=duration", 
                "-of", "default=noprint_wrappers=1:nokey=1", 
                ruta_video
            ]
            resultado = subprocess.run(
                comando, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                text=True, 
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            return float(resultado.stdout.strip())
        except Exception:
            return 0.0

    def analizar_audio_loudnorm(self, ruta_video):
        """
        Ejecuta el Paso 1 (Análisis) del filtro loudnorm.
        Devuelve un diccionario con los valores JSON necesarios para el Paso 2.
        """
        try:
            comando = [
                self.ffmpeg_path,
                "-hide_banner", "-i", ruta_video,
                "-vn", "-sn", "-dn", # Ignorar video, subtítulos y datos
                "-f", "null",        # No escribir archivo de salida
                "-af", "loudnorm=I=-14:LRA=11:TP=-1.0:print_format=json",
                "-"
            ]
            resultado = subprocess.run(
                comando, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                text=True, 
                encoding='utf-8', 
                errors='ignore', 
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            # Buscar el bloque JSON inyectado por loudnorm en stderr
            match = re.search(r'\{[\s\S]*"input_i"[\s\S]*\}', resultado.stderr)
            if match:
                datos = json.loads(match.group(0))
                return {
                    "measured_I": datos.get("input_i"),
                    "measured_LRA": datos.get("input_lra"),
                    "measured_TP": datos.get("input_tp"),
                    "measured_thresh": datos.get("input_thresh"),
                    "offset": datos.get("target_offset")
                }
            return None
        except Exception:
            return None