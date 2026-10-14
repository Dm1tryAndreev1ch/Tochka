"""Offline downloader contracts: tiny responses only, no runtime/model imports."""

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/fetch_models.py"
MODULE_SPEC = importlib.util.spec_from_file_location("fetch_models", MODULE_PATH)
fetch = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(fetch)


def artifact(body=b"abcdefghij", name="model.bin", **kwargs):
    return {
        "file": name, "repo": "test/model", "revision": "a" * 40,
        "source_file": name, "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body), "license": "test fixture", "profiles": ["portable"],
        **kwargs,
    }


class Response:
    def __init__(self, body, status=200, headers=None, *, break_after=None):
        self.status = status
        self.headers = {"ETag": '"version1"', "Content-Length": str(len(body)), **(headers or {})}
        self.body = io.BytesIO(body)
        self.break_after = break_after
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        self.reads += 1
        if self.break_after and self.reads > 1:
            raise ConnectionResetError("fixture connection broke")
        return self.body.read(self.break_after or size)


class FetchModelsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spec = artifact()
        self.messages = []

    def downloader(self, opener=None, retries=0):
        return fetch.Downloader(self.root, opener=opener, retries=retries,
                                output=self.messages.append, sleep=lambda _: None)

    def catalog(self, records=None):
        path = self.root / "catalog.json"
        path.write_text(json.dumps({"schema_version": 1, "artifacts": records or [self.spec]}))
        return path

    def write_partial(self, body=b"abcd", etag='"version1"'):
        part = self.root / "model.bin.part"
        part.write_bytes(body)
        (self.root / "model.bin.part.json").write_text(json.dumps({
            "url": fetch.source_url(self.spec), "etag": etag,
            "expected_sha256": self.spec["sha256"], "expected_bytes": self.spec["bytes"],
            "partial_bytes": len(body), "partial_sha256": hashlib.sha256(body).hexdigest(),
        }))

    def test_profiles_include_runtime_dependencies_and_strict_pins(self):
        catalog = fetch.load_catalog()
        groups = {profile: {s["file"] for s in catalog if profile in s["profiles"]}
                  for profile in fetch.PROFILES}
        self.assertEqual(groups["llm-worker"], {"qwen3-4b-q4.gguf"})
        self.assertTrue({"whisper-small/model.bin", "whisper-small/config.json",
                         "whisper-small/tokenizer.json", "whisper-small/vocabulary.txt",
                         "ru_RU-denis-medium.onnx", "ru_RU-denis-medium.onnx.json"} <= groups["portable"])
        self.assertTrue({"whisper-turbo-metal.bin", "qwen3-tts-base-0.6b/model.safetensors",
                         "qwen3-tts-base-0.6b/speech_tokenizer/model.safetensors",
                         "voices/metadata.json", "voices/male.wav", "voices/female.wav"} <= groups["mac-metal"])

    def test_dry_run_is_offline_and_does_not_create_directory(self):
        destination = self.root / "does-not-exist"
        opener = mock.Mock(side_effect=AssertionError("network called"))
        result = fetch.run("portable", destination, dry_run=True, catalog=self.catalog(),
                           opener=opener, output=self.messages.append)
        self.assertEqual(result, 0)
        self.assertFalse(destination.exists())
        opener.assert_not_called()
        self.assertTrue(any("10 bytes" in line for line in self.messages))

    def test_distributed_profile_excludes_llm_but_keeps_speech(self):
        opener = mock.Mock(side_effect=AssertionError("network called"))
        fetch.run("portable", self.root / "missing", dry_run=True, exclude_llm=True,
                  opener=opener, output=self.messages.append)
        self.assertTrue(any("whisper-small/model.bin" in line for line in self.messages))
        self.assertFalse(any("qwen3-4b-q4.gguf" in line for line in self.messages))
        opener.assert_not_called()

    def test_download_and_manifest_then_verified_cache_never_uses_network(self):
        opener = mock.Mock(return_value=Response(b"abcdefghij"))
        catalog = self.catalog()
        fetch.run("portable", self.root, catalog=catalog, opener=opener, output=self.messages.append)
        opener.assert_called_once()
        self.assertEqual((self.root / "model.bin").read_bytes(), b"abcdefghij")
        manifest = json.loads((self.root / fetch.MANIFEST).read_text())
        self.assertEqual(manifest["artifacts"][0]["sha256"], self.spec["sha256"])
        self.assertEqual(manifest["artifacts"][0]["verification"], "sha256-against-pinned-catalog")
        no_network = mock.Mock(side_effect=AssertionError("network called"))
        fetch.run("portable", self.root, catalog=catalog, opener=no_network, output=self.messages.append)
        no_network.assert_not_called()
        self.assertFalse((self.root / fetch.LOCK).exists())

    def test_interrupted_transfer_resumes_only_validated_prefix(self):
        requests = []
        responses = [Response(b"abcdefghij", break_after=4),
                     Response(b"efghij", 206, {"Content-Range": "bytes 4-9/10"})]
        def opener(request, timeout):
            requests.append(request)
            return responses.pop(0)
        self.downloader(opener, retries=1).fetch(self.spec)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].get_header("Range"), "bytes=4-")
        self.assertEqual(requests[1].get_header("If-range"), '"version1"')
        self.assertEqual((self.root / "model.bin").read_bytes(), b"abcdefghij")
        self.assertFalse((self.root / "model.bin.part.json").exists())

    def test_ignored_range_restarts_without_concatenating(self):
        self.write_partial()
        opener = mock.Mock(return_value=Response(b"abcdefghij"))
        self.downloader(opener).fetch(self.spec)
        self.assertEqual((self.root / "model.bin").read_bytes(), b"abcdefghij")
        self.assertEqual(opener.call_args.args[0].get_header("Range"), "bytes=4-")

    def test_changed_etag_does_not_append_response(self):
        self.write_partial()
        opener = mock.Mock(side_effect=[
            Response(b"efghij", 206, {"Content-Range": "bytes 4-9/10", "ETag": '"changed"'}),
            Response(b"abcdefghij"),
        ])
        self.downloader(opener, retries=1).fetch(self.spec)
        self.assertIsNone(opener.call_args_list[1].args[0].get_header("Range"))
        self.assertEqual((self.root / "model.bin").read_bytes(), b"abcdefghij")

    def test_invalid_content_range_preserves_final_file(self):
        self.write_partial()
        (self.root / "model.bin").write_bytes(b"old model")
        response = Response(b"efghij", 206, {"Content-Range": "bytes 3-8/10"})
        with self.assertRaisesRegex(fetch.FetchError, "Content-Range mismatch"):
            self.downloader(lambda *a, **k: response).fetch(self.spec)
        self.assertEqual((self.root / "model.bin").read_bytes(), b"old model")
        self.assertFalse((self.root / "model.bin.part").exists())

    def test_corrupt_partial_restarts_with_no_range(self):
        self.write_partial()
        (self.root / "model.bin.part").write_bytes(b"xxxx")
        opener = mock.Mock(return_value=Response(b"abcdefghij"))
        self.downloader(opener).fetch(self.spec)
        self.assertIsNone(opener.call_args.args[0].get_header("Range"))

    def test_digest_mismatch_never_replaces_existing_final_or_records_success(self):
        (self.root / "model.bin").write_bytes(b"old model")
        opener = mock.Mock(return_value=Response(b"XXXXXXXXXX"))
        with self.assertRaisesRegex(fetch.FetchError, "SHA-256"):
            fetch.run("portable", self.root, catalog=self.catalog(), opener=opener,
                      retries=0, output=self.messages.append)
        self.assertEqual((self.root / "model.bin").read_bytes(), b"old model")
        self.assertFalse((self.root / fetch.MANIFEST).exists())
        self.assertFalse((self.root / fetch.LOCK).exists())

    def test_complete_valid_partial_is_promoted_without_network(self):
        (self.root / "model.bin.part").write_bytes(b"abcdefghij")
        opener = mock.Mock(side_effect=AssertionError("network called"))
        self.downloader(opener).fetch(self.spec)
        self.assertEqual((self.root / "model.bin").read_bytes(), b"abcdefghij")
        opener.assert_not_called()

    def test_complete_partial_does_not_reserve_duplicate_disk_space(self):
        (self.root / "model.bin.part").write_bytes(b"abcdefghij")
        opener = mock.Mock(side_effect=AssertionError("network called"))
        with mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=fetch.SPACE_MARGIN)):
            fetch.run("portable", self.root, catalog=self.catalog(), opener=opener, output=self.messages.append)
        self.assertEqual((self.root / "model.bin").read_bytes(), b"abcdefghij")
        opener.assert_not_called()

    def test_malformed_resume_state_is_discarded(self):
        self.write_partial(etag=123)
        opener = mock.Mock(return_value=Response(b"abcdefghij"))
        self.downloader(opener).fetch(self.spec)
        self.assertIsNone(opener.call_args.args[0].get_header("Range"))

    def test_https_redirect_may_not_downgrade(self):
        handler = fetch.HTTPSOnlyRedirect()
        with self.assertRaisesRegex(fetch.FetchError, "downgrades HTTPS"):
            handler.redirect_request(None, None, 302, "", {}, "http://example.test/model")

    def test_low_disk_space_stops_before_network_or_existing_replacement(self):
        (self.root / "model.bin").write_bytes(b"old model")
        opener = mock.Mock(side_effect=AssertionError("network called"))
        with mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=5)):
            with self.assertRaisesRegex(fetch.FetchError, "Insufficient free space"):
                fetch.run("portable", self.root, catalog=self.catalog(), opener=opener,
                          output=self.messages.append)
        opener.assert_not_called()
        self.assertEqual((self.root / "model.bin").read_bytes(), b"old model")

    def test_bundle_copy_is_verified_and_does_not_use_network(self):
        source_root = self.root / "source"
        source_root.mkdir()
        (source_root / "voice.wav").write_bytes(b"fixture voice")
        spec = artifact(b"fixture voice", "voices/female.wav", source="bundled", source_file="voice.wav")
        opener = mock.Mock(side_effect=AssertionError("network called"))
        with mock.patch.object(fetch, "ROOT", source_root):
            result = self.downloader(opener).fetch(spec)
        self.assertEqual(result["sha256"], spec["sha256"])
        self.assertEqual((self.root / "voices/female.wav").read_bytes(), b"fixture voice")
        opener.assert_not_called()

    def test_bundle_corruption_fails_before_model_network_request(self):
        spec = artifact(b"reference", "voices/female.wav", source="bundled", source_file="absent.wav")
        opener = mock.Mock(side_effect=AssertionError("network called"))
        with mock.patch.object(fetch, "ROOT", self.root):
            with self.assertRaisesRegex(fetch.FetchError, "Bundled reference missing"):
                fetch.run("portable", self.root, catalog=self.catalog([self.spec, spec]),
                          opener=opener, output=self.messages.append)
        opener.assert_not_called()

    def test_404_is_actionable_and_not_retried(self):
        error = urllib.error.HTTPError("https://huggingface.co/test", 404, "Not Found", {}, None)
        opener = mock.Mock(side_effect=error)
        with self.assertRaisesRegex(fetch.FetchError, "pinned repository/revision"):
            self.downloader(opener, retries=3).fetch(self.spec)
        opener.assert_called_once()

    def test_path_traversal_and_symlink_rejected(self):
        for unsafe in ("../outside", "/tmp/outside", "dir/../../outside", "C:/outside", "dir\\outside"):
            with self.subTest(unsafe=unsafe), self.assertRaises(fetch.FetchError):
                fetch.safe_path(self.root, unsafe)
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable without platform privilege")
            with self.assertRaisesRegex(fetch.FetchError, "symlink"):
                fetch.safe_path(self.root, "link/model.bin")

    def test_lock_refuses_concurrent_run_and_preserves_original_lock(self):
        (self.root / fetch.LOCK).write_text("existing owner")
        with self.assertRaisesRegex(fetch.FetchError, "Another downloader"):
            fetch.run("portable", self.root, catalog=self.catalog(), output=self.messages.append)
        self.assertEqual((self.root / fetch.LOCK).read_text(), "existing owner")

    def test_existing_model_symlink_is_actionable_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "model.bin"
            external.write_bytes(b"external approved model")
            link = self.root / "model.bin"
            try:
                link.symlink_to(external)
            except OSError:
                self.skipTest("symlinks unavailable without platform privilege")
            original_target = link.readlink()
            catalog = self.catalog()
            opener = mock.Mock(side_effect=AssertionError("network called"))
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run), self.assertRaisesRegex(
                    fetch.FetchError, r"--model-dir REAL_DIRECTORY.*physical, mounted"
                ):
                    fetch.run("portable", self.root, dry_run=dry_run, catalog=catalog,
                              opener=opener, output=self.messages.append)
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.readlink(), original_target)
                self.assertEqual(external.read_bytes(), b"external approved model")
                self.assertFalse((self.root / fetch.MANIFEST).exists())
                self.assertFalse((self.root / fetch.LOCK).exists())
                self.assertFalse((self.root / "model.bin.part").exists())
            opener.assert_not_called()

    def test_catalog_requires_pinned_digest_and_revision(self):
        for change in ({"sha256": ""}, {"revision": "main"}, {"bytes": -1}):
            with self.subTest(change=change), self.assertRaises(fetch.FetchError):
                fetch.load_catalog(self.catalog([{**self.spec, **change}]))

    def test_manifest_keeps_previously_downloaded_other_profile_artifacts(self):
        (self.root / fetch.MANIFEST).write_text(json.dumps({
            "schema_version": 1, "artifacts": [{"file": "previous-model", "sha256": "historical"}],
        }))
        fetch.run("portable", self.root, catalog=self.catalog(),
                  opener=lambda *a, **k: Response(b"abcdefghij"), output=self.messages.append)
        data = json.loads((self.root / fetch.MANIFEST).read_text())
        self.assertEqual({r["file"] for r in data["artifacts"]}, {"previous-model", "model.bin"})


if __name__ == "__main__":
    unittest.main()
