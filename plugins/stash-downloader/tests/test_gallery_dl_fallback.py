import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "download.py"
SPEC = importlib.util.spec_from_file_location("stash_downloader_backend", SCRIPT_PATH)
backend = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backend
SPEC.loader.exec_module(backend)


class GalleryDlFallbackTests(unittest.TestCase):
    def test_normalizes_tiktok_photo_messages_as_gallery_metadata(self):
        messages = [
            [2, {
                "id": "123",
                "desc": "Photo post",
                "user": "creator_handle",
                "author": {"nickname": "Creator Name"},
            }],
            [3, "https://cdn.example/one.jpg", {
                "type": "image",
                "num": 1,
                "extension": "jpg",
                "width": 800,
                "height": 600,
            }],
            [3, "https://cdn.example/two.webp", {
                "type": "image",
                "num": 2,
                "extension": "webp",
            }],
        ]

        result = backend._normalize_gallery_dl_tiktok_metadata(
            messages, "https://www.tiktok.com/@creator/photo/123"
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["content_type"], "gallery")
        self.assertEqual(result["uploader"], "creator_handle")
        self.assertEqual(result["creator"], "Creator Name")
        self.assertEqual(len(result["gallery_images"]), 2)
        self.assertEqual(result["gallery_images"][1]["filename"], "tiktok-123-02.webp")

    @patch.object(backend.subprocess, "run")
    @patch.object(backend, "get_gallery_dl_cmd", return_value=["gallery-dl"])
    def test_extracts_gallery_metadata_with_proxy(self, _get_command, run):
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        run.return_value.stdout = json.dumps([
            [2, {"id": "123", "desc": "Post", "user": "creator"}],
            [3, "https://cdn.example/one.jpg", {"type": "image", "extension": "jpg"}],
        ])

        result, error = backend.extract_gallery_dl_tiktok_metadata(
            "https://www.tiktok.com/@creator/photo/123",
            proxy="http://proxy.example:8080",
        )

        self.assertIsNone(error)
        self.assertEqual(result["content_type"], "gallery")
        command = run.call_args.args[0]
        self.assertIn("--dump-json", command)
        self.assertIn("--proxy", command)
        self.assertIn("http://proxy.example:8080", command)

    @patch.object(backend.subprocess, "run")
    @patch.object(backend, "get_gallery_dl_cmd", return_value=[sys.executable, "-m", "gallery_dl"])
    def test_gallery_dl_check_reports_version_and_python_module_path(self, _get_command, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "1.29.2\n"
        run.return_value.stderr = ""

        result = backend.task_check_gallery_dl({})

        self.assertTrue(result["available"])
        self.assertEqual(result["version"], "1.29.2")
        self.assertEqual(result["resolved_path"], f"{sys.executable} -m gallery_dl")
        self.assertEqual(result["resolution_method"], "Python module")
        self.assertEqual(run.call_args.args[0][-1], "--version")

    @patch.object(backend, "extract_gallery_dl_tiktok_metadata")
    @patch.object(backend.subprocess, "run")
    @patch.object(backend, "get_ytdlp_cmd", return_value=["yt-dlp"])
    @patch.object(backend, "extract_tiktok_photo_post", return_value={"extraction_error": "No carousel data"})
    def test_metadata_uses_gallery_dl_after_tiktok_extractors_fail(
        self, _page_parser, _get_ytdlp_command, run, gallery_dl
    ):
        run.return_value.returncode = 1
        run.return_value.stderr = "yt-dlp failed"
        gallery_metadata = {"content_type": "gallery", "gallery_images": [{"url": "https://cdn.example/a.jpg"}]}
        gallery_dl.return_value = (gallery_metadata, None)

        result = backend.extract_metadata(
            "https://www.tiktok.com/@creator/photo/123",
            gallery_dl_path="/custom/gallery-dl",
        )

        self.assertIs(result, gallery_metadata)
        gallery_dl.assert_called_once()

    @patch.object(backend, "get_gallery_dl_cmd", return_value=["gallery-dl"])
    @patch.object(backend.subprocess, "Popen")
    def test_download_fallback_selects_requested_photo_index(self, popen, _get_command):
        with tempfile.TemporaryDirectory() as output_dir:
            def create_fake_download(command, **_kwargs):
                download_dir = Path(command[command.index("-d") + 1])
                gallery_folder = download_dir / "TikTok" / "creator_handle"
                gallery_folder.mkdir(parents=True)
                (gallery_folder / "123_1.jpg").write_bytes(b"first")
                (gallery_folder / "123_2.webp").write_bytes(b"second")

                class CompletedProcess:
                    returncode = 0

                    @staticmethod
                    def poll():
                        return 0

                return CompletedProcess()

            popen.side_effect = create_fake_download
            downloaded_path, error = backend.download_with_gallery_dl(
                "https://www.tiktok.com/@creator/photo/123",
                output_dir,
                filename="tiktok-123-02.webp",
                gallery_index=2,
            )

            self.assertIsNone(error)
            self.assertEqual(Path(downloaded_path).read_bytes(), b"second")
            self.assertEqual(Path(downloaded_path).name, "tiktok-123-02.webp")
            self.assertEqual(Path(downloaded_path).parent.name, "creator_handle")

    @patch.object(backend, "download_with_gallery_dl")
    @patch.object(backend, "download_video", return_value=(None, "yt-dlp failed"))
    @patch.object(backend, "get_ytdlp_cmd", return_value=["yt-dlp"])
    def test_tiktok_video_download_uses_gallery_dl_after_ytdlp_failure(
        self, _get_ytdlp_command, download_video, download_gallery_dl
    ):
        with tempfile.TemporaryDirectory() as output_dir:
            file_path = Path(output_dir) / "video.mp4"
            file_path.write_bytes(b"video")
            download_gallery_dl.return_value = (str(file_path), None)
            gallery_download_path = str(Path(output_dir) / "custom-gallery-dl")

            result = backend.task_download({
                "url": "https://www.tiktok.com/@creator/video/123",
                "output_dir": output_dir,
                "gallery_dl_download_path": gallery_download_path,
            })

            self.assertTrue(result["success"])
            self.assertEqual(result["file_path"], str(file_path))
            download_gallery_dl.assert_called_once()
            self.assertEqual(download_gallery_dl.call_args.args[1], gallery_download_path)
            self.assertEqual(download_video.call_args.args[1], output_dir)

    @patch.object(backend, "download_with_gallery_dl")
    @patch.object(backend, "download_direct_file", return_value=None)
    def test_gallery_image_download_passes_original_post_and_index(self, _direct_download, download_gallery_dl):
        with tempfile.TemporaryDirectory() as output_dir:
            file_path = Path(output_dir) / "image.jpg"
            file_path.write_bytes(b"image")
            download_gallery_dl.return_value = (str(file_path), None)
            result = backend.task_download({
                "url": "https://cdn.example/image.jpg",
                "fallback_url": "https://www.tiktok.com/@creator/photo/123",
                "output_dir": output_dir,
                "force_direct": True,
                "gallery_index": 2,
            })

            self.assertTrue(result["success"])
            self.assertEqual(download_gallery_dl.call_args.args[0], "https://www.tiktok.com/@creator/photo/123")
            self.assertEqual(download_gallery_dl.call_args.args[1], str(Path(output_dir) / "gallery-dl"))
            self.assertEqual(download_gallery_dl.call_args.kwargs["gallery_index"], 2)

    @patch.object(backend, "download_direct_file")
    def test_tiktok_gallery_image_prefers_configured_gallery_dl_directory(self, download_direct):
        with tempfile.TemporaryDirectory() as temp_dir:
            library_dir = Path(temp_dir) / "library"
            gallery_dl_dir = Path(temp_dir) / "gallery-dl-custom"
            downloaded_file = gallery_dl_dir / "photo.jpg"

            def save_file(_url, output_dir, filename, **_kwargs):
                self.assertEqual(output_dir, str(gallery_dl_dir))
                Path(output_dir).mkdir(parents=True, exist_ok=True)
                path = Path(output_dir) / filename
                path.write_bytes(b"image")
                return str(path)

            download_direct.side_effect = save_file
            result = backend.task_download({
                "url": "https://cdn.example/photo.jpg",
                "fallback_url": "https://www.tiktok.com/@creator/photo/123",
                "output_dir": str(library_dir),
                "gallery_dl_download_path": str(gallery_dl_dir),
                "force_direct": True,
                "gallery_index": 1,
                "filename": "photo.jpg",
            })

            self.assertTrue(result["success"])
            self.assertEqual(result["file_path"], str(downloaded_file))
            self.assertTrue(downloaded_file.is_file())


if __name__ == "__main__":
    unittest.main()
