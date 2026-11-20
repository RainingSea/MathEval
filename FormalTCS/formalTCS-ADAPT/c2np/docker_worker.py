"""Run Claude in a single container; stream only redacted progress and the final result."""

import json
import subprocess
import sys
from pathlib import Path

from run import MAX_OUTPUT_TOKENS_ENV, PROMPT, claude_command, parse_events


def emit(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def main():
    config = json.loads(sys.stdin.readline())
    key = config["key"]
    for directory in ("home", "config", "cache", "tmp"):
        (Path("/runtime") / directory).mkdir()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/runtime/home", "TMPDIR": "/runtime/tmp",
        "CLAUDE_CONFIG_DIR": "/runtime/config", "XDG_CACHE_HOME": "/runtime/cache", "LANG": "C.UTF-8",
        "ANTHROPIC_BASE_URL": config["base_url"], "ANTHROPIC_AUTH_TOKEN": key,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    if config.get("max_output_tokens") is not None:
        env[MAX_OUTPUT_TOKENS_ENV] = str(config["max_output_tokens"])
    emit("started")
    result = {}
    with subprocess.Popen(claude_command("/usr/local/bin/claude", config["model"]), cwd="/workspace", env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace") as process:
        try:
            with process.stdin as prompt:
                prompt.write(PROMPT)
        except BrokenPipeError:
            pass
        for line in process.stdout:
            clean = line.replace(key, "<REDACTED>")
            received, trace = parse_events(clean)
            for entry in trace:
                emit("trace", payload=entry)
            if received:
                result = received
            elif clean.strip() and not clean.lstrip().startswith("{"):
                emit("diagnostic", text=clean.strip()[:1000])
        exit_code = process.wait()
    emit("result", payload=result, claude_exit_code=exit_code)
    return 0 if exit_code == 0 and result.get("is_error") is False else 1


if __name__ == "__main__":
    raise SystemExit(main())
