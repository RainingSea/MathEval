from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
import shutil
import signal
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .items import RlmItem

DISABLED_CODEX_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
)
MAX_CANDIDATE_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 64 * 1024 * 1024

# 2026-10-31 隔离强化:评测以 root 运行,chmod/挪位置都拦不住主动探测
# (agent 可以 ls /data/projects 找到 exp_results)。mount namespace 在 agent
# 子进程树内用 tmpfs 盖住答案区:Read/Grep/cat 与 #eval IO.FS.readFile 失效,
# check_candidate.sh(lean 从 .olean 编译)不受影响(已验证)。
# 注意:这对 root 不是硬边界——root 可走 /proc/<pid>/root 或 nsenter 进入其他
# 进程的 mount ns 绕过(2026-10-31 实测)。硬边界需非 root 运行 agent(未做);
# 现行配套:提示词协议禁令(不绕过隔离)+ scan_integrity.py 事件扫描。
# 环境变量 RLM_MASK_PATHS(colon 分隔)可覆盖默认;设为空串显式关闭(不建议)。
MASK_DEFAULT_PATHS = ("/data/projects/exp_results",)


def build_masked_command(command: list[str], env: dict | None = None) -> list[str]:
    """把 agent 命令包进 mount namespace,遮蔽 MASK_DEFAULT_PATHS(或 RLM_MASK_PATHS)。"""
    raw = (env or os.environ).get("RLM_MASK_PATHS")
    mask_paths = (
        tuple(p for p in raw.split(":") if p) if raw is not None else MASK_DEFAULT_PATHS
    )
    if not mask_paths:
        return list(command)
    mounts = " && ".join(f"mount -t tmpfs -o mode=700 none {shlex.quote(p)}" for p in mask_paths)
    # mount 失败必须响亮退出(exit 97):宁可不跑,不能在未遮蔽状态下悄悄跑
    inner = f"{mounts} || {{ echo 'RLM mask mount failed' >&2; exit 97; }}; exec \"$@\""
    return ["unshare", "-m", "--propagation", "private", "bash", "-c", inner, "--", *command]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunConfig:
    model: str
    codex_bin: str
    timeout_seconds: int
    max_compile_attempts: int
    reasoning_effort: str | None
    artifacts_dir: Path
    workspaces_dir: Path
    concurrency: int = 1
    samples: int = 1
    task: str = "statement"
    remove_workspaces: bool = False


_LEAN_ENV_CACHE: dict[str, tuple[str, str]] = {}


def _resolve_lean_env(project_dir: str, elan_home: Path) -> tuple[str, str]:
    """Resolve the pinned lean binary and LEAN_PATH for a repo (outside the sandbox).

    Bypasses elan/lake entirely inside the agent sandbox: the elan shim cannot resolve
    toolchains from the workspace cwd and lake tries to rewrite the read-only .lake/config.
    """
    key = str(project_dir)
    if key in _LEAN_ENV_CACHE:
        return _LEAN_ENV_CACHE[key]
    toolchain = (Path(project_dir) / "lean-toolchain").read_text().strip()
    toolchain_dir = toolchain.replace("/", "--").replace(":", "---")
    lean_bin = str(Path(elan_home) / "toolchains" / f"leanprover--lean4---{toolchain_dir}" / "bin" / "lean")
    lean_bin = lean_bin.replace("leanprover--lean4---leanprover--lean4---", "leanprover--lean4---")
    if not Path(lean_bin).is_file():
        raise FileNotFoundError(f"toolchain binary not found: {lean_bin}")
    entries = [str(Path(project_dir) / ".lake" / "build" / "lib")]
    packages = Path(project_dir) / ".lake" / "packages"
    if packages.is_dir():
        entries.extend(str(p / ".lake" / "build" / "lib") for p in sorted(packages.iterdir()) if (p / ".lake" / "build" / "lib").is_dir())
    lean_path = ":".join(entries)
    _LEAN_ENV_CACHE[key] = (lean_bin, lean_path)
    return lean_bin, lean_path

CLAUDE_RECEIPT = (
    'Finish with exactly `{"candidate_path":"Candidate.lean","status":"completed"}` and no extra keys. '
    'Use status `blocked` only after a real attempt has failed.'
)


def _task_file_proof(item: RlmItem, max_compile_attempts: int, receipt: str = "") -> str:
    compile_budget = (
        f"at most {max_compile_attempts} times"
        if max_compile_attempts > 0
        else "as many times as you need"
    )
    return f"""# RLM25 proof formalization task ({item.repo})

`Candidate.lean` already contains the target theorem statement followed by `:= by sorry`.
Write a complete Lean 4 tactic proof that replaces the `sorry`. The theorem statement itself
must remain byte-identical: do not rename it, do not add or change hypotheses, do not add
imports, `open`, namespaces, axioms, definitions, or a second declaration.
`Context.lean` contains the Lean context (imports, namespaces, and the theorems of the paper
blueprint) available to you. The hidden file `.context-full.lean` is part of the checking
infrastructure: do not read, copy, or otherwise access it; the compiler uses it internally.
Your proof may use any lemma available in `Context.lean` or in Mathlib.
The informal statement and informal proof from the paper blueprint are given below as a hint;
you do not need to follow them line by line if a cleaner Lean proof works.
Your proof must not contain `sorry`, `admit`, `axiom`, `native_decide`, or `apply?`.
Edit only `Candidate.lean`. You may run `./check_candidate.sh` {compile_budget}.
Use only `./check_candidate.sh` for compilation; never invoke `lean` or `lake` directly.
Do not read, write, remove, or otherwise access `.compile_count`; the wrapper enforces the budget.
Do not inspect files outside this task directory and do not use the network.
This workspace is a deliberately isolated evaluation environment: do not attempt to bypass
that isolation by any means or through any process, namespace, or filesystem mechanism;
anything not present in this workspace is intentionally unavailable.
Before finishing, read this file and `Context.lean`, write your best proof into
`Candidate.lean`, and run `./check_candidate.sh` at least once.
{receipt}

## Informal statement

{item.informal_stmt}

## Informal proof (paper blueprint)

{item.informal_proof}
"""


def _task_file(item: RlmItem, max_compile_attempts: int, receipt: str = "") -> str:
    compile_budget = (
        f"at most {max_compile_attempts} times"
        if max_compile_attempts > 0
        else "as many times as you need"
    )
    return f"""# RLM25 statement formalization task ({item.repo})

Translate only the natural-language theorem statement below into one Lean 4 theorem declaration.
Do not translate or produce a proof. The declaration's proof must remain exactly `:= by sorry`.
The declaration must be named `{item.name}`. `Context.lean` contains the exact Lean context
(imports, namespaces, prior declarations) under which your declaration is elaborated.
Do not add or repeat imports, `open` directives, namespaces, helpers, axioms, definitions, or a second declaration.
Edit only `Candidate.lean`. You may run `./check_candidate.sh` {compile_budget}.
Use only `./check_candidate.sh` for compilation; never invoke `lean` or `lake` directly.
Do not read, write, remove, or otherwise access `.compile_count`; the wrapper enforces the budget.
Do not inspect files outside this task directory and do not use the network.
This workspace is a deliberately isolated evaluation environment: do not attempt to bypass
that isolation by any means or through any process, namespace, or filesystem mechanism;
anything not present in this workspace is intentionally unavailable.
Before finishing, read this file and `Context.lean`, write your best attempt into
`Candidate.lean`, and run `./check_candidate.sh` at least once.
{receipt}

## Natural-language statement

{item.informal_stmt}
"""


def _check_script(workspace: Path, item: RlmItem, max_compile_attempts: int, cache_home: Path, elan_home: Path, lean_bin: str = "", lean_path: str = "") -> str:
    compile_guard = (
        f'''if [ "$count" -ge {max_compile_attempts} ]; then
  echo "Compile budget exhausted ({max_compile_attempts})." >&2
  exit 64
fi
'''
        if max_compile_attempts > 0
        else ""
    )
    return f"""#!/bin/sh
set -eu
count_file={shlex.quote(str(workspace / '.compile_count'))}
generated_dir={shlex.quote(str(workspace / '.generated'))}
context_file={shlex.quote(str(workspace / 'Context.lean'))}
candidate_file={shlex.quote(str(workspace / 'Candidate.lean'))}
combined_file={shlex.quote(str(workspace / '.generated' / 'Check.lean'))}
project_root={shlex.quote(item.project_dir)}
cache_home={shlex.quote(str(cache_home))}
elan_home={shlex.quote(str(elan_home))}

count=0
if [ -f "$count_file" ]; then
  count=$(/bin/cat "$count_file")
fi
{compile_guard}count=$((count + 1))
/bin/echo "$count" > "$count_file"
mkdir -p "$generated_dir"
full_context_file={shlex.quote(str(workspace / '.context-full.lean'))}
if [ -f "$full_context_file" ]; then
  ctx="$full_context_file"
else
  ctx="$context_file"
fi
{{ /bin/cat "$ctx"; /bin/echo; /bin/cat "$candidate_file"; }} > "$combined_file"
if [ -n "{shlex.quote(lean_bin)}" ]; then
  /usr/bin/env XDG_CACHE_HOME="$cache_home" GIT_CONFIG_GLOBAL=/dev/null LEAN_PATH={shlex.quote(lean_path)} {shlex.quote(lean_bin)} "$combined_file"
else
  /usr/bin/env XDG_CACHE_HOME="$cache_home" GIT_CONFIG_GLOBAL=/dev/null ELAN_HOME="$elan_home" PATH="$elan_home/bin:/usr/bin:/bin:/usr/sbin:/sbin" lake -d "$project_root" env lean "$combined_file"
fi
"""


def prepare_workspace(item: RlmItem, *, run_id: str, config: RunConfig, cache_home: Path, elan_home: Path, lean_bin: str = "", lean_path: str = "", receipt: str = "") -> Path:
    workspace_parent = config.workspaces_dir.resolve() / run_id
    workspace_parent.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        workspace = workspace_parent / f"task-{secrets.token_hex(16)}"
        try:
            workspace.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("Unable to allocate a unique agent workspace")
    (workspace / ".generated").mkdir()
    (workspace / ".compile_count").write_text("0\n", encoding="ascii")
    if config.task == "proof-normal":
        (workspace / "Context.lean").write_text(item.context_normal, encoding="utf-8")
        (workspace / ".context-full.lean").write_text(item.context, encoding="utf-8")
        (workspace / "TASK.md").write_text(_task_file_proof(item, config.max_compile_attempts, receipt), encoding="utf-8")
        (workspace / "Candidate.lean").write_text(item.decl + "\n  sorry\n", encoding="utf-8")
    elif config.task == "proof":
        (workspace / "Context.lean").write_text(item.context, encoding="utf-8")
        (workspace / "TASK.md").write_text(_task_file_proof(item, config.max_compile_attempts, receipt), encoding="utf-8")
        (workspace / "Candidate.lean").write_text(item.decl + "\n  sorry\n", encoding="utf-8")
    else:
        (workspace / "Context.lean").write_text(item.context, encoding="utf-8")
        (workspace / "TASK.md").write_text(_task_file(item, config.max_compile_attempts, receipt), encoding="utf-8")
        (workspace / "Candidate.lean").write_text(
            f"theorem {item.name} : True := by sorry\n", encoding="utf-8"
        )
    script = workspace / "check_candidate.sh"
    script.write_text(
        _check_script(workspace, item, config.max_compile_attempts, cache_home, elan_home, lean_bin, lean_path),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return workspace


AGENT_PROMPT_STATEMENT = (
    "Read TASK.md and Context.lean. Formalize only the theorem statement by editing Candidate.lean. "
    "Keep the proof exactly `:= by sorry`. Use only ./check_candidate.sh for compiler feedback; never "
    "invoke lean or lake directly, and do not access .compile_count. Respect the compile budget and do "
    "not inspect outside this workspace. The workspace is deliberately isolated; do not attempt "
    "to bypass that isolation by any means or through any process, namespace, or filesystem mechanism. "
    "You MUST first read TASK.md and Context.lean, write your best "
    "formalization into Candidate.lean, and run ./check_candidate.sh at least once; responding without "
    "these steps is a protocol violation."
)


AGENT_PROMPT_PROOF = (
    "Read TASK.md and Context.lean. Candidate.lean already contains the target theorem with `:= by sorry`; "
    "replace the sorry with a complete tactic proof, keeping the theorem statement byte-identical. "
    "Use only ./check_candidate.sh for compiler feedback; never "
    "invoke lean or lake directly, and do not access .compile_count. Respect the compile budget and do "
    "not inspect outside this workspace. The workspace is deliberately isolated; do not attempt "
    "to bypass that isolation by any means or through any process, namespace, or filesystem mechanism. "
    "You MUST first read TASK.md and Context.lean, write your best "
    "proof into Candidate.lean, and run ./check_candidate.sh at least once; responding without "
    "these steps is a protocol violation."
)


async def _run_one(
    item: RlmItem,
    sample_id: int,
    run_id: str,
    config: RunConfig,
    generations_path: Path,
    cache_home: Path,
    elan_home: Path,
    write_lock: asyncio.Lock,
) -> None:
    try:
        lean_bin, lean_path = _resolve_lean_env(item.project_dir, elan_home)
    except (OSError, FileNotFoundError):
        lean_bin, lean_path = "", ""
    workspace = prepare_workspace(
        item, run_id=run_id, config=config, cache_home=cache_home, elan_home=elan_home, lean_bin=lean_bin, lean_path=lean_path
    )
    agent_prompt = (
        AGENT_PROMPT_PROOF if config.task in {"proof", "proof-normal"} else AGENT_PROMPT_STATEMENT
    )
    command = [
        config.codex_bin,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--json",
        "--model",
        config.model,
        "--cd",
        str(workspace),
        "--config",
        'web_search="disabled"',
        "--config",
        'shell_environment_policy.inherit="none"',
        "--sandbox",
        "workspace-write",
    ]
    for feature in DISABLED_CODEX_FEATURES:
        command.extend(["--disable", feature])
    if config.reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{config.reasoning_effort}"'])
    command.append("-")
    # 隔离强化(2026-10-31):codex 沙箱只禁写不禁读,统一用 mount namespace 遮蔽答案区
    command = build_masked_command(command)

    env = os.environ.copy()
    env["ELAN_HOME"] = str(elan_home)
    env["XDG_CACHE_HOME"] = str(cache_home)
    started_at = utc_now()
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        stdout, stderr = b"", b""
        exit_code = None
        termination = "launch_error"
        error_detail = f"{type(exc).__name__}: {exc}"
    else:
        error_detail = None
        termination = None
        communicate = asyncio.create_task(process.communicate(agent_prompt.encode("utf-8")))
        try:
            if config.timeout_seconds > 0:
                stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout=config.timeout_seconds)
            else:
                stdout, stderr = await communicate
        except asyncio.TimeoutError:
            termination = "timeout"
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout=5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                try:
                    stdout, stderr = await asyncio.wait_for(communicate, timeout=5)
                except asyncio.TimeoutError:
                    stdout, stderr = b"", b""
                    error_detail = "Codex subprocess did not report exit after termination"
        exit_code = process.returncode
    wall_ms = int((time.monotonic() - started) * 1000)
    status = (
        "agent_timeout"
        if termination == "timeout"
        else ("ok" if exit_code == 0 else "agent_error")
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
    final_message = None
    for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
        if raw_line.strip().startswith("{\"type\":"):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                final_message = event["item"].get("text")
    record = {
        "run_id": run_id,
        "track": "codex_agent",
        "item_index": item.index,
        "item_name": item.label,
        "item_repo": item.repo,
        "sample_id": sample_id,
        "model": config.model,
        "status": status,
        "exit_code": exit_code,
        "error_detail": error_detail,
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


async def save_generation(record: dict, generations_path: Path, write_lock: asyncio.Lock, remove_workspace: bool) -> None:
    raw_text = record["raw_text"]
    result_dir = generations_path.parent / "candidates" / f"{record['item_index']:06d}-{record['sample_id']:04d}"
    result_dir.mkdir(parents=True, exist_ok=False)
    candidate_path = result_dir / "Candidate.lean"
    if raw_text:
        candidate_path.write_text(raw_text, encoding="utf-8")
    record["candidate_path"] = str(candidate_path) if raw_text else None
    record["candidate_present"] = bool(raw_text)
    metadata = {key: value for key, value in record.items() if key != "raw_text"}
    (result_dir / "generation-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False) + "\n", encoding="utf-8")
    async with write_lock:
        with generations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if remove_workspace:
        shutil.rmtree(record["workspace"])


async def run_codex(
    items: list[RlmItem],
    run_id: str,
    config: RunConfig,
    cache_home: Path,
    elan_home: Path,
) -> Path:
    if not items or min(config.concurrency, config.samples) < 1:
        raise ValueError("Select at least one item and use positive concurrency and sample counts")
    generations_path = config.artifacts_dir.resolve() / run_id / "generations.jsonl"
    generations_path.parent.mkdir(parents=True, exist_ok=True)
    if generations_path.exists():
        raise FileExistsError(f"{generations_path} already exists; choose a new --run-id")
    write_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(config.concurrency)
    async def run_sample(item: RlmItem, sample_id: int) -> None:
        async with semaphore:
            await _run_one(item, sample_id, run_id, config, generations_path, cache_home, elan_home, write_lock)

    await asyncio.gather(
        *(
            run_sample(item, sample_id)
            for item in items
            for sample_id in range(config.samples)
        )
    )
    return generations_path
