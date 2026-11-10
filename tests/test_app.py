import base64
import io
import json
import tempfile
import time
import unittest
import logging
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import MagicMock, patch

import app
from updater import GitHubReleaseUpdater, UpdateError


class CsToolsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config_patch = patch.object(app, "CONFIG_FILE", self.root / "config.json")
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.home_patch = patch.object(Path, "home", return_value=self.root / "home")
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.api = app.CsToolsApi()
        self.addCleanup(self.api.stop_media_preview_server)

    def test_defaults_are_persisted_and_all_output_directories_exist(self):
        self.assertEqual(
            set(self.api.get_config()),
            {"dir_video", "dir_audio", "dir_optimized"},
        )
        self.assertTrue(app.CONFIG_FILE.exists())
        for directory in self.api.get_config().values():
            self.assertTrue(Path(directory).is_dir())

    def test_url_validation_accepts_http_and_rejects_non_web_schemes(self):
        self.assertEqual(
            self.api._validate_url(" https://example.com/video "),
            "https://example.com/video",
        )
        for value in ("", "file:///C:/video.mp4", "https:/missing-host"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api._validate_url(value)

    def test_processing_options_validate_and_keep_supported_values(self):
        settings = self.api._validate_settings(
            {
                "compression": True,
                "interpolation": True,
                "fps": "90",
                "motion_blur_preset": "Sutil (Ligero)",
            }
        )
        self.assertTrue(settings["compression"])
        self.assertTrue(settings["interpolation"])
        self.assertEqual(settings["fps"], "90")
        self.assertFalse(settings["normalize_audio"])

    def test_invalid_fps_and_motion_profile_are_rejected(self):
        for settings in (
            {"fps": "999"},
            {"motion_blur_preset": "unknown"},
            {"compression": "yes"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.api._validate_settings(settings)

    def test_quality_accepts_resolutions_reported_by_the_extractor(self):
        self.assertEqual(self.api._validate_quality(540), 540)
        self.assertEqual(self.api._validate_quality(0), 0)
        for quality in (-1, 20000, True, "1080"):
            with self.subTest(quality=quality), self.assertRaises(ValueError):
                self.api._validate_quality(quality)

    def test_input_file_validation_accepts_supported_videos_only(self):
        source = self.root / "sample.MP4"
        source.touch()
        self.assertEqual(
            self.api.validate_input_file(str(source)),
            str(source.resolve()),
        )
        unsupported = self.root / "sample.txt"
        unsupported.touch()
        for path in (str(unsupported), str(self.root / "missing.mp4")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.api.validate_input_file(path)

    def test_input_preview_returns_jpeg_data_and_keeps_valid_file_on_preview_failure(self):
        source = self.root / "sample.mp4"
        source.touch()
        jpeg = b"\xff\xd8sample-jpeg\xff\xd9"
        success = type(
            "CompletedProcess",
            (),
            {"returncode": 0, "stdout": jpeg, "stderr": b""},
        )()
        with patch("app.subprocess.run", return_value=success) as run:
            result = self.api.prepare_input_file(str(source))

        self.assertEqual(result["path"], str(source.resolve()))
        self.assertRegex(result["media_url"], r"^http://127\.0\.0\.1:\d+/media/[0-9a-f]{32}$")
        self.assertEqual(
            result["thumbnail"],
            "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
        )
        self.assertIsNone(result["preview_error"])
        self.assertIn("image2pipe", run.call_args.args[0])

        failed = type(
            "CompletedProcess",
            (),
            {"returncode": 1, "stdout": b"", "stderr": b"unsupported codec"},
        )()
        with patch("app.subprocess.run", return_value=failed):
            with self.assertLogs(level=logging.WARNING):
                result = self.api.prepare_input_file(str(source))
        self.assertEqual(result["path"], str(source.resolve()))
        self.assertIsNone(result["thumbnail"])
        self.assertEqual(result["preview_error"], "unsupported codec")

    def test_media_preview_supports_browser_range_requests_and_rejects_unknown_tokens(self):
        source = self.root / "sample.mp4"
        source.write_bytes(b"0123456789")
        media_url = self.api.create_media_preview(str(source))

        request = Request(media_url, headers={"Range": "bytes=2-5"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.read(), b"2345")

        try:
            urlopen(media_url.rsplit("/", 1)[0] + "/" + "0" * 32)
        except HTTPError as error:
            self.assertEqual(error.code, 404)
            error.close()
        else:
            self.fail("An unknown media-preview token should be rejected.")

    def test_native_file_drop_paths_are_forwarded_to_the_ui(self):
        window = MagicMock()
        app._forward_dropped_files(
            window,
            {
                "dataTransfer": {
                    "files": [
                        {"name": "sample.mp4", "pywebviewFullPath": "C:\\Videos\\sample.mp4"},
                        {"name": "missing-path.mp4"},
                    ]
                }
            },
        )

        script = window.evaluate_js.call_args.args[0]
        self.assertIn("cstools-file-drop", script)
        self.assertIn("C:\\\\Videos\\\\sample.mp4", script)
        self.assertNotIn("missing-path", script)
        window.evaluate_js.reset_mock()
        app._forward_dropped_files(window, {"dataTransfer": {"files": []}})
        window.evaluate_js.assert_not_called()

    def test_classic_metadata_does_not_enable_node_runtime(self):
        import yt_dlp

        with patch.object(yt_dlp, "YoutubeDL") as youtube_dl:
            context = youtube_dl.return_value.__enter__.return_value
            context.extract_info.return_value = {
                "title": "Sample",
                "formats": [{"height": 540, "vcodec": "h264"}],
            }
            result = self.api._analyze_task(
                app.Task(task_id="metadata-test", kind="metadata-video"),
                "https://example.com/video",
                "video",
            )

        self.assertEqual(youtube_dl.call_count, 1)
        options = youtube_dl.call_args.args[0]
        self.assertNotIn("js_runtimes", options)
        self.assertEqual(options["extract_flat"], "in_playlist")
        self.assertEqual(options["playlistend"], 4)
        self.assertEqual(result["formats"], [540])

    def test_playlist_metadata_only_reads_first_four_entries_and_covers_both_modes(self):
        import yt_dlp

        with patch.object(yt_dlp, "YoutubeDL") as youtube_dl:
            context = youtube_dl.return_value.__enter__.return_value
            context.extract_info.side_effect = lambda *_args, **_kwargs: {
                "_type": "playlist",
                "title": "Playlist de prueba",
                "entries": (
                    {"title": f"Entry {index}", "thumbnail": f"https://img/{index}.jpg"}
                    for index in range(1, 11)
                ),
            }
            results = [
                self.api._analyze_task(
                    app.Task(task_id=f"playlist-{mode}", kind=f"metadata-{mode}"),
                    "https://example.com/playlist",
                    mode,
                )
                for mode in ("video", "audio")
            ]

        self.assertEqual(youtube_dl.call_count, 2)
        for result, call in zip(results, youtube_dl.call_args_list):
            options = call.args[0]
            self.assertFalse(options["noplaylist"])
            self.assertEqual(options["playlistend"], 4)
            self.assertEqual(options["extract_flat"], "in_playlist")
            self.assertTrue(result["is_playlist"])
            self.assertEqual(
                result["thumbnails"],
                [f"https://img/{index}.jpg" for index in range(1, 5)],
            )
            self.assertEqual(result["title"], "Playlist de prueba")

    def test_metadata_retries_with_bundled_node_runtime_after_classic_failure(self):
        import yt_dlp

        classic_context = MagicMock()
        classic_context.extract_info.side_effect = RuntimeError("classic blocked")
        node_context = MagicMock()
        node_context.extract_info.return_value = {
            "title": "Sample",
            "formats": [{"height": 540, "vcodec": "h264"}],
        }
        with patch.object(yt_dlp, "YoutubeDL") as youtube_dl:
            youtube_dl.return_value.__enter__.side_effect = [
                classic_context,
                node_context,
            ]
            result = self.api._analyze_task(
                app.Task(task_id="metadata-fallback", kind="metadata-video"),
                "https://example.com/video",
                "video",
            )

        self.assertEqual(youtube_dl.call_count, 2)
        self.assertNotIn("js_runtimes", youtube_dl.call_args_list[0].args[0])
        self.assertEqual(youtube_dl.call_args_list[0].args[0]["playlistend"], 4)
        self.assertEqual(
            youtube_dl.call_args_list[1].args[0]["js_runtimes"]["node"]["path"],
            str(app.RESOURCE_DIR / "node.exe"),
        )
        self.assertEqual(youtube_dl.call_args_list[1].args[0]["playlistend"], 4)
        self.assertEqual(result["formats"], [540])

    def test_download_retries_with_node_runtime_after_classic_failure(self):
        classic_process = MagicMock()
        classic_process.stdout = io.StringIO("ERROR: JavaScript challenge\n")
        classic_process.wait.return_value = 1
        node_process = MagicMock()
        node_process.stdout = io.StringIO("[download] 100.0%\n")
        node_process.wait.return_value = 0
        task = app.Task(task_id="download-fallback", kind="download-video")

        with patch(
            "app.subprocess.Popen", side_effect=[classic_process, node_process]
        ) as popen:
            result = self.api._download_task(
                task, "https://example.com/video", "video", 0
            )

        classic_command = popen.call_args_list[0].args[0]
        node_command = popen.call_args_list[1].args[0]
        self.assertNotIn("--js-runtimes", classic_command)
        self.assertIn("--js-runtimes", node_command)
        self.assertIn(f"node:{app.RESOURCE_DIR / 'node.exe'}", node_command)
        self.assertEqual(result["directory"], self.api.get_config()["dir_video"])

    def test_cookie_download_mode_is_not_available(self):
        with self.assertRaises(ValueError):
            self.api.analyze_url("https://example.com/video", "protected")

    def test_tasks_return_results_and_report_failures(self):
        success_id = self.api._start_task("test", lambda task: {"value": 7})
        success = self._wait_for_task(success_id)
        self.assertEqual(success["status"], "completed")
        self.assertEqual(success["result"], {"value": 7})

        with self.assertLogs(level=logging.ERROR):
            failure_id = self.api._start_task(
                "test", lambda task: (_ for _ in ()).throw(RuntimeError("expected test error"))
            )
            failure = self._wait_for_task(failure_id)
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["error"], "expected test error")

    def test_optimization_outputs_do_not_overwrite_existing_files(self):
        output = self.root / "output"
        output.mkdir()
        (output / "clip_optimizado.mp4").touch()
        next_path = self.api._new_output_path(output, "clip")
        self.assertEqual(next_path.name, "clip_optimizado_2.mp4")

    def test_update_check_reports_unconfigured_repository_without_network(self):
        self.api._update_repository = ""
        task_id = self.api.start_update_check()
        task = self._wait_for_task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("configure el repositorio", task["error"])

    def test_github_updater_parses_release_and_sha256_asset_when_digest_missing(self):
        import hashlib

        installer_name = "Instalar_CsTools.exe"
        payload = b"fake-installer-data"
        checksum = hashlib.sha256(payload).hexdigest()
        release_data = {
            "tag_name": "v1.0.1",
            "html_url": "https://github.com/Chmeiu/CsTools/releases/tag/v1.0.1",
            "body": "Fixes",
            "assets": [
                {"name": installer_name, "size": len(payload), "browser_download_url": f"https://github.com/Chmeiu/CsTools/releases/download/v1.0.1/{installer_name}"},
                {"name": installer_name + ".sha256", "browser_download_url": f"https://github.com/Chmeiu/CsTools/releases/download/v1.0.1/{installer_name}.sha256"},
            ],
        }
        class Response:
            def __init__(self, data, url="https://github.com/Chmeiu/CsTools/releases/download/v1.0.1/asset"):
                self.data, self.url = data, url
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return self.data if size < 0 else self.data[:size]
            def geturl(self): return self.url
        responses = [Response(json.dumps(release_data).encode()), Response(f"{checksum}  {installer_name}".encode("ascii"))]
        with patch("updater.urlopen", side_effect=responses):
            release = GitHubReleaseUpdater("Chmeiu/CsTools", "1.0.0").check_latest_release()
        self.assertEqual(release.sha256, checksum)
        self.assertEqual(release.version, "1.0.1")

    def test_github_updater_rejects_malformed_checksum(self):
        updater = GitHubReleaseUpdater("Chmeiu/CsTools", "1.0.0")
        with self.assertRaises(UpdateError):
            updater._version_tuple("v1.0")
        self.assertFalse(updater._is_github_asset_url("https://evil.example/installer.exe"))

    def test_update_api_reports_version_and_requires_release_before_install(self):
        self.assertEqual(self.api.get_app_version(), app.APP_VERSION)
        with self.assertRaises(app.UpdateError):
            self.api.start_update_install()

    def _wait_for_task(self, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = self.api.get_task(task_id)
            if snapshot["status"] != "running":
                return snapshot
            time.sleep(0.01)
        self.fail(f"Task {task_id} did not finish in time")


if __name__ == "__main__":
    unittest.main()

