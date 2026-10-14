"""Offline baseline tests; no model/runtime imports or private-file reads."""

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("runtime_manifest", Path(__file__).resolve().parents[1] / "scripts/runtime_manifest.py")
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)
SECRET = "NEVER_EXPOSE_REAL_PERSON_OR_API_SECRET_329187"
CONFIG_SOURCE = '''
raise RuntimeError("source must never be imported")
class Settings:
    stt_backend: str = "faster-whisper"
    llm_model: str = "qwen3-4b"
    llm_model_path: str = "models/qwen3-4b-q4.gguf"
    llm_remote: bool = False
    llm_context_tokens: int = Field(default=8192, ge=1)
    llm_reply_tokens: int = 256
    whisper_model: str = "models/whisper-small"
    piper_model: str = "models/ru_RU-denis-medium.onnx"
    natural_tts_enabled: bool = False
    voice_reference_dir: str = "app/assets/voices"
    tts_cache_max_bytes: int = Field(default=8 * 1024 * 1024, ge=0)
'''


class RuntimeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "app").mkdir()
        (self.root / "app/config.py").write_text(CONFIG_SOURCE)
        self.environment = mock.patch.object(baseline, "collect_environment", return_value={"python": "3.12.0", "packages": {"numpy": "2.0"}})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def collect(self, config=None, verify=False):
        return baseline.collect_runtime_manifest(self.root, config={} if config is None else config, verify_models=verify)

    def add_model(self, body=b"tiny fake GGUF"):
        model = self.root / "models/qwen3-4b-q4.gguf"
        model.parent.mkdir(exist_ok=True)
        model.write_bytes(body)
        record = {"file": "qwen3-4b-q4.gguf", "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF",
                  "revision": "a" * 40, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                  "private_metadata": SECRET}
        (self.root / "model-manifest.json").write_text(json.dumps([record]))
        return model

    def test_digest_is_deterministic_and_excludes_capture_time(self):
        first, second = self.collect(), self.collect()
        self.assertEqual(first["semantic_sha256"], second["semantic_sha256"])
        self.assertNotEqual(first["captured_at"], second["captured_at"])
        self.assertEqual(first["configuration"]["llm_context_tokens"], 8192)
        self.assertEqual(first["configuration"]["tts_cache_max_bytes"], 8 * 1024 * 1024)

    def test_poisoned_private_files_and_config_values_are_not_read_or_exposed(self):
        forbidden = [self.root / ".env", self.root / "app/uploads/patient.py",
                     self.root / "app/private/credentials.py", self.root / "tests/fixtures/patient.json"]
        for path in forbidden:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SECRET)
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path in forbidden:
                raise AssertionError("private file was opened")
            return real_open(path, *args, **kwargs)
        config = {"ADMIN_PASSWORD": SECRET, "LLM_API_KEY": SECRET, "LLM_URL": "https://" + SECRET,
                  "LLM_MODEL": SECRET, "PIPER_MODEL": str(self.root / SECRET / "voice.onnx"),
                  "NATURAL_TTS_VOICE": {"poison": SECRET}, "LLM_CONTEXT_TOKENS": SECRET}
        with mock.patch.object(Path, "open", guarded_open):
            manifest = self.collect(config)
        rendered = json.dumps(manifest)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("ADMIN_PASSWORD", rendered)
        self.assertEqual(manifest["configuration"]["llm_model"], "custom-value-redacted")

    def test_only_allowlisted_process_environment_is_used_without_dotenv(self):
        (self.root / ".env").write_text("LLM_CONTEXT_TOKENS=12345\nLLM_API_KEY=" + SECRET)
        with mock.patch.dict(os.environ, {"LLM_CONTEXT_TOKENS": "4096", "LLM_API_KEY": SECRET}, clear=True):
            result = baseline.collect_runtime_manifest(self.root)
        self.assertEqual(result["configuration"]["llm_context_tokens"], 4096)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn("no-dotenv", result["configuration_input"])

    def test_source_covers_modified_deleted_and_untracked_files(self):
        def git(*args):
            result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=False)
            if result.returncode:
                self.skipTest("git unavailable for isolated fixture")
        git("init", "-q")
        (self.root / "app/deleted.py").write_text("one=1\n")
        git("add", "app")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        before = self.collect()
        (self.root / "app/deleted.py").unlink()
        with (self.root / "app/config.py").open("a") as stream:
            stream.write("# modified source\n")
        (self.root / "app/new_module.py").write_text("result=2\n")
        brand = self.root / "app/static/brand/tokens.css"
        brand.parent.mkdir(parents=True)
        brand.write_text(":root { --gap: 4px; }")
        result = self.collect()
        rows = {r["file"]: r for r in result["source"]["files"]}
        self.assertEqual(rows["app/config.py"]["change"], "modified")
        self.assertEqual(rows["app/deleted.py"]["change"], "deleted")
        self.assertEqual(rows["app/new_module.py"]["change"], "added")
        self.assertIn("app/static/brand/tokens.css", rows)
        self.assertTrue(result["source"]["dirty_scoped"])
        self.assertNotEqual(before["semantic_sha256"], result["semantic_sha256"])
        self.assertEqual(before["source"]["head_commit"], result["source"]["head_commit"])

    def test_models_default_to_declared_unverified_without_reading_weights(self):
        model = self.add_model()
        real_hash = baseline.file_hash
        def guarded_hash(path, **kwargs):
            if path == model:
                raise AssertionError("model weights hashed without explicit opt-in")
            return real_hash(path, **kwargs)
        with mock.patch.object(baseline, "file_hash", guarded_hash):
            result = self.collect()
        entry = next(m for m in result["models"] if m.get("artifact") == model.name)
        self.assertFalse(entry["verified"])
        self.assertEqual(entry["verification"], "not_verified")
        self.assertIn("declared-snapshot", entry["provenance"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_explicit_verification_detects_corrupt_weights(self):
        model = self.add_model()
        good = self.collect(verify=True)
        entry = next(m for m in good["models"] if m.get("artifact") == model.name)
        self.assertTrue(entry["verified"])
        model.write_bytes(b"wrong bytes")
        bad = self.collect(verify=True)
        entry = next(m for m in bad["models"] if m.get("artifact") == model.name)
        self.assertFalse(entry["verified"])
        self.assertEqual(entry["verification"], "mismatch")
        self.assertNotEqual(good["semantic_sha256"], bad["semantic_sha256"])

    def test_model_symlink_to_private_file_is_never_read(self):
        model = self.add_model()
        secret = self.root / ".env"
        secret.write_text(SECRET)
        model.unlink()
        try:
            model.symlink_to(secret)
        except OSError:
            self.skipTest("symlink support unavailable")
        real_hash = baseline.file_hash
        def guarded_hash(path, **kwargs):
            self.assertNotEqual(path, secret)
            return real_hash(path, **kwargs)
        with mock.patch.object(baseline, "file_hash", guarded_hash):
            result = self.collect(verify=True)
        entry = next(m for m in result["models"] if m.get("artifact") == model.name)
        self.assertEqual(entry["verification"], "path_not_approved")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_source_symlink_is_not_followed(self):
        secret = self.root / ".env"
        secret.write_text(SECRET)
        try:
            (self.root / "app/external.py").symlink_to(secret)
        except OSError:
            self.skipTest("symlink support unavailable")
        result = self.collect()
        record = next(r for r in result["source"]["files"] if r["file"] == "app/external.py")
        self.assertEqual(record["status"], "symlink_not_read")
        self.assertFalse(result["source"]["fingerprints_complete"])

    def test_approved_external_model_symlink_can_be_verified_without_modification(self):
        model = self.add_model()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
            content = model.read_bytes()
            target.write_bytes(content)
            model.unlink()
            try:
                model.symlink_to(target)
            except OSError:
                self.skipTest("symlink support unavailable")
            result = self.collect(verify=True)
            entry = next(m for m in result["models"] if m.get("artifact") == model.name)
            self.assertTrue(entry["verified"])
            self.assertTrue(model.is_symlink())
            self.assertEqual(target.read_bytes(), content)
            self.assertNotIn(outside, json.dumps(result))

    def test_approved_voice_refs_are_hashed_without_content_or_arbitrary_assets(self):
        folder = self.root / "app/assets/voices"
        folder.mkdir(parents=True)
        for filename in ("male.wav", "female.wav", "metadata.json", "private_recording.wav"):
            (folder / filename).write_text(SECRET)
        result = self.collect()
        refs = result["voice_references"]
        self.assertEqual(len(refs["bundled_files"]), 3)
        self.assertTrue(all(r["status"] == "hashed" for r in refs["bundled_files"]))
        self.assertEqual(refs["selected_reference_source"], "bundled")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("private_recording", json.dumps(result))

    def test_remote_llm_is_not_claimed_verified(self):
        result = self.collect({"LLM_REMOTE": "true", "LLM_URL": "https://" + SECRET})
        self.assertIn({"component": "llm_model_path", "verification": "remote_runtime_not_observed", "verified": False}, result["models"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_worker_role_does_not_declare_app_speech_models(self):
        result = baseline.collect_runtime_manifest(self.root, config={}, role="llm")
        self.assertEqual(result["role"], "llm")
        self.assertEqual({m["component"] for m in result["models"]}, {"llm_model_path"})

    def test_writer_removes_own_partial_on_serialization_failure(self):
        destination = self.root / "invalid.json"
        with self.assertRaises(ValueError):
            baseline.write_runtime_manifest(destination, {"invalid": float("nan")})
        self.assertFalse(destination.exists())

    def test_writer_creates_new_manifest_and_never_overwrites(self):
        destination = self.root / "reports/baseline.json"
        manifest = self.collect()
        baseline.write_runtime_manifest(destination, manifest)
        self.assertEqual(json.loads(destination.read_text()), manifest)
        before = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            baseline.write_runtime_manifest(destination, {"replacement": True})
        self.assertEqual(destination.read_bytes(), before)
        with self.assertRaises(ValueError):
            baseline.write_runtime_manifest(self.root / ".env", {})


if __name__ == "__main__":
    unittest.main()
