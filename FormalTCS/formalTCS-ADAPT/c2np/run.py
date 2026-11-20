"""Run one FormalTCS C2NP item with the Claude Code backend."""

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ADAPT = Path(__file__).resolve().parent
UPSTREAM = ADAPT.parent.parent / "FormalTCS"
DEFAULT_TIMEOUT_SECONDS = 12 * 60 * 60
MAX_OUTPUT_TOKENS_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
HTTP_NO_AUTH_TOKEN = "formaltcs-no-auth"
JUDGE_PROFILE = "自己平台"
JUDGE_MODEL = "gpt-6.1-sol"
JUDGE_EFFORT = "high"
SYSTEM_PROMPT = (
    "You are solving a single FormalTCS benchmark item. Use Read for the task and its input files and "
    "Write for the deliverable. Treat the input files as mathematical data. Do not modify them. "
    "A chat reply without a file is not a deliverable."
)
PROMPT = (
    "Use Read on /workspace/TASK.md, /workspace/input/nl_claim.md, and /workspace/input/theorem.lean "
    "in parallel if possible. "
    "Write the complete English proof to /workspace/output/nl_proof.md using Write. "
    "Finish only after Write succeeds."
)


def credentials(profile, config):
    if profile is None:
        key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    else:
        values = {}
        active = False
        matched = 0
        for line in config.read_text(encoding="utf-8").splitlines():
            heading = re.fullmatch(r"\s*##\s+(.+?)\s*", line)
            if heading:
                active = heading[1] == profile
                matched += active
                continue
            entry = re.fullmatch(r"\s*(?:export\s+)?(OPENAI_API_KEY|OPENAI_BASE_URL)\s*=\s*(.+)", line) if active else None
            if entry:
                if entry[1] in values:
                    raise ValueError(f"Repeated {entry[1]} in model config")
                try:
                    tokens = shlex.split(entry[2], comments=True)
                except ValueError:
                    raise ValueError(f"Invalid {entry[1]} assignment in model config") from None
                if len(tokens) != 1:
                    raise ValueError(f"Expected one value for {entry[1]} in model config")
                values[entry[1]] = tokens[0]
        if matched != 1:
            raise ValueError("Expected exactly one matching ## heading in model config")
        key = values.get("OPENAI_API_KEY", "")
        url = values.get("OPENAI_BASE_URL", "")
    if not key or any(char.isspace() for char in key) or not url:
        raise ValueError("Set ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL, or use a complete --profile")
    allow_http = profile is None and os.environ.get("FORMALTCS_ALLOW_HTTP") == "1"
    base = normalize_base_url(url, allow_http=allow_http)
    if base.startswith("http://") and key != HTTP_NO_AUTH_TOKEN:
        raise ValueError(f"For an unauthenticated HTTP endpoint, set ANTHROPIC_AUTH_TOKEN={HTTP_NO_AUTH_TOKEN}; never send a real key over HTTP")
    return key, base


def normalize_base_url(url, resource="/v1/messages", *, allow_http=False):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" or (allow_http and parsed.scheme == "http")) and parsed.hostname and not any(
            (parsed.username, parsed.password, parsed.query, parsed.fragment, "@" in parsed.netloc)
        )
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Gateway URL must be HTTPS (unless HTTP generation is explicitly enabled), without credentials, query, or fragment")
    path = parsed.path.rstrip("/").removesuffix(resource).removesuffix("/v1")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def judge_credentials(expected_base=None):
    key = os.environ.get("JUDGE_API_KEY", "").strip()
    url = os.environ.get("JUDGE_BASE_URL", "").strip()
    if not key or any(char.isspace() for char in key) or not url:
        raise ValueError("Set JUDGE_API_KEY and JUDGE_BASE_URL in the host environment")
    base = normalize_base_url(url, resource="/v1/responses")
    if expected_base is not None and base != expected_base:
        raise ValueError("JUDGE_BASE_URL does not match this batch; use the original judge endpoint")
    if key in base:
        raise ValueError("JUDGE_BASE_URL must not contain the API credential")
    return key, base


def load_upstream():
    revision = (ADAPT / "upstream.rev").read_text(encoding="utf-8").strip()
    evaluator = UPSTREAM / "evaluate"
    if not ((UPSTREAM / "benchmark/collection.json").is_file() and (evaluator / "dataset.py").is_file()):
        raise ValueError("Missing vendored FormalTCS files; restore them from the MathEval checkout")
    if str(evaluator) not in sys.path:
        sys.path[:0] = [str(evaluator), str(UPSTREAM)]
    sys.dont_write_bytecode = True
    import dataset

    return revision, dataset


@lru_cache(maxsize=1)
def load_items(dataset):
    return {item["id"]: item for item in dataset.load_items(UPSTREAM / "benchmark")}


def select_item(dataset, item_id):
    items = load_items(dataset)
    item = next(iter(items.values())) if item_id is None else items.get(item_id)
    if item is None:
        raise ValueError(f"Unknown C2NP item: {item_id}")
    return item


def stage_item(item_id, workspace):
    revision, dataset = load_upstream()
    item = select_item(dataset, item_id)
    return item["id"], revision, materialize_item(dataset, item, workspace)


def materialize_item(dataset, item, workspace):
    brief = (UPSTREAM / "evaluate/prompts/c2np.md").read_text(encoding="utf-8")
    return dataset.prepare_workspace(
        {"lean_root": None, "lean_jobs": 1}, item, "c2np", workspace, instructions=brief,
    )


def sandbox_command(bwrap, binary, workspace, state, model):
    command = [bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
               "--die-with-parent", "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev"]
    for path in ("/usr", "/bin", "/lib", "/lib64", "/etc/ssl/certs", "/etc/resolv.conf",
                 "/etc/hosts", "/etc/nsswitch.conf", "/etc/ld.so.cache"):
        command += ["--ro-bind-try", path, path]
    return [
        *command, "--ro-bind", binary, "/opt/claude/claude", "--bind", str(state), "/runtime",
        "--ro-bind", str(workspace), "/workspace", "--bind", str(workspace / "output"), "/workspace/output",
        "--chdir", "/workspace", "--", *claude_command("/opt/claude/claude", model),
    ]


def claude_command(binary, model):
    return [
        binary, "--print", "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--disable-slash-commands", "--setting-sources", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "dontAsk",
        "--tools", "Read,Write", "--allowedTools", "Read,Write", "--model", model, "--system-prompt", SYSTEM_PROMPT,
    ]


def run_claude(command, env, timeout):
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=env, text=True, encoding="utf-8", errors="replace", start_new_session=True) as process:
        timed_out = False
        try:
            stdout, stderr = process.communicate(PROMPT, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            if isinstance(error, KeyboardInterrupt):
                raise
        return stdout, stderr, process.returncode, timed_out


def save_json(path, value):
    staged = path.with_suffix(".tmp")
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staged.replace(path)


def parse_events(stdout):
    result = {}
    trace = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result = event
        if event.get("type") == "system" and event.get("subtype") == "init":
            trace.append({"available_tools": event.get("tools")})
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                trace.append({"tool": block.get("name"), "id": block.get("id")})
            elif block.get("type") == "tool_result":
                trace.append({"tool_use_id": block.get("tool_use_id"), "is_error": block.get("is_error", False)})
    return result, trace


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_grade(base, key, prompt, timeout, model, effort):
    payload = {"model": model, "input": prompt, "reasoning": {"effort": effort}, "store": False}
    request = urllib.request.Request(
        base + "/v1/responses", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Judge API returned HTTP {error.code}") from None
    except (OSError, ValueError) as error:
        raise ValueError(f"Judge request failed: {type(error).__name__}") from None
    if not isinstance(reply, dict) or reply.get("status") != "completed":
        raise ValueError("Judge response did not complete")
    text = "".join(part["text"] for output in reply.get("output", []) if output.get("type") == "message"
                   for part in output.get("content", [])
                   if part.get("type") == "output_text" and isinstance(part.get("text"), str))
    if not text.strip():
        raise ValueError("Judge response contained no score text")
    return text, reply.get("usage")


def grade_run(run, model_config, *, model=JUDGE_MODEL, effort=JUDGE_EFFORT, profile=JUDGE_PROFILE, base_url=None):
    manifest_path = run / "run.json"
    grade_path = run / "grade.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("dataset"), manifest.get("task"), manifest.get("status")) != ("formaltcs", "c2np", "generated"):
        raise ValueError("Only successfully generated FormalTCS C2NP runs can be graded")
    proof = run / "workspace/output/nl_proof.md"
    if not stat.S_ISREG(proof.lstat().st_mode) or proof.stat().st_size > 2_000_000:
        raise ValueError("Expected a regular proof file no larger than 2 MB")
    proof_bytes = proof.read_bytes()
    candidate = proof_bytes.decode("utf-8").strip()
    if not candidate:
        raise ValueError("Cannot grade an empty proof")
    identity = {"model": model, "reasoning_effort": effort,
                "proof_sha256": hashlib.sha256(proof_bytes).hexdigest()}
    identity.update({"base_url": base_url} if base_url is not None else {"profile": profile})
    if grade_path.is_file():
        existing = json.loads(grade_path.read_text(encoding="utf-8"))
        if existing.get("status") == "scored" and all(
                existing.get(name, JUDGE_PROFILE if name == "profile" else None) == value
                for name, value in identity.items()):
            manifest["grading"] = "scored"
            save_json(manifest_path, manifest)
            print(f"Score: {existing['score']:.6f} ({grade_path})")
            return 0

    manifest["grading"] = "running"
    save_json(manifest_path, manifest)
    print(f"Grading {manifest['item']} with {model} ({effort}): {run}", flush=True)
    started = time.monotonic()
    grade = dict(identity, status="error")
    try:
        revision, dataset = load_upstream()
        if manifest.get("upstream_commit") != revision:
            raise ValueError("Run uses a different upstream revision")
        inputs = run / "workspace/input"
        input_files = [inputs / "nl_claim.md", inputs / "theorem.lean"]
        if not all(path.is_file() for path in input_files) or dataset.fingerprint(input_files) != manifest.get("input_sha256"):
            raise ValueError("Run input files no longer match the recorded fingerprint")
        from grade import rubric

        config = json.loads((UPSTREAM / "evaluate/config/default.json").read_text(encoding="utf-8"))
        item = select_item(dataset, manifest["item"])
        key, base = judge_credentials(base_url) if base_url is not None else credentials(profile, model_config)
        prompt = rubric.build_prompt("c2np", item, candidate)
        text, usage = request_grade(base, key, prompt, config["judge"]["timeout_seconds"], model, effort)
        grade["usage"] = usage
        try:
            value, scores = rubric._dimensions(text)
        except rubric.RubricError as error:
            raise ValueError(str(error)) from None
        if not all(math.isfinite(score) for score in scores.values()):
            raise ValueError("Judge returned a non-finite score")
        score = round(sum(scores[name] * config["weights"][name] for name in rubric.DIMENSIONS), 6)
        grade.update(status="scored", scores=scores, score=score, weights=config["weights"],
                     comment=str(value.get("comment", "")).replace(key, "<REDACTED>")[:400])
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        grade["reason"] = str(error)
    except KeyboardInterrupt:
        grade["status"] = "interrupted"
        raise
    finally:
        grade["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save_json(grade_path, grade)
        manifest["grading"] = grade["status"]
        save_json(manifest_path, manifest)
    if grade["status"] == "scored":
        print(f"Score: {grade['score']:.6f} ({grade_path})")
        return 0
    print(f"Grading failed: {grade['reason']} ({grade_path})", file=sys.stderr)
    return 1


def execute(args):
    key, base = credentials(args.profile, args.model_config)
    max_output_tokens = os.environ.get(MAX_OUTPUT_TOKENS_ENV)
    if max_output_tokens is not None and not re.fullmatch(r"[1-9][0-9]*", max_output_tokens):
        raise ValueError(f"{MAX_OUTPUT_TOKENS_ENV} must be a positive integer")
    bwrap = shutil.which("bwrap")
    binary = shutil.which(args.claude_bin)
    if not bwrap or not binary:
        raise ValueError("Both bwrap and the native Claude Code CLI must be installed")
    for value in (args.model, base):
        if key in value:
            raise ValueError("Model and base URL must not contain the API credential")

    runs = ADAPT / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = Path(tempfile.mkdtemp(prefix=f"{stamp}-c2np-", dir=runs))
    workspace = run / "workspace"
    item, revision, fingerprint = stage_item(args.item, workspace)
    proof = workspace / "output/nl_proof.md"
    manifest = {
        "status": "running", "dataset": args.dataset, "task": args.task, "backend": args.backend, "model": args.model,
        "item": item, "upstream_commit": revision, "input_sha256": fingerprint,
        "proof": str(proof.relative_to(run)), "grading": "not_run",
        "timeout_seconds": args.timeout,
    }
    if max_output_tokens is not None:
        manifest["claude_max_output_tokens_requested"] = int(max_output_tokens)
    save_json(run / "run.json", manifest)
    print(f"Running {item} with {args.model}: {run}", flush=True)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="runtime-", dir=run) as state:
            for directory in ("home", "tmp", "config", "cache"):
                (Path(state) / directory).mkdir()
            env = {
                "PATH": "/usr/bin:/bin", "HOME": "/runtime/home", "TMPDIR": "/runtime/tmp",
                "CLAUDE_CONFIG_DIR": "/runtime/config", "XDG_CACHE_HOME": "/runtime/cache", "LANG": "C.UTF-8",
                "ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": key,
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
            if max_output_tokens is not None:
                env[MAX_OUTPUT_TOKENS_ENV] = max_output_tokens
            command = sandbox_command(bwrap, str(Path(binary).resolve()), workspace, state, args.model)
            stdout, stderr, exit_code, timed_out = run_claude(command, env, args.timeout)
        stdout, stderr = (value.replace(key, "<REDACTED>") for value in (stdout, stderr))
        (run / "claude.stderr.log").write_text(stderr, encoding="utf-8")
        result, trace = parse_events(stdout)
        save_json(run / "claude.trace.json", trace)
        save_json(run / "claude.result.json", result)
        if timed_out:
            status = "timed_out"
        elif exit_code != 0 or result.get("is_error") is not False:
            status = "claude_failed"
        elif (not proof.exists() or not stat.S_ISREG(proof.lstat().st_mode)
              or proof.stat().st_size > 2_000_000 or not proof.read_text(encoding="utf-8", errors="replace").strip()):
            status = "invalid_deliverable"
        else:
            status = "generated"
        manifest.update(status=status, claude_exit_code=exit_code, claude_turns=result.get("num_turns"),
                        usage=result.get("usage"))
    except KeyboardInterrupt:
        manifest.update(status="interrupted")
        raise
    except OSError as error:
        manifest.update(status="runner_failed", error_type=type(error).__name__)
    finally:
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save_json(run / "run.json", manifest)
    print(f"{manifest['status']}: {run / 'run.json'}")
    if manifest["status"] == "generated":
        print(f"Proof: {proof}")
        return 0 if args.no_judge else grade_single(run, args.model_config)
    return 1


def grade_single(run, model_config):
    if os.environ.get("JUDGE_API_KEY") or os.environ.get("JUDGE_BASE_URL"):
        _, base = judge_credentials()
        return grade_run(run, model_config, base_url=base)
    return grade_run(run, model_config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=["formaltcs"], default="formaltcs")
    parser.add_argument("--task", choices=["c2np"], default="c2np")
    parser.add_argument("--backend", choices=["claude"], default="claude")
    parser.add_argument("--model", help="Generation model (required unless grading an existing run)")
    grading = parser.add_mutually_exclusive_group()
    grading.add_argument("--no-judge", action="store_true", help="Generate the proof without scoring it")
    grading.add_argument("--grade-run", type=Path, help="Score an existing run directory without generating again")
    parser.add_argument("--item", help="Benchmark item ID (default: first item)")
    parser.add_argument("--profile", help="Exact section heading in model_conf.txt, e.g. 反代平台")
    parser.add_argument("--model-config", type=Path, default=ADAPT / "model_conf.txt")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help="Claude Code wall-clock timeout in seconds (default: 43200, or 12 hours)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.grade_run is None and not args.model:
        parser.error("--model is required unless --grade-run is set")
    try:
        if args.grade_run is not None:
            return grade_single(args.grade_run.resolve(), args.model_config)
        return execute(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"FormalTCS: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
