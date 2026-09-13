import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("amd_probe", Path(__file__).parents[1] / "tools/amd_voice_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class AMDProbeTests(unittest.TestCase):
    def test_windows10_stops_before_download(self):
        self.assertEqual(probe.eligibility({"os": "Windows", "build": "10.0.19045", "gpus": [{"Name": "AMD Radeon"}]}), "WINDOWS_11_REQUIRED")

    def test_nvidia_is_not_amd_acceptance(self):
        self.assertEqual(probe.eligibility({"os": "Windows", "build": "10.0.26100", "gpus": [{"Name": "NVIDIA RTX 3080"}]}), "AMD_GPU_NOT_DETECTED")

    def test_amd_preflight_allows_operator_test(self):
        self.assertIsNone(probe.eligibility({"os": "Windows", "build": "10.0.26100", "gpus": [{"Name": "AMD Radeon RX 9070 XT"}]}))

    def test_failure_never_exports_message(self):
        try:
            raise RuntimeError("HIP out of memory C:/Users/privateperson/sk-secret")
        except RuntimeError as exc:
            value = probe.failure(exc)
        self.assertEqual(value["category"], "gpu_memory")
        self.assertNotIn("sk-secret", json.dumps(value))
        self.assertNotIn("privateperson", json.dumps(value))

    def test_export_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("voice-config.json", "local-worker-0.log", "reference.wav", "worker-0.json"):
                (root / name).write_text("private-secret")
            (root / "voice-1.wav").write_bytes(b"test-generated-audio")
            result = probe.export_report(root, {"status": "failed"})
            with zipfile.ZipFile(result) as z:
                self.assertEqual(set(z.namelist()), {"diagnostic.json", "test-texts.json", "voice-1.wav"})
                self.assertTrue(all(b"private-secret" not in z.read(n) for n in z.namelist()))

    def test_failure_always_exports_without_install(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, "ROOT", Path(tmp)), \
             patch.object(probe, "hardware", return_value={"os": "Windows", "build": "10.0.19045", "gpus": []}), \
             patch.object(probe, "install_runtime") as install, patch("sys.argv", ["probe"]):
            self.assertEqual(probe.main(), 2)
            install.assert_not_called()
            self.assertEqual(len(list(Path(tmp).rglob("AMD-voice-diagnostic.zip"))), 1)

    def test_installer_cannot_modify_system_python(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, "ROOT", Path(tmp)):
            with self.assertRaisesRegex(RuntimeError, "BUNDLED_PYTHON_REQUIRED"):
                probe.install_runtime(Path(tmp))

    def test_installer_uses_python_isolation_not_just_pip_isolation(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, "ROOT", Path(tmp)), \
             patch.object(probe.sys, "executable", str(Path(tmp) / "python/python.exe")), \
             patch.object(probe.shutil, "disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)), \
             patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            probe.install_runtime(Path(tmp))
            assert run.call_count == 2
            assert all(call.args[0][1:4] == ["-I", "-m", "pip"] for call in run.call_args_list)

    def test_gpu_crash_does_not_pass_even_with_completed_receipt(self):
        from types import SimpleNamespace
        def crashed(command, **kwargs):
            output = Path(command[command.index("--output") + 1])
            (output / "result-0.json").write_text('{"status":"completed","phase":"voice"}')
            return SimpleNamespace(returncode=-1073741819)
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, "ROOT", Path(tmp)), \
             patch.object(probe, "hardware", return_value={"os": "Windows", "build": "10.0.26100", "gpus": [{"Name": "AMD Radeon"}]}), \
             patch.object(probe, "make_request", return_value={}), patch.object(probe, "install_runtime"), \
             patch.object(probe.subprocess, "run", side_effect=crashed), patch("sys.argv", ["probe"]):
            self.assertEqual(probe.main(), 2)
            report = json.loads(next(Path(tmp).rglob("diagnostic.json")).read_text())
            self.assertEqual(report["tests"][0]["status"], "process_failed")

    def test_config_does_not_take_user_text_or_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime").mkdir()
            (root / "model").mkdir()
            (root / "ref.wav").touch()
            config = root / "config.json"
            config.write_text(json.dumps({"settings": {"provider": "breeze_tts2", "runtime_root": "runtime",
                "model_dir": "model", "reference_audio": "ref.wav", "reference_text": "reference",
                "provider_options": {"adapter_dir": "adapter", "device": "cpu", "text": "private"}}}))
            from types import SimpleNamespace
            with patch.dict("sys.modules", {"breeze_adapter": SimpleNamespace(adapter_metadata=lambda p: {"sha256": "test"})}):
                result = probe.make_request(config, probe.TEXTS[0])
            self.assertEqual(result["device"], "cuda")
            self.assertEqual(result["text"], probe.TEXTS[0])
            self.assertEqual(Path(result["model_dir"]), root / "model")


if __name__ == "__main__":
    unittest.main()
