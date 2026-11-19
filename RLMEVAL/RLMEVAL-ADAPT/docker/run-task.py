"""Run the existing RLMEval generation CLI on an isolated single-task bundle."""
import argparse
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys

from rlm_agent.items import load_items
from rlm_pipeline.task_bundle import load_task


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    bundle = Path("/input/task")
    task = load_task(bundle)
    items = load_items(Path("/input/item.jsonl"))
    if set(items) != {task["index"]}:
        raise ValueError("Expected exactly the mounted task in item.jsonl")
    item = items[task["index"]]
    for field in ("repo", "file", "context", "decl", "informal_stmt", "informal_proof"):
        if getattr(item, field) != task[field]:
            raise ValueError(f"Task bundle and item disagree on {field}")

    project = Path("/workspace/project")
    shared = Path("/opt/rlm/envs") / item.repo.lower()
    toolchain = (bundle / "lean-toolchain").read_text()
    if (shared / "lean-toolchain").read_text().strip() != toolchain.strip():
        raise ValueError("Task and image toolchains differ")
    project.mkdir()
    (project / "lean-toolchain").write_text(toolchain)
    build = project / ".lake/build"
    build.mkdir(parents=True)
    (build / "lib").symlink_to(bundle / "lib")
    package = project / ".lake/packages/shared/.lake/build"
    package.mkdir(parents=True)
    (package / "lib").symlink_to(shared / "lib")

    item = dataclasses.replace(item, project_dir=str(project))
    dataset = Path("/workspace/item.jsonl")
    dataset.write_text(json.dumps(item.to_row(), ensure_ascii=False) + "\n")
    if args.prepare_only:
        from rlm_agent.runner import _resolve_lean_env
        lean, lean_path = _resolve_lean_env(item.project_dir, Path("/opt/elan"))
        check = Path("/workspace/Check.lean")
        check.write_text(item.context + "\n" + (bundle / "Candidate.lean").read_text())
        subprocess.run([lean, str(check)], env=dict(os.environ, LEAN_PATH=lean_path), check=True)
        return

    backend = os.environ["BACKEND"]
    command = [sys.executable, "-m", "rlm_agent.cli", f"run-{backend}",
               "--dataset", str(dataset), "--indices", str(item.index),
               "--task", "proof", "--model", os.environ["MODEL"],
               "--run-id", "generation", "--artifacts-dir", "/output",
               "--workspaces-dir", "/workspace", "--cache-dir", "/cache_dir",
               "--elan-home", "/opt/elan", "--remove-workspaces",
               "--samples", os.environ.get("SAMPLES", "1"), "--concurrency", "1",
               "--timeout-seconds", os.environ.get("TMO", "21600"),
               "--max-compile-attempts", os.environ.get("MAX_COMPILE_ATTEMPTS", "0"),
               "--reasoning-effort", os.environ.get("REASONING_EFFORT", "high")]
    if backend == "codex":
        command += ["--codex-bin", "/opt/adapter/codex-container.sh"]
    elif backend == "claude":
        command += ["--claude-bin", "/usr/local/bin/claude",
                    "--anthropic-base-url", os.environ["ANTHROPIC_BASE_URL"]]
        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            command += ["--anthropic-auth-token-env", "ANTHROPIC_AUTH_TOKEN"]
    else:
        raise ValueError(f"Unknown backend: {backend}")
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
