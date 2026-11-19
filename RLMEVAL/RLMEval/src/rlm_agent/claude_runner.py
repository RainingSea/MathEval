"""Claude Code agent track: same ProofNet-Verified protocol as the codex track,
with Claude Code CLI as the harness backend instead of codex.

Mirrors runner.run_codex / runner._run_one:
- identical workspace preparation (TASK.md / Context.lean / Candidate.lean /
  check_candidate.sh) and identical agent prompts, so both baselines see the
  same task contract;
- claude runs headless (`claude -p`), reading the agent prompt on stdin, cwd =
  workspace; tools are gated by an explicit allowlist (file tools + the
  protocol's check script) instead of a sandbox, which also keeps network-ish
  commands denied per TASK.md;
- the Anthropic protocol spoken by Claude Code is served by the local a2r
  adapter (apiAdapt/a2r.py), which relays to the same upstream as the c2r
  route, so the model under test stays identical (e.g. deepseek-v4-pro-0813);
- stdout is `--output-format stream-json` (one JSON object per line) and is
  stored as .events.jsonl exactly like the codex track; the final
  `{"type":"result"}` line provides agent_final_message;
- raw_text is the post-run Candidate.lean content (same source of truth as
  codex), compile_attempts from .compile_count.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .items import RlmItem
from .runner import (
    AGENT_PROMPT_PROOF,
    AGENT_PROMPT_STATEMENT,
    CLAUDE_RECEIPT,
    MAX_CANDIDATE_BYTES,
    MAX_STDOUT_BYTES,
    _resolve_lean_env,
    build_masked_command,
    prepare_workspace,
    save_generation,
    utc_now,
)

# 协议所需最小工具集:文件读写 + check_candidate.sh + 只读命令。
# 白名单之外的一切(含网络类命令)在 headless 模式下自动拒绝——正好落实
# TASK.md 的 "do not use the network"。
CLAUDE_ALLOWED_TOOLS = (
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash(./check_candidate.sh:*)",
    "Bash(check_candidate.sh:*)",
    "Bash(bash ./check_candidate.sh:*)",
    "Bash(sh ./check_candidate.sh:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(grep:*)",
    "Bash(rg:*)",
    "Bash(sed -n:*)",
)
CLAUDE_DISALLOWED_TOOLS = ("WebSearch", "WebFetch", "WebFetchTool", "TodoWrite", "Task", "Agent")


@dataclass
class ClaudeRunConfig:
    model: str
    claude_bin: str
    timeout_seconds: int
    max_compile_attempts: int
    reasoning_effort: str | None
    artifacts_dir: Path
    workspaces_dir: Path
    anthropic_base_url: str
    concurrency: int = 1
    samples: int = 1
    task: str = "statement"
    api_key: str = "dummy-local-a2r"
    auth_token: str = ""
    remove_workspaces: bool = False


async def _run_one_claude(
    item: RlmItem,
    sample_id: int,
    run_id: str,
    config: ClaudeRunConfig,
    generations_path: Path,
    cache_home: Path,
    elan_home: Path,
    write_lock: asyncio.Lock,
    semaphore: asyncio.Semaphore,
) -> None:
    try:
        lean_bin, lean_path = _resolve_lean_env(item.project_dir, elan_home)
    except (OSError, FileNotFoundError):
        lean_bin, lean_path = "", ""
    workspace = prepare_workspace(
        item, run_id=run_id, config=config, cache_home=cache_home, elan_home=elan_home, lean_bin=lean_bin, lean_path=lean_path,
        receipt=CLAUDE_RECEIPT,
    )
    agent_prompt = (
        AGENT_PROMPT_PROOF if config.task in {"proof", "proof-normal"} else AGENT_PROMPT_STATEMENT
    )
    agent_prompt += " " + CLAUDE_RECEIPT
    claude_config_dir = workspace / ".claude-config"
    claude_config_dir.mkdir(exist_ok=True)

    command = [
        config.claude_bin,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        config.model,
        "--allowedTools",
        *CLAUDE_ALLOWED_TOOLS,
        "--disallowedTools",
        *CLAUDE_DISALLOWED_TOOLS,
    ]
    # 注意:claude CLI 无 --reasoning-effort flag;推理档由 a2r 侧注入
    # (A2R_REASONING_EFFORT,默认 high),config.reasoning_effort 仅作记录。

    # 隔离强化(2026-10-31):白名单只限工具种类不限路径,vault 仍是 755 可读;
    # 用 mount namespace 把答案区对 agent 子进程树整个遮蔽(含 #eval 文件读)。
    command = build_masked_command(command)

    env = os.environ.copy()
    env["ELAN_HOME"] = str(elan_home)
    env["XDG_CACHE_HOME"] = str(cache_home)
    env["ANTHROPIC_BASE_URL"] = config.anthropic_base_url
    env["ANTHROPIC_API_KEY"] = "" if config.auth_token else config.api_key
    env["ANTHROPIC_AUTH_TOKEN"] = config.auth_token
    env["ANTHROPIC_MODEL"] = config.model
    for _slot in ("SONNET", "HAIKU", "OPUS", "FABLE"):
        slot_model = env.get(f"ANTHROPIC_DEFAULT_{_slot}_MODEL", config.model) if config.auth_token else config.model
        env[f"ANTHROPIC_DEFAULT_{_slot}_MODEL"] = slot_model
        env[f"ANTHROPIC_DEFAULT_{_slot}_MODEL_NAME"] = slot_model
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    # 端点分流:本地 a2r(127.0.0.1)清代理防串到 3128;远程 Anthropic 兼容端点
    # (如 lhcodex 反代直连)是外网域名,必须保留环境代理才出得了内网。
    if config.anthropic_base_url.startswith(("http://127.0.0.1", "http://localhost")):
        env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
        env["http_proxy"] = env["https_proxy"] = env["HTTP_PROXY"] = env["HTTPS_PROXY"] = ""

    started_at = utc_now()
    started = time.monotonic()
    async with semaphore:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(workspace),
            start_new_session=True,
        )
        try:
            communicate = process.communicate(agent_prompt.encode("utf-8"))
            if config.timeout_seconds > 0:
                stdout, stderr = await asyncio.wait_for(communicate, timeout=config.timeout_seconds)
            else:
                stdout, stderr = await communicate
            termination = None
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(process.pid), 15)
            except (ProcessLookupError, PermissionError):
                process.kill()
            stdout, stderr = await process.communicate()
            termination = "timeout"
    wall_ms = int((time.monotonic() - started) * 1000)
    status = (
        "agent_timeout"
        if termination == "timeout"
        else ("ok" if process.returncode == 0 else "agent_error")
    )
    logs_dir = generations_path.parent.resolve() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    (logs_dir / f"{token}.events.jsonl").write_bytes(stdout[:MAX_STDOUT_BYTES])
    (logs_dir / f"{token}.stderr.log").write_bytes(stderr[-65536:])

    try:
        raw_text = (workspace / "Candidate.lean").read_text(encoding="utf-8")
        if len(raw_text.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raw_text = ""
            output_truncated = True
        else:
            output_truncated = False
    except (OSError, UnicodeError):
        raw_text = ""
        output_truncated = True
    compile_attempts = 0
    try:
        compile_attempts = int((workspace / ".compile_count").read_text().strip() or 0)
    except (OSError, ValueError):
        pass

    # stream-json:最后一行 {"type":"result","result":...} 为最终答复
    final_message = None
    for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip().startswith("{"):
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            final_message = event.get("result")
            if event.get("is_error"):
                status = "agent_error" if status == "ok" else status

    record = {
        "run_id": run_id,
        "track": "claude_agent",
        "item_index": item.index,
        "item_name": item.label,
        "item_repo": item.repo,
        "sample_id": sample_id,
        "model": config.model,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "termination_reason": termination,
        "started_at": started_at,
        "finished_at": utc_now(),
        "wall_ms": wall_ms,
        "workspace": str(workspace),
        "events_path": str(logs_dir / f"{token}.events.jsonl"),
        "compile_attempts": compile_attempts,
        "output_truncated": output_truncated,
        "raw_text": raw_text,
        "agent_final_message": final_message,
    }
    await save_generation(record, generations_path, write_lock, config.remove_workspaces)


async def run_claude(
    items: list[RlmItem],
    run_id: str,
    config: ClaudeRunConfig,
    cache_home: Path,
    elan_home: Path,
) -> Path:
    if not items or min(config.samples, config.concurrency) < 1:
        raise ValueError("Select at least one item and use positive concurrency and sample counts")
    generations_path = config.artifacts_dir.resolve() / run_id / "generations.jsonl"
    generations_path.parent.mkdir(parents=True, exist_ok=True)
    if generations_path.exists():
        raise FileExistsError(f"{generations_path} already exists; choose a new --run-id")
    write_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(config.concurrency)
    await asyncio.gather(
        *(
            _run_one_claude(item, sample_id, run_id, config, generations_path, cache_home, elan_home, write_lock, semaphore)
            for item in items
            for sample_id in range(config.samples)
        )
    )
    return generations_path
