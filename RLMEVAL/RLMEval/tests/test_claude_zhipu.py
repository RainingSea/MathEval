import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/claude_zhipu_experiment.py"
SPEC = importlib.util.spec_from_file_location("claude_zhipu_experiment", SCRIPT)
zhipu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(zhipu)


class TestClaudeZhipu(unittest.TestCase):
    def test_select_second_account_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret.txt"
            secret_file.write_text("## 智谱API KEY\nfirst-token\n## 智谱API KEY （第二个）\nsecond-token\n")
            self.assertEqual(zhipu.load_token(secret_file), "first-token")
            self.assertEqual(zhipu.load_token(secret_file, "智谱API KEY （第二个）"), "second-token")
            with self.assertRaises(ValueError):
                zhipu.load_token(secret_file, "missing")

    def test_load_token_only_from_zhipu_section(self):
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret.txt"
            secret_file.write_text("## 反代平台\nexport OPENAI_API_KEY='other'\n## 智谱API KEY\ntest-token\n")
            self.assertEqual(zhipu.load_token(secret_file), "test-token")
            secret_file.write_text("## 智谱\nexport ANTHROPIC_AUTH_TOKEN='test-token'\n")
            self.assertEqual(zhipu.load_token(secret_file), "test-token")

    def test_missing_or_ambiguous_credentials_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret.txt"
            for content in ("## Other\nother-token\n", "## 智谱\none\ntwo\n", "## 智谱\n"):
                secret_file.write_text(content)
                with self.assertRaises(ValueError):
                    zhipu.load_token(secret_file)

    def test_env_overrides_stale_credentials_and_models(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "old", "ANTHROPIC_MODEL": "old"}):
            env = zhipu.build_env("test-token", "glm-5.2[1m]", Path("/tmp/config"))
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-token")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(env["ANTHROPIC_MODEL"], "glm-5.2[1m]")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "glm-4.7")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "glm-5.2[1m]")
        self.assertEqual(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "131072")

    def test_smoke_requires_success_result(self):
        env = zhipu.build_env("test-token", "glm-5.2[1m]", Path("/tmp/config"))
        for subtype, is_error, expected in (("success", False, True), ("error_during_execution", True, False)):
            result = subprocess.CompletedProcess([], 0, json.dumps({"type": "result", "subtype": subtype, "is_error": is_error}), "")
            with patch.object(zhipu.subprocess, "run", return_value=result), patch("builtins.print"):
                self.assertEqual(zhipu.smoke_test("claude", env, 10, Path("/tmp")), expected)

    def test_smoke_timeout_is_failure(self):
        env = zhipu.build_env("test-token", "glm-5.2[1m]", Path("/tmp/config"))
        with patch.object(zhipu.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 10)), patch("builtins.print"):
            self.assertFalse(zhipu.smoke_test("claude", env, 10, Path("/tmp")))
