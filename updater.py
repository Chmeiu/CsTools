from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


MAX_INSTALLER_SIZE = 1024 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateRelease:
    repository: str
    version: str
    tag: str
    notes: str
    release_url: str
    download_url: str
    installer_name: str
    installer_size: int
    sha256: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "version": self.version,
            "tag": self.tag,
            "notes": self.notes,
            "release_url": self.release_url,
            "installer_name": self.installer_name,
            "installer_size": self.installer_size,
        }


class GitHubReleaseUpdater:
    def __init__(
        self,
        repository: str,
        current_version: str,
        installer_name: str = "Instalar_CsTools.exe",
    ) -> None:
        self.repository = repository.strip()
        self.current_version = current_version
        self.installer_name = installer_name
        if self.repository and not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository
        ):
            raise UpdateError(
                "El repositorio de actualizaciones debe tener el formato propietario/repositorio."
            )
        self._version_tuple(current_version)

    def check_latest_release(self) -> UpdateRelease | None:
        if not self.repository:
            raise UpdateError(
                "Las actualizaciones estarán disponibles cuando se configure el repositorio público de CsTools."
            )

        api_url = (
            "https://api.github.com/repos/"
            f"{self.repository}/releases/latest"
        )
        request = Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "CsTools-Updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                release_data = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 404:
                raise UpdateError(
                    "No se encontró el repositorio público o todavía no tiene un Release publicado."
                ) from error
            raise UpdateError(
                f"GitHub rechazó la consulta de actualizaciones (HTTP {error.code})."
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise UpdateError(
                f"No se pudo consultar GitHub. Comprueba tu conexión e inténtalo de nuevo: {error}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("GitHub devolvió una respuesta de actualización no válida.") from error

        if not isinstance(release_data, dict):
            raise UpdateError("GitHub devolvió datos de Release con un formato no válido.")

        tag = release_data.get("tag_name")
        if not isinstance(tag, str):
            raise UpdateError("El Release de GitHub no tiene una etiqueta de versión válida.")
        release_version = tag.removeprefix("v")
        latest_version = self._version_tuple(release_version)
        if latest_version <= self._version_tuple(self.current_version):
            return None

        release_url = release_data.get("html_url")
        if (
            not self._is_github_release_url(release_url)
            or urlparse(release_url).path.split("/")[1:3]
            != self.repository.split("/")
        ):
            raise UpdateError("La dirección del Release no pertenece a GitHub.")
        assets = release_data.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("El Release no contiene una lista de archivos válida.")
        asset = next(
            (
                item
                for item in assets
                if isinstance(item, dict)
                and item.get("name") == self.installer_name
            ),
            None,
        )
        if asset is None:
            raise UpdateError(
                f"El Release {tag} no incluye {self.installer_name}."
            )

        download_url = asset.get("browser_download_url")
        if not self._is_github_asset_url(download_url):
            raise UpdateError("El enlace del instalador no pertenece a GitHub Releases.")
        size = asset.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INSTALLER_SIZE:
            raise UpdateError("El tamaño del instalador publicado no es válido.")
        digest = asset.get("digest")
        match = re.fullmatch(r"sha256:([a-fA-F0-9]{64})", digest or "")
        expected_sha256 = match.group(1).lower() if match else None
        if expected_sha256 is None:
            checksum_asset = next(
                (
                    item
                    for item in assets
                    if isinstance(item, dict)
                    and item.get("name") == f"{self.installer_name}.sha256"
                ),
                None,
            )
            checksum_url = checksum_asset.get("browser_download_url") if checksum_asset else None
            if not self._is_github_asset_url(checksum_url, f"{self.installer_name}.sha256"):
                raise UpdateError(
                    "El Release no incluye una huella SHA-256 verificable del instalador."
                )
            try:
                with urlopen(Request(checksum_url, headers={"User-Agent": "CsTools-Updater"}), timeout=15) as response:
                    checksum_text = response.read(512).decode("ascii").strip()
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError) as error:
                raise UpdateError(f"No se pudo obtener la huella SHA-256: {error}") from error
            checksum_match = re.fullmatch(
                rf"([a-fA-F0-9]{{64}})\s+\*?{re.escape(self.installer_name)}",
                checksum_text,
            )
            if not checksum_match:
                raise UpdateError("El archivo de checksum del Release no tiene un formato válido.")
            expected_sha256 = checksum_match.group(1).lower()

        notes = release_data.get("body")
        if not isinstance(notes, str):
            notes = ""
        return UpdateRelease(
            repository=self.repository,
            version=release_version,
            tag=tag,
            notes=notes[:4000],
            release_url=release_url,
            download_url=download_url,
            installer_name=self.installer_name,
            installer_size=size,
            sha256=expected_sha256,
        )

    def download_installer(
        self,
        release: UpdateRelease,
        directory: Path,
        on_progress: Callable[[float, str], None],
    ) -> Path:
        if release.repository != self.repository:
            raise UpdateError("El Release seleccionado no corresponde al repositorio configurado.")
        directory.mkdir(parents=True, exist_ok=True)
        request = Request(
            release.download_url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "CsTools-Updater",
            },
        )
        temporary_path: Path | None = None
        try:
            with urlopen(request, timeout=30) as response:
                final_url = urlparse(response.geturl())
                if final_url.scheme != "https" or final_url.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                    raise UpdateError("La descarga fue redirigida a un servidor no permitido.")
                descriptor, name = tempfile.mkstemp(
                    prefix="CsTools-update-",
                    suffix=".exe",
                    dir=directory,
                )
                temporary_path = Path(name)
                digest = hashlib.sha256()
                downloaded = 0
                with os.fdopen(descriptor, "wb") as installer:
                    while chunk := response.read(1024 * 1024):
                        downloaded += len(chunk)
                        if downloaded > release.installer_size or downloaded > MAX_INSTALLER_SIZE:
                            raise UpdateError("El instalador descargado supera el tamaño publicado.")
                        installer.write(chunk)
                        digest.update(chunk)
                        progress = min(downloaded / release.installer_size, 0.99)
                        on_progress(progress, f"Descargando actualización… {progress * 100:.0f}%")
                if downloaded != release.installer_size:
                    raise UpdateError("La descarga del instalador quedó incompleta.")
                if digest.hexdigest() != release.sha256:
                    raise UpdateError("La huella SHA-256 del instalador no coincide.")
            on_progress(1.0, "Descarga verificada.")
            return temporary_path
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if isinstance(error, UpdateError):
                raise
            raise UpdateError(f"No se pudo descargar el instalador: {error}") from error
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _version_tuple(version: str) -> tuple[int, int, int]:
        if not isinstance(version, str) or not re.fullmatch(
            r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version
        ):
            raise UpdateError(f"La versión {version!r} no tiene el formato esperado X.Y.Z.")
        major, minor, patch = (int(part) for part in version.split(".", maxsplit=2))
        return major, minor, patch

    def _is_github_asset_url(self, value: object, asset_name: str | None = None) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        expected_path = f"/{self.repository}/releases/download/"
        name = asset_name or self.installer_name
        return (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.path.startswith(expected_path)
            and parsed.path.endswith("/" + quote(name))
        )

    @staticmethod
    def _is_github_release_url(value: object) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.path.startswith("/")
            and "/releases/tag/" in parsed.path
        )
