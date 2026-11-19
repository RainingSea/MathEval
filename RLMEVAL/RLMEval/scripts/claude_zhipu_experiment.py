from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLAUDE = "/root/.nvm/versions/node/v24.22.0/bin/claude"


def load_token(path: Path, section_name: str | None = None) -> str:
    pattern = r"^##\s*智谱[^\n]*\n(.*?)(?=^##|\Z)" if section_name is None else rf"^##[ \t]*{re.escape(section_name)}[ \t]*\n(.*?)(?=^##|\Z)"
    section = re.search(pattern, path.read_text(), re.M | re.S)
    if section is None:
        raise ValueError("secret.txt lacks a 智谱 section")
    lines = [line.strip() for line in section.group(1).splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise ValueError("The 智谱 section must contain one token or token assignment")
    token = re.sub(r"^(?:export\s+)?(?:ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY)\s*=\s*", "", lines[0]).strip("\"'")
    if not token or any(character.isspace() for character in token):
        raise ValueError("Invalid 智谱 token format")
    return token


def build_env(token: str, model: str, config_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
        "ANTHROPIC_MODEL": model,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "131072"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "API_TIMEOUT_MS": "3000000",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    })
    for slot in ("HAIKU", "SONNET", "OPUS", "FABLE"):
        slot_model = "glm-4.7" if slot == "HAIKU" else model
        env[f"ANTHROPIC_DEFAULT_{slot}_MODEL"] = slot_model
        env[f"ANTHROPIC_DEFAULT_{slot}_MODEL_NAME"] = slot_model
    return env


def smoke_test(claude_bin: str, env: dict[str, str], timeout: int, cwd: Path) -> bool:
    command = [claude_bin, "-p", "--output-format", "stream-json", "--verbose",
               "--model", env["ANTHROPIC_MODEL"], "--tools", "", "--max-turns", "1",
               "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    try:
        result = subprocess.run(command, input="Reply with the single word: ok", text=True,
                                capture_output=True, env=env, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"CC smoke test timed out after {timeout}s; no benchmark was started.", flush=True)
        return False
    successful = False
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            print(json.dumps({key: event.get(key) for key in ("type", "subtype", "is_error", "result", "errors")}, ensure_ascii=False).replace(env["ANTHROPIC_AUTH_TOKEN"], "<redacted>"), flush=True)
            successful = not event.get("is_error", False) and event.get("subtype") == "success"
    if result.returncode or not successful:
        print("CC smoke test failed; no benchmark was started.", flush=True)
        print(result.stderr[-2000:].replace(env["ANTHROPIC_AUTH_TOKEN"], "<redacted>"), flush=True)
    return result.returncode == 0 and successful


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code × Zhipu: connectivity test by default; --run starts the rerun list only after a successful test.")
    parser.add_argument("--secret-file", type=Path, default=ROOT / "secret.txt")
    parser.add_argument("--secret-section", help="Exact credential section heading, without ##")
    parser.add_argument("--model", default="glm-5.2[1m]")
    parser.add_argument("--claude-bin", default=DEFAULT_CLAUDE)
    parser.add_argument("--smoke-timeout", type=int, default=60)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--indices")
    parser.add_argument("--reverse-order", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=43200)
    args = parser.parse_args()
    token = load_token(args.secret_file, args.secret_section)
    with tempfile.TemporaryDirectory(prefix="rlm-zhipu-") as temporary:
        config_dir = Path(temporary) / "config"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
        env = build_env(token, args.model, config_dir)
        print(f"Testing CC → {env['ANTHROPIC_BASE_URL']} model={args.model} (credentials hidden)", flush=True)
        if not smoke_test(args.claude_bin, env, args.smoke_timeout, Path(temporary)):
            return 1
        if not args.run:
            print("CC connectivity OK. Use --run to start benchmark generation.", flush=True)
            return 0
        indices = args.indices
        if indices is None:
            match = re.search(r"^INDICES=([\d,]+)\s*$", (ROOT / "run-logs/pfr-rerun-list.md").read_text(), re.M)
            if match is None:
                raise ValueError("Missing INDICES in rerun list")
            indices = match.group(1)
        run_id = args.run_id or f"claude-zhipu-pfr-rerun-{datetime.now():%Y%m%d-%H%M%S}"
        if not re.fullmatch(r"[\w.-]+", run_id) or run_id in {".", ".."}:
            raise ValueError("Invalid run ID")
        if (ROOT / "artifacts" / run_id).exists():
            raise ValueError("Run ID already exists")
        env["ELAN_HOME"] = str(ROOT.parent / "elan")
        env["PATH"] = f"{env['ELAN_HOME']}/bin:{env.get('PATH', '')}"
        env["HOME"] = str(ROOT.parent / "rlm-env/home")
        env["XDG_CACHE_HOME"] = str(Path(env["HOME"]) / ".cache")
        command = [str(ROOT / "rlmv-eval"), "run-claude", "--model", args.model,
                   "--indices", indices, "--samples", "1", "--concurrency", str(args.concurrency),
                   "--timeout-seconds", str(args.timeout_seconds), "--max-compile-attempts", "0",
                   "--task", "proof", "--run-id", run_id, "--claude-bin", args.claude_bin,
                   "--anthropic-base-url", env["ANTHROPIC_BASE_URL"],
                   "--anthropic-auth-token-env", "ANTHROPIC_AUTH_TOKEN", "--elan-home", env["ELAN_HOME"]]
        if args.reverse_order:
            command.append("--reverse-order")
        print(f"Starting run_id={run_id} indices={indices} concurrency={args.concurrency}", flush=True)
        status = subprocess.run(command, cwd=ROOT, env=env).returncode
        if status:
            return status
        return subprocess.run([str(ROOT / "rlmv-eval"), "score", "--input", f"artifacts/{run_id}/generations.jsonl",
                               "--output", f"artifacts/{run_id}/scores.jsonl", "--task", "proof"], cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
