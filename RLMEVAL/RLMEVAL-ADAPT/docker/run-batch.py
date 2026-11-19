"""Host orchestrator for isolated, single-task RLMEval generation containers."""
from concurrent.futures import ThreadPoolExecutor
import argparse
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass_environment
import uuid

from rlm_agent.items import load_items, parse_indices

ADAPT = Path(__file__).resolve().parents[1]
RLMEVAL = ADAPT.parent / "RLMEval"
PROXY_IMAGE = "python:3.12-slim-bookworm"
RUNTIME_VARS = ("BACKEND", "MODEL", "SAMPLES", "TMO", "MAX_COMPILE_ATTEMPTS",
                "REASONING_EFFORT")
CREDENTIALS = {
    "codex": ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
    "claude": ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
}


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def prepare_output(path):
    path.mkdir()
    if os.getuid() == 0:
        os.chown(path, 1000, 1000)
    elif os.getuid() != 1000:
        raise ValueError("Run as root or UID 1000 so the benchmark user can write the output mount")


def run_task(item, bundle, run, image, network, proxy, settings, prepare_only):
    case = run / "tasks" / str(item.index)
    case.mkdir()
    row = item.to_row()
    row["project_dir"] = "/workspace/project"
    # The proof runner only needs the released single-task context and declaration.
    row.update(context_normal="", formal_stmt="", gt_proof_len=0)
    item_file = case / "item.jsonl"
    item_file.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    output = case / "output"
    prepare_output(output)
    command = ["run", "--pull=never", "--rm", "--init", "--read-only", "--cap-drop=ALL",
               "--security-opt", "no-new-privileges", "--pids-limit", "256",
               "--memory", "8g", "--cpus", "2",
               "--mount", f"type=bind,src={bundle},dst=/input/task,readonly",
               "--mount", f"type=bind,src={item_file},dst=/input/item.jsonl,readonly",
               "--mount", f"type=bind,src={output},dst=/output"]
    for path, size in (("/workspace", "1g"), ("/cache_dir", "512m"),
                       ("/home/benchmark", "512m"), ("/tmp", "512m")):
        command += ["--tmpfs", f"{path}:rw,uid=1000,gid=1000,mode=0700,size={size}"]
    # lean-interact creates this directory on import, even during generation.
    command += ["--tmpfs", "/usr/local/lib/python3.12/site-packages/lean_interact/cache"
                            ":rw,uid=1000,gid=1000,mode=0700,size=64m"]
    env = os.environ.copy()
    if prepare_only:
        command += ["--network", "none", image, "--prepare-only"]
    else:
        command += ["--network", network, "--dns", "127.0.0.1"]
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[name] = proxy
        env.update(NO_PROXY="", no_proxy="")
        backend_vars = ("CLAUDE_CODE_MAX_OUTPUT_TOKENS",) if settings["BACKEND"] == "claude" else ()
        keys = (*RUNTIME_VARS, *backend_vars, *CREDENTIALS[settings["BACKEND"]],
                "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
        env.update(settings)
        if settings["BACKEND"] == "codex":
            env["OPENAI_BASE_URL"] = proxy + "/v1"
            env["NO_PROXY"] = env["no_proxy"] = urlsplit(proxy).hostname
        for name in keys:
            if name in env:
                command += ["-e", name]
        command += [image]
    with (case / "container.log").open("w") as log:
        print(f"[{item.index}] container started", flush=True)
        result = subprocess.run(["docker", *command], env=env, stdout=log, stderr=subprocess.STDOUT)
    print(f"[{item.index}] container exit {result.returncode}", flush=True)
    return output / "generation", result.returncode


def translate_path(value, directory):
    if not value:
        return None
    source = (directory / Path(value).relative_to("/output/generation")).resolve()
    if not source.is_relative_to(directory.resolve()) or not source.is_file():
        raise ValueError(f"Invalid container artifact path: {value}")
    return source


def collect(run, jobs, samples):
    diagnostics = []
    for name in ("candidates", "logs"):
        (run / name).mkdir()
    with (run / "generations.jsonl").open("w") as combined:
        for task, (directory, exit_code) in jobs:
            source = directory / "generations.jsonl"
            records = {}
            if source.is_file() and source.resolve().is_relative_to(directory.parent.resolve()):
                for line in source.read_text().splitlines():
                    row = json.loads(line)
                    sample = row["sample_id"]
                    if row["item_index"] != task.index or not 0 <= sample < samples or sample in records:
                        raise ValueError(f"Unexpected generation identity for task {task.index}")
                    records[sample] = row
            diagnostics.append({"item_index": task.index, "container_exit_code": exit_code,
                                "missing_sample_ids": sorted(set(range(samples)) - records.keys())})
            for sample, row in sorted(records.items()):
                row["run_id"] = run.name
                for key in ("candidate_path", "events_path"):
                    value = row.get(key)
                    if not value:
                        continue
                    source = translate_path(value, directory)
                    if key == "candidate_path":
                        destination = run / "candidates" / f"{task.index:06d}-{sample:04d}" / "Candidate.lean"
                        destination.parent.mkdir()
                    else:
                        destination = run / "logs" / source.name
                    destination.write_bytes(source.read_bytes())
                    row[key] = str(destination)
                    if key == "events_path":
                        stderr = source.with_name(source.name.removesuffix(".events.jsonl") + ".stderr.log")
                        if stderr.is_file() and stderr.resolve().is_relative_to(directory.resolve()):
                            (run / "logs" / stderr.name).write_bytes(stderr.read_bytes())
                if row.get("candidate_path"):
                    metadata = {key: value for key, value in row.items() if key != "raw_text"}
                    Path(row["candidate_path"]).with_name("generation-metadata.json").write_text(
                        json.dumps(metadata, ensure_ascii=False) + "\n")
                combined.write(json.dumps(row, ensure_ascii=False) + "\n")
    (run / "container-results.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
    return diagnostics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true", help="Test bundled Lean imports with Docker networking disabled")
    args = parser.parse_args()
    settings = {name: os.environ[name] for name in RUNTIME_VARS if name in os.environ}
    backend = settings.setdefault("BACKEND", "codex")
    if backend not in CREDENTIALS:
        parser.error("BACKEND must be codex or claude")
    codex_limit = os.environ.get("CODEX_MAX_OUTPUT_TOKENS", "131072")
    if backend == "codex" and not re.fullmatch(r"[1-9][0-9]*", codex_limit):
        parser.error("CODEX_MAX_OUTPUT_TOKENS must be a positive integer")
    samples = int(settings.setdefault("SAMPLES", "1"))
    concurrency = int(os.environ.get("CONCURRENCY", "1"))
    if min(samples, concurrency) < 1:
        parser.error("SAMPLES and CONCURRENCY must be positive")
    if not args.prepare_only:
        url_name, *credentials = CREDENTIALS[backend]
        if not settings.get("MODEL") or not os.environ.get(url_name) or not any(os.environ.get(key) for key in credentials):
            parser.error(f"Set MODEL, {url_name} and a credential for {backend}")
        endpoint = os.environ[url_name]

    dataset = ADAPT / os.environ.get("DATASET", str(Path(os.environ["RLMEVAL_DEPS"]) / "dataset.jsonl"))
    items = load_items(dataset)
    bundles = {
        index: Path(os.environ["RLMEVAL_DEPS"]) / "题目详细信息" / item.repo / "tasks" / item.repo / str(index)
        for index, item in items.items()
        if item.repo in {"PFR", "TLB"}
    }
    available = [index for index, bundle in bundles.items() if (bundle / "item.json").is_file()]
    indices = parse_indices(os.environ.get("INDICES", "166,174"), available)
    if not indices:
        parser.error("Select at least one task index")
    image = os.environ.get("RLMEVAL_DOCKER_IMAGE", "rlmeval-native:portable")
    docker("image", "inspect", image, stdout=subprocess.DEVNULL)
    run_id = os.environ.get("RUN_ID", f"docker-{backend}-{uuid.uuid4().hex[:12]}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        parser.error("Invalid RUN_ID")
    run = ADAPT / os.environ.get("RUNS_DIR", "runs") / run_id
    (run / "tasks").mkdir(parents=True, exist_ok=False)
    print(f"Run directory {run}", flush=True)

    suffix = uuid.uuid4().hex[:12]
    network = f"rlmeval-model-{suffix}"
    uplink = f"rlmeval-uplink-{suffix}"
    proxy = f"rlmeval-proxy-{suffix}"
    networks = []
    proxy_url = None
    try:
        if not args.prepare_only:
            docker("network", "create", "--internal", "--opt",
                   "com.docker.network.bridge.gateway_mode_ipv4=isolated", network, stdout=subprocess.DEVNULL)
            networks.append(network)
            docker("network", "create", uplink, stdout=subprocess.DEVNULL)
            networks.append(uplink)
            upstream = os.environ.get("RLMEVAL_UPSTREAM_PROXY")
            if upstream is None:
                upstream = "" if proxy_bypass_environment(urlsplit(endpoint).hostname) else getproxies().get(urlsplit(endpoint).scheme, "")
            docker("run", "--pull=never", "-d", "--name", proxy, "--network", network, "--network", uplink,
                   "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
                   "--user", "65534:65534",
                   "-e", "RLMEVAL_UPSTREAM_PROXY",
                   "--mount", f"type=bind,src={Path(__file__).with_name('api-proxy.py')},dst=/proxy.py,readonly",
                   PROXY_IMAGE, "python", "/proxy.py", endpoint, "0.0.0.0",
                   *(["--codex-limit", codex_limit] if backend == "codex" else []),
                   env=dict(os.environ, RLMEVAL_UPSTREAM_PROXY=upstream), stdout=subprocess.DEVNULL)
            for attempt in range(10):
                ready = subprocess.run(
                    ["docker", "exec", proxy, "python", "-c",
                     "import socket; socket.create_connection(('127.0.0.1', 18080), 1).close()"],
                    capture_output=True,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError(f"Model proxy failed to start: {ready.stderr.decode().strip()}")
            # Codex's HTTP client cannot resolve the bridge alias with external DNS disabled.
            address = docker("inspect", "--format",
                             '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}',
                             proxy, capture_output=True, text=True).stdout.strip()
            proxy_url = f"http://{IPv4Address(address)}:18080"
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(run_task, items[index], bundles[index], run, image, network,
                                       proxy_url, settings, args.prepare_only)
                       for index in indices]
            jobs = [(items[index], future.result()) for index, future in zip(indices, futures)]
    finally:
        if networks:
            subprocess.run(["docker", "rm", "-f", proxy], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for name in reversed(networks):
                docker("network", "rm", name, stdout=subprocess.DEVNULL)
    if args.prepare_only:
        return int(any(code for _, (_, code) in jobs))
    diagnostics = collect(run, jobs, samples)
    if not (run / "generations.jsonl").stat().st_size:
        print(f"No generation records. See {run / 'tasks'} for container logs.", file=sys.stderr)
        return 1
    subprocess.run([sys.executable, "-m", "rlm_agent.cli", "score", "--dataset", str(dataset),
                    "--task", "proof", "--input", str(run / "generations.jsonl"),
                    "--output", str(run / "scores.jsonl")], check=True)
    subprocess.run([sys.executable, str(RLMEVAL / "scripts/summarize_codex_run.py"),
                    "--dataset", str(dataset), "--run-id", run_id, "--artifacts-dir", str(run.parent),
                    "--output-dir", str(run), "--expected-samples", str(samples)], check=True)
    return int(any(task["container_exit_code"] or task["missing_sample_ids"] for task in diagnostics))


if __name__ == "__main__":
    sys.exit(main())
