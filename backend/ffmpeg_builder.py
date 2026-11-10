import os

class CompresorInteligenteConfig:
    @staticmethod
    def obtener_parametros():
        return [
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "20",
            "-x264-params", "aq-mode=3:aq-strength=0.7",
            "-maxrate", "12M",
            "-bufsize", "24M",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart"
        ]

class FFmpegBuilder:
    FILTRO_DIMENSIONES_PARES = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    PRESETS_MOTION_BLUR = {
        "Sutil (Ligero)": "tmix=frames=3:weights='1 2 1'",
        "Cinemático (Estándar)": "tmix=frames=5:weights='1 2 3 2 1'",
        "Intenso (Acción)": "tmix=frames=7:weights='1 2 3 4 3 2 1'"
    }

    def __init__(self, ruta_ffmpeg, archivo_entrada, archivo_salida):
        self.ruta_ffmpeg = ruta_ffmpeg
        self.archivo_entrada = archivo_entrada
        self.archivo_salida = archivo_salida

        self.compresor_inteligente_activo = False
        self.filtro_audio = None

        self.filtros_activos = {
            "anime": None,
            "ruido": None,
            "nitidez": None,
            "interpolacion": None,
            "motion_blur": None,
        }

    def activar_compresor_inteligente(self):
        self.compresor_inteligente_activo = True
        return self

    def aplicar_optimizacion_anime(self):
        self.filtros_activos["anime"] = True
        return self

    def aplicar_reduccion_ruido(self):
        self.filtros_activos["ruido"] = "hqdn3d=1.5:1.5:6:6"
        return self

    def aplicar_nitidez(self):
        self.filtros_activos["nitidez"] = "unsharp=5:5:0.8"
        return self

    def aplicar_interpolacion_fps(self, fps=60):
        try:
            fps_val = int(fps)
            if fps_val <= 0:
                fps_val = 60
        except (ValueError, TypeError):
            fps_val = 60
        self.filtros_activos["interpolacion"] = f"minterpolate=fps={fps_val}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir"
        return self

    def aplicar_interpolacion_60fps(self):
        return self.aplicar_interpolacion_fps(60)

    def aplicar_motion_blur(self, preset="Cinemático (Estándar)"):
        filtro = self.PRESETS_MOTION_BLUR.get(preset, "tmix=frames=5:weights='1 2 3 2 1'")
        self.filtros_activos["motion_blur"] = filtro
        return self

    def aplicar_normalizacion_audio(self, stats: dict):
        """Aplica el Paso 2 de loudnorm con datos ultra precisos del Paso 1."""
        if not stats: return self
        self.filtro_audio = (
            f"loudnorm=I=-14:LRA=11:TP=-1.0:"
            f"measured_I={stats['measured_I']}:"
            f"measured_LRA={stats['measured_LRA']}:"
            f"measured_TP={stats['measured_TP']}:"
            f"measured_thresh={stats['measured_thresh']}:"
            f"offset={stats['offset']}:linear=true"
        )
        return self

    def construir_comando(self):
        comando = [
            self.ruta_ffmpeg,
            "-y",
            "-i", self.archivo_entrada
        ]

        # Pipeline de Video (Orden Justificado: Limpieza -> Interpolación -> Motion Blur -> Nitidez -> Escala)
        cadena_filtros = []
        orden_pipeline = ["ruido", "interpolacion", "motion_blur", "nitidez"]

        for clave in orden_pipeline:
            if self.filtros_activos.get(clave):
                cadena_filtros.append(self.filtros_activos[clave])

        cadena_filtros.append(self.FILTRO_DIMENSIONES_PARES)
        comando.extend(["-vf", ",".join(cadena_filtros)])
        
        # Pipeline de Audio
        if self.filtro_audio:
            comando.extend(["-af", self.filtro_audio])

        if self.compresor_inteligente_activo:
            comando.extend(CompresorInteligenteConfig.obtener_parametros())
        else:
            comando.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"])

        if self.filtros_activos.get("anime"):
            comando.extend(["-tune", "animation"])

        comando.append(self.archivo_salida)

        return comando
