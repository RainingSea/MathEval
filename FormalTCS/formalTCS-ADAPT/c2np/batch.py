"""Orchestrate one Docker container per FormalTCS C2NP sample and score on the host."""

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import run


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def selected_items(dataset, requested, limit, indices=None):
    available = run.load_items(dataset)
    if requested:
        ids = [item.strip() for item in requested.split(",")]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate item IDs; use PASS_K to request multiple samples")
        for item_id in ids:
            if item_id not in available:
                raise ValueError(f"Unknown C2NP item: {item_id!r}")
    elif indices is not None and indices.strip() != "all":
        ordered = list(available)
        ids = []
        seen = set()
        for entry in indices.split(","):
            match = re.fullmatch(r"([0-9]+)(?:\s*-\s*([0-9]+))?", entry.strip())
            if not match:
                raise ValueError("INDICES must be 'all' or a list like 1,3-5")
            first, last = int(match[1]), int(match[2] or match[1])
            if not 1 <= first <= last <= len(ordered):
                raise ValueError(f"INDICES ranges must be ascending and between 1 and {len(ordered)}")
            for index in range(first, last + 1):
                if index in seen:
                    raise ValueError("Duplicate indices; use SAMPLES to request multiple samples")
                seen.add(index)
                ids.append(ordered[index - 1])
    elif indices is not None:
        ids = list(available)
    else:
        ids = list(available)[:limit or 1]
    return [available[item_id] for item_id in ids]


def container_command(name, directory, image):
    workspace = directory / "workspace"
    if "," in str(workspace):
        raise ValueError("Docker workspace path cannot contain a comma")
    return [
        "docker", "run", "--rm", "--interactive", "--init", "--name", name, "--pull=never", "--log-driver=none",
        "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=128",
        "--tmpfs", "/runtime:rw,nosuid,nodev,noexec,mode=1777,size=128m",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777,size=128m",
        "--mount", f"type=bind,source={workspace},target=/workspace,readonly",
        "--mount", f"type=bind,source={workspace / 'output'},target=/workspace/output",
        image,
    ]


def stop_container(name, process):
    try:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        process.kill()


def final_event(log):
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("event") == "result":
                return event
    return {}


def generate_sample(directory, image, config, timeout, cancelled):
    manifest_path = directory / "run.json"
    manifest = read_json(manifest_path)
    if cancelled.is_set():
        run.save_json(manifest_path, dict(manifest, status="interrupted"))
        return
    name = "formaltcs-" + uuid.uuid4().hex
    log = directory / "docker.log"
    proof = directory / "workspace/output/nl_proof.md"
    started = time.monotonic()
    manifest.update(status="running", container=name)
    run.save_json(manifest_path, manifest)
    try:
        with log.open("w", encoding="utf-8") as output, subprocess.Popen(
                container_command(name, directory, image), stdin=subprocess.PIPE, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True) as process:
            try:
                with process.stdin as feed:
                    feed.write(json.dumps(config).encode("utf-8") + b"\n")
            except BrokenPipeError:
                pass
            while process.poll() is None:
                if cancelled.is_set() or time.monotonic() - started >= timeout:
                    manifest["status"] = "interrupted" if cancelled.is_set() else "timed_out"
                    stop_container(name, process)
                    break
                cancelled.wait(0.25)
        event = final_event(log)
        result = event.get("payload", {})
        manifest.update(docker_exit_code=process.returncode, claude_exit_code=event.get("claude_exit_code"),
                        claude_turns=result.get("num_turns"), usage=result.get("usage"))
        if manifest["status"] == "running":
            if process.returncode in (125, 126, 127) or not event:
                manifest["status"] = "docker_failed"
            elif process.returncode != 0 or result.get("is_error") is not False:
                manifest["status"] = "claude_failed"
            elif (not proof.exists() or not stat.S_ISREG(proof.lstat().st_mode) or proof.stat().st_size > 2_000_000
                  or not proof.read_text(encoding="utf-8").strip()):
                manifest["status"] = "invalid_deliverable"
            else:
                manifest["status"] = "generated"
    except (OSError, ValueError) as error:
        manifest.update(status="runner_failed", error_type=type(error).__name__)
    finally:
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        run.save_json(manifest_path, manifest)
    print(f"{manifest['status']}: {directory}", file=sys.stderr, flush=True)


def generate(args):
    revision, dataset = run.load_upstream()
    items = selected_items(dataset, args.items, args.limit, args.indices)
    key, base = run.credentials(None, None)
    judge_key, judge_base = run.judge_credentials()
    if any(secret in value for secret in (key, judge_key)
           for value in (base, judge_base, args.model, args.judge_model, args.judge_effort)):
        raise ValueError("Gateway URLs, model names, and judge effort must not contain API credentials")
    maximum = os.environ.get(run.MAX_OUTPUT_TOKENS_ENV)
    if maximum is not None and not re.fullmatch(r"[1-9][0-9]*", maximum):
        raise ValueError(f"{run.MAX_OUTPUT_TOKENS_ENV} must be a positive integer")
    config = {"key": key, "base_url": base, "model": args.model,
              "max_output_tokens": int(maximum) if maximum else None}
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected exactly one locally built Docker image")
    runs = run.ADAPT / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch = Path(tempfile.mkdtemp(prefix=f"batch-{stamp}-", dir=runs))
    plan = []
    settings = {"status": "generating", "upstream_commit": revision, "image_id": image,
                "dataset": "formaltcs", "task": "c2np", "backend": "claude", "model": args.model,
                "judge": {"model": args.judge_model, "effort": args.judge_effort, "base_url": judge_base},
                "workers": args.workers, "samples_per_item": args.samples, "timeout_seconds": args.timeout,
                "claude_max_output_tokens_requested": config["max_output_tokens"], "samples": plan}
    for item in items:
        for index in range(1, args.samples + 1):
            relative = Path(item["id"]) / f"sample-{index:03d}"
            directory = batch / relative
            fingerprint = run.materialize_item(dataset, item, directory / "workspace")
            run.save_json(directory / "run.json", {
                "status": "queued", "dataset": "formaltcs", "task": "c2np", "backend": "claude", "model": args.model,
                "item": item["id"], "sample": index, "upstream_commit": revision, "input_sha256": fingerprint,
                "proof": "workspace/output/nl_proof.md", "grading": "not_run", "timeout_seconds": args.timeout,
            })
            plan.append(str(relative))
    run.save_json(batch / "batch.json", settings)
    print(f"Batch: {batch} ({len(plan)} samples, {args.workers} workers)", file=sys.stderr, flush=True)
    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(generate_sample, batch / relative, image, config, args.timeout, cancelled) for relative in plan]
        try:
            for job in as_completed(jobs):
                job.result()
        except BaseException:
            cancelled.set()
            settings["status"] = "interrupted"
            run.save_json(batch / "batch.json", settings)
            raise
    settings["status"] = "generation_finished"
    run.save_json(batch / "batch.json", settings)
    print(batch)
    return 0


def grade_batch(args):
    batch = args.batch.resolve()
    with (batch / "batch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return grade_results(batch, args.model_config)


def grade_results(batch, model_config):
    settings = read_json(batch / "batch.json")
    if settings.get("status") not in {"generation_finished", "scoring", "scored", "incomplete"}:
        raise ValueError("Batch generation must finish before grading")
    if settings["judge"].get("base_url") is not None and settings.get("status") != "scored":
        run.judge_credentials(settings["judge"]["base_url"])
    samples = []
    for relative in settings["samples"]:
        directory = (batch / relative).resolve()
        if not directory.is_relative_to(batch):
            raise ValueError("Sample path escapes the batch directory")
        samples.append((directory, read_json(directory / "run.json")))
    if any(result["status"] in {"queued", "running"} for _, result in samples):
        raise ValueError("Batch still has unfinished samples")
    settings["status"] = "scoring"
    run.save_json(batch / "batch.json", settings)
    records = []
    scores = []
    generated = 0
    for directory, result in samples:
        row = {"item": result["item"], "sample": result["sample"], "status": result["status"],
               "path": str(directory.relative_to(batch)), "grading": "not_run", "score": None}
        if result["status"] == "generated":
            generated += 1
            try:
                exit_code = run.grade_run(directory, model_config, **settings["judge"])
                row["grading"] = "scored" if exit_code == 0 else "error"
                if exit_code == 0:
                    row["score"] = read_json(directory / "grade.json")["score"]
                    scores.append(row["score"])
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                row.update(grading="error", error_type=type(error).__name__)
        records.append(row)
    complete = len(scores) == len(samples)
    summary = {"status": "scored" if complete else "incomplete", "total_score": round(sum(scores), 6),
               "possible_score": len(samples), "scored_samples": len(scores), "generated_samples": generated,
               "expected_samples": len(samples), "samples": records}
    run.save_json(batch / "summary.json", summary)
    settings["status"] = summary["status"]
    run.save_json(batch / "batch.json", settings)
    print(f"Total score: {summary['total_score']:.6f}; scored {len(scores)}/{len(samples)} samples: {batch / 'summary.json'}")
    return 0 if complete else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--image", required=True)
    generate_parser.add_argument("--model", required=True)
    selection = generate_parser.add_mutually_exclusive_group()
    selection.add_argument("--items")
    selection.add_argument("--limit", type=positive)
    selection.add_argument("--indices", help="One-based indices or ranges in dataset order, or 'all'")
    generate_parser.add_argument("--workers", type=positive, default=1)
    generate_parser.add_argument("--samples", type=positive, default=1)
    generate_parser.add_argument("--timeout", type=positive, default=run.DEFAULT_TIMEOUT_SECONDS)
    generate_parser.add_argument("--judge-model", default=run.JUDGE_MODEL)
    generate_parser.add_argument("--judge-effort", default=run.JUDGE_EFFORT)
    grade_parser = subparsers.add_parser("grade")
    grade_parser.add_argument("batch", type=Path)
    grade_parser.add_argument("--model-config", type=Path, default=run.ADAPT / "model_conf.txt",
                              help="Legacy profile config for batches created by the old launcher")
    args = parser.parse_args()
    try:
        return generate(args) if args.command == "generate" else grade_batch(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"FormalTCS batch: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
