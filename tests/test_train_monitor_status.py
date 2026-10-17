import os
import time
import unittest
from unittest import mock

from train_monitor import server


class TrainMonitorStatusTests(unittest.TestCase):
    def test_gui_api_failure_is_non_blocking_warning(self):
        with mock.patch.object(server, "newest_preview_images", return_value=[]), \
                mock.patch.object(server, "_training_output_dir", return_value=None), \
                mock.patch.object(server, "latest_training_config", return_value={}), \
                mock.patch.object(server, "build_model_outputs", return_value={}), \
                mock.patch.object(server, "_extract_train_params", return_value=[]), \
                mock.patch.object(server, "tensorboard_loss_scalars", return_value=[{"tag": "loss/average"}]), \
                mock.patch.object(server, "gpu_info", return_value={}), \
                mock.patch.object(server, "fetch_gui_json", side_effect=OSError("HTTP Error 404: Not Found")):
            status = server.collect_status()

        self.assertNotIn("error", status)
        self.assertIn("gui_warning", status)
        self.assertEqual(status["state"], "GUI 离线")
        self.assertEqual(status["tensorboard_loss"], [{"tag": "loss/average"}])

    def test_queued_config_does_not_replace_running_task_config(self):
        from contextlib import ExitStack
        running = {"id": "running", "status": "RUNNING", "config_file": "first.toml"}
        queued = {"id": "queued", "status": "QUEUED", "config_file": "second.toml"}
        config = {"output_dir": "first-output", "output_name": "first"}
        with ExitStack() as stack:
            for name, value in {
                "newest_preview_images": [], "_training_output_dir": None,
                "build_model_outputs": {}, "_extract_train_params": [],
                "tensorboard_loss_scalars": [], "gpu_info": {}, "newest_files": [],
                "gpu_memory_used_mb": None,
            }.items():
                stack.enter_context(mock.patch.object(server, name, return_value=value))
            load_config = stack.enter_context(mock.patch.object(server, "latest_training_config", return_value=config))
            stack.enter_context(mock.patch.object(server, "fetch_gui_json", return_value=(
                {"data": {"tasks": [running, queued]}}, "http://gui")))
            stack.enter_context(mock.patch.object(server, "fetch_json", return_value={"data": {"lines": []}}))
            infer = stack.enter_context(mock.patch.object(server, "infer_model_type", return_value="Anima LoRA"))
            status = server.collect_status()
            self.assertEqual(status["active_task"], running)
            load_config.assert_called_once_with("first.toml")
            server.newest_preview_images.assert_called_once_with(config=config)
            infer.assert_called_once_with([], "first.toml")

    def test_explicit_config_selection_ignores_newer_autosave(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            autosave = root / "config/autosave"
            autosave.mkdir(parents=True)
            first = autosave / "first.toml"
            second = autosave / "second.toml"
            first.write_text('output_dir = "first-output"\n', encoding="utf-8")
            second.write_text('output_dir = "second-output"\n', encoding="utf-8")
            os.utime(second, (time.time() + 10, time.time() + 10))
            with mock.patch.object(server, "REPO", root):
                self.assertEqual(server.latest_training_config()["output_dir"], "second-output")
                self.assertEqual(server.latest_training_config(str(first))["output_dir"], "first-output")

    def test_infer_model_type_anima_finetune_from_script(self):
        lines = [
            "accelerate launch scripts/dev/anima_train.py --config_file config/autosave/foo.toml",
            "INFO dit device: cuda:0",
        ]
        self.assertEqual(server.infer_model_type(lines), "Anima Finetune")

    def test_infer_model_type_anima_finetune_from_config(self):
        autosave = server.REPO / "config/autosave"
        autosave.mkdir(parents=True, exist_ok=True)
        cfg = autosave / "_unit_test_anima_finetune_monitor.toml"
        try:
            cfg.write_text('model_train_type = "anima-finetune"\n', encoding="utf-8")
            future = time.time() + 7200
            os.utime(cfg, (future, future))
            self.assertEqual(server.infer_model_type(["starting training"]), "Anima Finetune")
        finally:
            cfg.unlink(missing_ok=True)

    def test_infer_model_type_anima_lora_network(self):
        lines = ["python vendor/sd-scripts/anima_train_network.py --config_file x.toml"]
        self.assertEqual(server.infer_model_type(lines), "Anima LoRA")


if __name__ == "__main__":
    unittest.main()
