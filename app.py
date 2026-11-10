from __future__ import annotations

import base64
import itertools
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlparse
from urllib.error import URLError

import webview
from webview.dom import DOMEventHandler

from backend.ffmpeg_builder import FFmpegBuilder
from backend.video_analyzer import VideoAnalyzer
from updater import GitHubReleaseUpdater, UpdateError, UpdateRelease
from version import APP_VERSION


APP_NAME = "CsTools"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
CONFIG_FILE = APP_DATA_DIR / "config.json"
LOG_DIR = APP_DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"
UPDATE_CONFIG_FILE = RESOURCE_DIR / "update_config.json"


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def _forward_dropped_files(window: webview.Window, event: dict[str, Any]) -> None:
    data_transfer = event.get("dataTransfer")
    if not isinstance(data_transfer, dict):
        return
    files = data_transfer.get("files")
    if not isinstance(files, list):
        return
    paths = [
        path
        for file in files
        if isinstance(file, dict)
        and isinstance((path := file.get("pywebviewFullPath")), str)
        and path
    ]
    if paths:
        window.evaluate_js(
            "window.dispatchEvent(new CustomEvent('cstools-file-drop', "
            f"{{ detail: {json.dumps(paths)} }}));"
        )


def _bind_file_drop(window: webview.Window) -> None:
    document = window.dom.document
    document.events.dragover += DOMEventHandler(lambda _event: None, prevent_default=True)
    document.events.drop += DOMEventHandler(
        lambda event: _forward_dropped_files(window, event),
        prevent_default=True,
    )


@dataclass
class Task:
    task_id: str
    kind: str
    status: str = "running"
    progress: float = 0.0
    message: str = "Preparando…"
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen[str] | None = None


class MediaPreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    media_paths: dict[str, Path]
    media_lock: threading.RLock


class MediaPreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._serve_media(include_body=True)

    def do_HEAD(self) -> None:
        self._serve_media(include_body=False)

    def _serve_media(self, *, include_body: bool) -> None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "media" or not parts[1]:
            self.send_error(404)
            return

        server = cast(MediaPreviewServer, self.server)
        with server.media_lock:
            path = server.media_paths.get(parts[1])
        if path is None or not path.is_file():
            self.send_error(404)
            return

        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return

        start = 0
        end = size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or size == 0:
                self._send_range_error(size)
                return
            range_start, range_end = match.groups()
            if range_start:
                start = int(range_start)
                end = min(int(range_end), size - 1) if range_end else size - 1
            elif range_end:
                suffix_length = int(range_end)
                if suffix_length == 0:
                    self._send_range_error(size)
                    return
                start = max(size - suffix_length, 0)
            if start >= size or end < start:
                self._send_range_error(size)
                return
            status = 206

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not include_body:
            return

        try:
            with path.open("rb") as media_file:
                media_file.seek(start)
                remaining = content_length
                while remaining:
                    chunk = media_file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            logging.debug("El reproductor cerró la conexión de vista previa de %s.", path.name)

    def _send_range_error(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logging.debug("Vista previa multimedia: " + format, *args)


class CsToolsApi:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._window: webview.Window | None = None
        self._media_server: MediaPreviewServer | None = None
        self._update_repository, self._update_config_error = self._load_update_repository()
        self._available_update: UpdateRelease | None = None
        self._config = self._load_config()
        self._analyzer = VideoAnalyzer(
            str(RESOURCE_DIR / "core" / "ffprobe.exe"),
            str(RESOURCE_DIR / "core" / "ffmpeg.exe"),
        )

    def set_window(self, window: webview.Window) -> None:
        self._window = window

    def get_config(self) -> dict[str, str]:
        return dict(self._config)

    def get_app_version(self) -> str:
        return APP_VERSION

    def start_update_check(self) -> str:
        return self._start_task("update-check", self._check_update_task)

    def start_update_install(self) -> str:
        with self._lock:
            release = self._available_update
        if release is None:
            raise UpdateError("Primero busca una actualización disponible.")
        return self._start_task(
            "update-install",
            lambda task: self._install_update_task(task, release),
        )

    def select_directory(self, kind: str) -> dict[str, str]:
        if kind not in {"video", "audio", "optimized"}:
            raise ValueError("Tipo de directorio no válido.")
        selected = self._choose_path(webview.FileDialog.FOLDER)
        if selected:
            self._config[f"dir_{kind}"] = str(Path(selected).resolve())
            self._save_config()
        return self.get_config()

    def select_input_file(self) -> str | None:
        selected = self._choose_path(
            webview.FileDialog.OPEN,
            file_types=("Archivos de video (*.mp4;*.mov;*.avi;*.mkv)",),
        )
        return str(Path(selected).resolve()) if selected else None

    @staticmethod
    def validate_input_file(input_path: str) -> str:
        source = Path(input_path).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() not in {
            ".mp4",
            ".mov",
            ".avi",
            ".mkv",
        }:
            raise ValueError("Selecciona un archivo MP4, MOV, AVI o MKV válido.")
        return str(source)

    def prepare_input_file(self, input_path: str) -> dict[str, str | None]:
        valid_path = self.validate_input_file(input_path)
        media_url: str | None = None
        preview_error: str | None = None
        try:
            media_url = self.create_media_preview(valid_path)
        except OSError as error:
            logging.warning("No se pudo iniciar la vista previa de %s: %s", valid_path, error)
            preview_error = str(error)

        ffmpeg = RESOURCE_DIR / "core" / "ffmpeg.exe"
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0.5",
            "-i",
            valid_path,
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2",
            "-q:v",
            "5",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=12,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logging.warning("No se pudo generar la vista previa de %s: %s", valid_path, error)
            return {
                "path": valid_path,
                "thumbnail": None,
                "preview_error": preview_error or str(error),
                "media_url": media_url,
            }

        if result.returncode != 0 or not result.stdout:
            details = result.stderr.decode("utf-8", errors="replace").strip()
            message = details[:300] or "FFmpeg no devolvió una imagen."
            logging.warning("No se pudo generar la vista previa de %s: %s", valid_path, message)
            return {
                "path": valid_path,
                "thumbnail": None,
                "preview_error": preview_error or message,
                "media_url": media_url,
            }

        thumbnail = base64.b64encode(result.stdout).decode("ascii")
        return {
            "path": valid_path,
            "thumbnail": f"data:image/jpeg;base64,{thumbnail}",
            "preview_error": preview_error,
            "media_url": media_url,
        }

    def create_media_preview(self, input_path: str) -> str:
        source = Path(self.validate_input_file(input_path))
        token = uuid.uuid4().hex
        with self._lock:
            if self._media_server is None:
                server = MediaPreviewServer(("127.0.0.1", 0), MediaPreviewHandler)
                server.media_paths = {}
                server.media_lock = self._lock
                self._media_server = server
                threading.Thread(
                    target=server.serve_forever,
                    name="CsToolsMediaPreview",
                    daemon=True,
                ).start()
            server = self._media_server
            server.media_paths[token] = source
            address, port = server.server_address[:2]
        return f"http://{address}:{port}/media/{token}"

    def stop_media_preview_server(self) -> None:
        with self._lock:
            server = self._media_server
            self._media_server = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def analyze_url(self, url: str, mode: str) -> str:
        clean_url = self._validate_url(url)
        if mode not in {"video", "audio"}:
            raise ValueError("Tipo de contenido no válido.")
        return self._start_task(
            f"metadata-{mode}",
            lambda task: self._analyze_task(task, clean_url, mode),
        )

    def start_download(self, url: str, mode: str, quality: int = 0) -> str:
        clean_url = self._validate_url(url)
        if mode not in {"video", "audio"}:
            raise ValueError("Tipo de contenido no válido.")
        quality = self._validate_quality(quality)
        return self._start_task(
            f"download-{mode}",
            lambda task: self._download_task(task, clean_url, mode, quality),
        )

    def start_optimization(self, input_path: str, settings: dict[str, Any]) -> str:
        source = Path(self.validate_input_file(input_path))
        normalized_settings = self._validate_settings(settings)
        return self._start_task(
            "optimization",
            lambda task: self._optimization_task(task, source, normalized_settings),
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError("La operación solicitada ya no existe.")
            return self._task_snapshot(task)

    def cancel_task(self, task_id: str) -> dict[str, str]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError("La operación solicitada ya no existe.")
            if task.status != "running":
                return {"status": task.status}
            task.cancel_event.set()
            task.message = "Cancelando…"
            process = task.process
        if process and process.poll() is None:
            self._terminate_process(process)
        return {"status": "cancelling"}

    def open_output_directory(self, kind: str) -> None:
        if kind not in {"video", "audio", "optimized"}:
            raise ValueError("Tipo de directorio no válido.")
        path = Path(self._config[f"dir_{kind}"])
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]

    def _load_config(self) -> dict[str, str]:
        defaults = {
            "dir_video": str(Path.home() / "Videos" / APP_NAME / "Descargas"),
            "dir_audio": str(Path.home() / "Videos" / APP_NAME / "Audio"),
            "dir_optimized": str(Path.home() / "Videos" / APP_NAME / "Optimizados"),
        }
        write_config = not CONFIG_FILE.exists()
        try:
            if CONFIG_FILE.exists():
                with CONFIG_FILE.open("r", encoding="utf-8") as file:
                    stored = json.load(file)
                for key in defaults:
                    value = stored.get(key)
                    if isinstance(value, str) and value.strip():
                        defaults[key] = value
                    else:
                        write_config = True
        except (OSError, json.JSONDecodeError, AttributeError) as error:
            logging.warning("No se pudo leer la configuración: %s", error)
            write_config = True
        for path in defaults.values():
            Path(path).mkdir(parents=True, exist_ok=True)
        if write_config:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = CONFIG_FILE.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(defaults, file, ensure_ascii=False, indent=2)
            temporary.replace(CONFIG_FILE)
        return defaults

    @staticmethod
    def _load_update_repository() -> tuple[str, str | None]:
        try:
            with UPDATE_CONFIG_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return "", None
        except (OSError, json.JSONDecodeError) as error:
            return "", f"No se pudo leer la configuración del actualizador: {error}"
        if not isinstance(data, dict) or not isinstance(data.get("repository", ""), str):
            return "", "La configuración del actualizador debe contener un repositorio válido."
        return data.get("repository", "").strip(), None

    def _save_config(self) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_FILE.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(self._config, file, ensure_ascii=False, indent=2)
        temporary.replace(CONFIG_FILE)

    def _choose_path(
        self, dialog_type: int, file_types: tuple[str, ...] = ()
    ) -> str | None:
        if self._window is None:
            raise RuntimeError("La ventana de CsTools aún no está lista.")
        paths = self._window.create_file_dialog(
            dialog_type,
            allow_multiple=False,
            file_types=file_types,
        )
        return str(paths[0]) if paths else None

    @staticmethod
    def _validate_url(url: str) -> str:
        value = url.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Introduce una URL válida que comience por http:// o https://.")
        return value

    @staticmethod
    def _validate_quality(quality: int) -> int:
        if isinstance(quality, bool) or not isinstance(quality, int) or not 0 <= quality <= 16384:
            raise ValueError("La resolución seleccionada no es válida.")
        return quality

    @staticmethod
    def _validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("La configuración de procesamiento no es válida.")
        fps = settings.get("fps", "60")
        if str(fps) not in {"48", "60", "90", "120", "144", "240"}:
            raise ValueError("La tasa de cuadros seleccionada no es válida.")
        preset = settings.get("motion_blur_preset", "Cinemático (Estándar)")
        if preset not in FFmpegBuilder.PRESETS_MOTION_BLUR:
            raise ValueError("El perfil de desenfoque seleccionado no es válido.")
        names = ("compression", "anime", "denoise", "sharpen", "interpolation", "motion_blur", "normalize_audio")
        if any(not isinstance(settings.get(name, False), bool) for name in names):
            raise ValueError("Una de las opciones de procesamiento no es válida.")
        return {
            **{name: settings.get(name, False) for name in names},
            "fps": str(fps),
            "motion_blur_preset": preset,
        }

    def _start_task(self, kind: str, work: Callable[[Task], dict[str, Any]]) -> str:
        task = Task(task_id=uuid.uuid4().hex, kind=kind)
        with self._lock:
            self._tasks[task.task_id] = task
        threading.Thread(
            target=self._run_task,
            args=(task, work),
            name=f"cstools-{kind}",
            daemon=True,
        ).start()
        return task.task_id

    def _run_task(
        self, task: Task, work: Callable[[Task], dict[str, Any]]
    ) -> None:
        try:
            result = work(task)
            with self._lock:
                if task.cancel_event.is_set():
                    task.status = "cancelled"
                    task.message = "Operación cancelada."
                else:
                    task.status = "completed"
                    task.progress = 1.0
                    task.message = "Operación completada."
                    task.result = result
        except Exception as error:
            logging.exception("Falló la operación %s (%s)", task.kind, task.task_id)
            with self._lock:
                task.status = "cancelled" if task.cancel_event.is_set() else "failed"
                task.message = "Operación cancelada." if task.cancel_event.is_set() else str(error)
                task.error = None if task.cancel_event.is_set() else str(error)
        finally:
            with self._lock:
                task.process = None

    def _analyze_task(
        self, task: Task, url: str, mode: str
    ) -> dict[str, Any]:
        self._update_task(task, message="Consultando metadatos…")
        ydl_options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
            "extract_flat": "in_playlist",
            "playlistend": 4,
        }
        import yt_dlp

        def extract_preview(
            options: dict[str, Any],
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            with yt_dlp.YoutubeDL(options) as ydl:
                extracted = ydl.extract_info(url, download=False)
                if not extracted:
                    raise RuntimeError("No se pudieron leer los metadatos del enlace.")
                info = dict(extracted)
                entries = info.get("entries")
                preview_entries = (
                    list(itertools.islice(entries, 4)) if entries is not None else []
                )
            return info, preview_entries

        try:
            info, preview_entries = extract_preview(ydl_options)
        except Exception as classic_error:
            if task.cancel_event.is_set():
                raise RuntimeError("Operación cancelada.") from classic_error
            self._update_task(
                task,
                message="El método clásico no respondió; reintentando con Node.js…",
            )
            node_options = {
                **ydl_options,
                "js_runtimes": {"node": {"path": str(RESOURCE_DIR / "node.exe")}},
            }
            try:
                info, preview_entries = extract_preview(node_options)
            except Exception as node_error:
                raise RuntimeError(
                    "Fallaron ambos métodos de análisis. "
                    f"Método clásico: {classic_error}. Método con Node.js: {node_error}"
                ) from node_error

        formats = info.get("formats") or []
        heights = sorted(
            {
                int(item["height"])
                for item in formats
                if item.get("height") and item.get("vcodec") != "none"
            },
            reverse=True,
        )
        resolutions = heights[:3]
        if 1080 in heights and 1080 not in resolutions:
            resolutions.append(1080)
        is_playlist = info.get("_type") == "playlist" or bool(preview_entries)
        thumbnails = [
            thumbnail
            for entry in preview_entries
            if (thumbnail := self._thumbnail_url(entry))
        ][:4]
        thumbnail = self._thumbnail_url(info)
        if not thumbnails and thumbnail:
            thumbnails = [thumbnail]
        return {
            "title": info.get("title") or info.get("playlist_title") or "Sin título",
            "uploader": info.get("uploader") or info.get("channel") or "Desconocido",
            "duration": info.get("duration"),
            "thumbnail": thumbnail,
            "thumbnails": thumbnails,
            "is_playlist": is_playlist,
            "formats": resolutions,
        }

    @staticmethod
    def _thumbnail_url(info: dict[str, Any]) -> str | None:
        thumbnail = info.get("thumbnail")
        if thumbnail:
            return str(thumbnail)
        thumbnails = info.get("thumbnails") or []
        for item in reversed(thumbnails):
            if item.get("url"):
                return str(item["url"])
        return None

    def _download_task(
        self, task: Task, url: str, mode: str, quality: int
    ) -> dict[str, Any]:
        output_key = "dir_audio" if mode == "audio" else "dir_video"
        output_dir = Path(self._config[output_key])
        output_dir.mkdir(parents=True, exist_ok=True)
        executable = RESOURCE_DIR / "core" / "yt-dlp.exe"
        ffmpeg = RESOURCE_DIR / "core" / "ffmpeg.exe"
        if not executable.is_file() or not ffmpeg.is_file():
            raise FileNotFoundError("Faltan yt-dlp.exe o ffmpeg.exe en la carpeta de la aplicación.")

        command = [
            str(executable),
            "--newline",
            "--progress",
            "-P",
            str(output_dir),
            "--ffmpeg-location",
            str(ffmpeg),
        ]
        if mode == "audio":
            command.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0"])
        else:
            if quality:
                fmt = (
                    f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]"
                    f"/bestvideo[height<={quality}]+bestaudio"
                    f"/best[height<={quality}]/best"
                )
            else:
                fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            command.extend(["-f", fmt, "--merge-output-format", "mp4"])
        command.extend(["--", url])

        self._update_task(task, progress=0, message="Iniciando yt-dlp (método clásico)…")
        return_code, classic_error = self._run_download_attempt(task, command)
        if task.cancel_event.is_set():
            raise RuntimeError("Operación cancelada.")
        if return_code != 0:
            self._update_task(
                task,
                progress=0,
                message="El método clásico falló; reintentando con Node.js…",
            )
            node_command = command[:-2] + [
                "--js-runtimes",
                f"node:{RESOURCE_DIR / 'node.exe'}",
                *command[-2:],
            ]
            return_code, node_error = self._run_download_attempt(task, node_command)
            if task.cancel_event.is_set():
                raise RuntimeError("Operación cancelada.")
            if return_code != 0:
                raise RuntimeError(
                    "Fallaron ambos métodos de descarga. "
                    f"Método clásico: {classic_error}. Método con Node.js: {node_error}"
                )
        return {"directory": str(output_dir)}

    def _run_download_attempt(
        self, task: Task, command: list[str]
    ) -> tuple[int, str]:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        self._set_process(task, process)
        if process.stdout is None:
            raise RuntimeError("No se pudo leer la salida del proceso yt-dlp.")
        percent_pattern = re.compile(r"(\d+(?:\.\d+)?)%")
        last_error: str | None = None
        for line in process.stdout:
            if task.cancel_event.is_set():
                self._terminate_process(process)
                break
            match = percent_pattern.search(line)
            if match:
                percent = max(0.0, min(float(match.group(1)), 100.0))
                self._update_task(
                    task,
                    progress=percent / 100,
                    message=f"Descargando… {percent:.1f}%",
                )
            elif line.strip():
                text = line.strip()
                if re.search(r"\b(error|failed|unable to)\b", text, re.IGNORECASE):
                    last_error = text[:300]
                self._update_task(task, message=text[:180])
        return_code = process.wait()
        with self._lock:
            if task.process is process:
                task.process = None
        return return_code, last_error or f"yt-dlp terminó con código {return_code}"

    def _optimization_task(
        self, task: Task, source: Path, settings: dict[str, Any]
    ) -> dict[str, Any]:
        self._update_task(task, message="Leyendo duración del archivo…")
        duration = self._analyzer.obtener_duracion(str(source))
        if duration <= 0:
            raise RuntimeError("FFprobe no pudo leer la duración del archivo.")

        output_dir = Path(self._config["dir_optimized"])
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._new_output_path(output_dir, source.stem)
        builder = FFmpegBuilder(
            str(RESOURCE_DIR / "core" / "ffmpeg.exe"),
            str(source),
            str(output_path),
        )
        if settings["normalize_audio"]:
            self._update_task(task, message="Analizando niveles de audio…")
            stats = self._analyzer.analizar_audio_loudnorm(str(source))
            if not stats:
                raise RuntimeError(
                    "FFmpeg no pudo analizar el audio. Verifica que el video tenga una pista de audio válida."
                )
            builder.aplicar_normalizacion_audio(stats)
        if settings["compression"]:
            builder.activar_compresor_inteligente()
        if settings["anime"]:
            builder.aplicar_optimizacion_anime()
        if settings["denoise"]:
            builder.aplicar_reduccion_ruido()
        if settings["sharpen"]:
            builder.aplicar_nitidez()
        if settings["interpolation"]:
            builder.aplicar_interpolacion_fps(settings["fps"])
        if settings["motion_blur"]:
            builder.aplicar_motion_blur(settings["motion_blur_preset"])

        command = builder.construir_comando()
        command[-1:-1] = ["-progress", "pipe:1", "-nostats"]
        self._update_task(task, message="Procesando con FFmpeg…")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        self._set_process(task, process)
        if process.stdout is None:
            raise RuntimeError("No se pudo leer la salida del proceso FFmpeg.")
        time_pattern = re.compile(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)")
        last_error: str | None = None
        for line in process.stdout:
            if task.cancel_event.is_set():
                self._terminate_process(process)
                break
            match = time_pattern.search(line)
            if match:
                seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                progress = min(seconds / duration, 0.99)
                self._update_task(
                    task,
                    progress=progress,
                    message=f"Procesando… {progress * 100:.0f}%",
                )
            elif re.search(r"\b(error|invalid|failed)\b", line, re.IGNORECASE):
                last_error = line.strip()[:300]
        return_code = process.wait()
        if task.cancel_event.is_set():
            output_path.unlink(missing_ok=True)
            raise RuntimeError("Operación cancelada.")
        if return_code != 0:
            details = f" Detalle: {last_error}" if last_error else ""
            raise RuntimeError(f"FFmpeg terminó con código {return_code}.{details}")
        return {"output_path": str(output_path), "directory": str(output_dir)}

    def _check_update_task(self, task: Task) -> dict[str, Any]:
        if self._update_config_error:
            raise UpdateError(self._update_config_error)
        self._update_task(task, message="Consultando versiones publicadas en GitHub…")
        updater = GitHubReleaseUpdater(self._update_repository, APP_VERSION)
        release = updater.check_latest_release()
        with self._lock:
            self._available_update = release
        if release is None:
            return {
                "configured": True,
                "available": False,
                "current_version": APP_VERSION,
            }
        return {
            "configured": True,
            "available": True,
            "current_version": APP_VERSION,
            "release": release.as_dict(),
        }

    def _install_update_task(
        self, task: Task, release: UpdateRelease
    ) -> dict[str, Any]:
        updater = GitHubReleaseUpdater(self._update_repository, APP_VERSION)
        installer_path = updater.download_installer(
            release,
            APP_DATA_DIR / "updates",
            lambda progress, message: self._update_task(
                task, progress=progress, message=message
            ),
        )
        try:
            subprocess.Popen([str(installer_path)], close_fds=True)
        except OSError as error:
            installer_path.unlink(missing_ok=True)
            raise UpdateError(f"No se pudo iniciar el instalador descargado: {error}") from error
        logging.info(
            "Se inició el instalador de CsTools %s desde GitHub Releases.",
            release.version,
        )
        return {"version": release.version, "installer_path": str(installer_path)}

    @staticmethod
    def _new_output_path(directory: Path, stem: str) -> Path:
        candidate = directory / f"{stem}_optimizado.mp4"
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{stem}_optimizado_{suffix}.mp4"
            suffix += 1
        return candidate

    def _update_task(
        self,
        task: Task,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            if progress is not None:
                task.progress = max(0.0, min(progress, 1.0))
            if message is not None and not task.cancel_event.is_set():
                task.message = message

    def _set_process(self, task: Task, process: subprocess.Popen[str]) -> None:
        with self._lock:
            task.process = process
            should_stop = task.cancel_event.is_set()
        if should_stop:
            self._terminate_process(process)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _task_snapshot(task: Task) -> dict[str, Any]:
        return {
            "id": task.task_id,
            "kind": task.kind,
            "status": task.status,
            "progress": task.progress,
            "message": task.message,
            "result": task.result,
            "error": task.error,
        }


def main() -> None:
    configure_logging()
    api = CsToolsApi()
    window = webview.create_window(
        APP_NAME,
        str(RESOURCE_DIR / "index.html"),
        js_api=api,
        width=1440,
        height=960,
        min_size=(1000, 720),
        background_color="#0b0c0e",
    )
    api.set_window(window)
    try:
        webview.start(_bind_file_drop, window, debug=False)
    finally:
        api.stop_media_preview_server()


if __name__ == "__main__":
    main()
