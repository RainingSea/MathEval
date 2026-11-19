from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from .items import build_items, load_items, parse_indices

DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "data" / "rlm25_pfr_tlb.jsonl"


def _cmd_items(args: argparse.Namespace) -> None:
    override = {}
    if args.project_map:
        for pair in args.project_map.split(","):
            name, path = pair.split("=", 1)
            override[name] = path
    items = build_items(Path(args.config), project_override=override or None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item.to_row(), ensure_ascii=False) + "\n")
    per_repo = Counter(item.repo for item in items)
    print(f"wrote {len(items)} items to {out}: {dict(per_repo)}")


def _cmd_run_direct(args: argparse.Namespace) -> None:
    items_map = load_items(Path(args.dataset))
    indices = parse_indices(args.indices, list(items_map))
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from .direct import run_direct

    generations = run_direct([items_map[i] for i in indices], args)
    print(f"generations: {generations}")


def _cmd_run_codex(args: argparse.Namespace) -> None:
    items_map = load_items(Path(args.dataset))
    indices = parse_indices(args.indices, list(items_map))
    from .runner import RunConfig, run_codex
    import asyncio

    config = RunConfig(
        task=args.task,
        model=args.model,
        codex_bin=args.codex_bin,
        remove_workspaces=args.remove_workspaces,
        timeout_seconds=args.timeout_seconds,
        max_compile_attempts=args.max_compile_attempts,
        reasoning_effort=args.reasoning_effort,
        artifacts_dir=Path(args.artifacts_dir).resolve(),
        workspaces_dir=Path(args.workspaces_dir).resolve(),
        concurrency=args.concurrency,
        samples=args.samples,
    )
    generations = asyncio.run(
        run_codex(
            [items_map[i] for i in indices],
            args.run_id,
            config,
            cache_home=Path(args.cache_dir),
            elan_home=Path(args.elan_home),
        )
    )
    print(f"generations: {generations}")


def _cmd_run_claude(args: argparse.Namespace) -> None:
    auth_token = ""
    if args.anthropic_auth_token_env:
        auth_token = os.environ.get(args.anthropic_auth_token_env, "")
        if not auth_token.strip():
            raise ValueError(f"Missing token environment variable: {args.anthropic_auth_token_env}")
    items_map = load_items(Path(args.dataset))
    indices = parse_indices(args.indices, list(items_map))
    if args.reverse_order:
        indices.reverse()
    from .claude_runner import ClaudeRunConfig, run_claude
    import asyncio

    config = ClaudeRunConfig(
        task=args.task,
        model=args.model,
        claude_bin=args.claude_bin,
        remove_workspaces=args.remove_workspaces,
        timeout_seconds=args.timeout_seconds,
        max_compile_attempts=args.max_compile_attempts,
        reasoning_effort=args.reasoning_effort,
        artifacts_dir=Path(args.artifacts_dir).resolve(),
        workspaces_dir=Path(args.workspaces_dir).resolve(),
        concurrency=args.concurrency,
        samples=args.samples,
        anthropic_base_url=args.anthropic_base_url,
        api_key=args.anthropic_api_key,
        auth_token=auth_token,
    )
    generations = asyncio.run(
        run_claude(
            [items_map[i] for i in indices],
            args.run_id,
            config,
            cache_home=Path(args.cache_dir),
            elan_home=Path(args.elan_home),
        )
    )
    print(f"generations: {generations}")


def _cmd_score(args: argparse.Namespace) -> None:
    items_map = load_items(Path(args.dataset))
    records = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    from .scoring import score_records

    counts = score_records(records, items_map, Path(args.output), task=args.task)
    total = sum(counts.values())
    certified = counts.get("certified", 0) + counts.get("passed", 0)
    print(f"summary: {counts}")
    if total:
        print(f"pass rate: {certified}/{total} = {certified / total:.1%}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rlmv-eval", description="RLM25 codex-agent statement formalization")
    sub = parser.add_subparsers(dest="cmd", required=True)

    items_cmd = sub.add_parser("items", help="Build the item dataset from traced_repos")
    items_cmd.add_argument("--config", required=True)
    items_cmd.add_argument("--out", default=str(DEFAULT_DATASET))
    items_cmd.add_argument("--project-map", default="", help="e.g. PFR=/data/.../pfr,TLB=/data/.../tlb")
    items_cmd.set_defaults(func=_cmd_items)

    run_cmd = sub.add_parser("run-codex", help="Run codex agent generation (ProofNet-Verified protocol)")
    run_cmd.add_argument("--model", required=True)
    run_cmd.add_argument("--dataset", default=str(DEFAULT_DATASET))
    run_cmd.add_argument("--indices", required=True, help="e.g. 1,2,3 or all")
    run_cmd.add_argument("--samples", type=int, default=1)
    run_cmd.add_argument("--concurrency", type=int, default=1)
    run_cmd.add_argument("--timeout-seconds", type=int, default=900)
    run_cmd.add_argument("--max-compile-attempts", type=int, default=3, help="0 = unlimited")
    run_cmd.add_argument("--reasoning-effort", default=None)
    run_cmd.add_argument("--no-output-schema", action="store_true", help=argparse.SUPPRESS)
    run_cmd.add_argument("--task", choices=["statement", "proof", "proof-normal"], default="statement")
    run_cmd.add_argument("--run-id", required=True)
    run_cmd.add_argument("--artifacts-dir", default="artifacts")
    run_cmd.add_argument("--workspaces-dir", default="agent_workspaces")
    run_cmd.add_argument("--remove-workspaces", action="store_true", help="Remove each temporary workspace after saving its candidate and logs")
    run_cmd.add_argument("--codex-bin", default="codex")
    run_cmd.add_argument("--elan-home", default="/data/projects/math-harness-eval/elan")
    run_cmd.add_argument("--cache-dir", default=".rlm-cache")
    run_cmd.set_defaults(func=_cmd_run_codex)

    claude_cmd = sub.add_parser("run-claude", help="Run Claude Code agent generation (same protocol as run-codex)")
    claude_cmd.add_argument("--model", required=True)
    claude_cmd.add_argument("--dataset", default=str(DEFAULT_DATASET))
    claude_cmd.add_argument("--indices", required=True, help="e.g. 1,2,3 or all")
    claude_cmd.add_argument("--reverse-order", action="store_true", help="Run selected indices in descending order")
    claude_cmd.add_argument("--samples", type=int, default=1)
    claude_cmd.add_argument("--concurrency", type=int, default=1)
    claude_cmd.add_argument("--timeout-seconds", type=int, default=900)
    claude_cmd.add_argument("--max-compile-attempts", type=int, default=3, help="0 = unlimited")
    claude_cmd.add_argument("--reasoning-effort", default=None)
    claude_cmd.add_argument("--task", choices=["statement", "proof", "proof-normal"], default="statement")
    claude_cmd.add_argument("--run-id", required=True)
    claude_cmd.add_argument("--artifacts-dir", default="artifacts")
    claude_cmd.add_argument("--workspaces-dir", default="agent_workspaces")
    claude_cmd.add_argument("--remove-workspaces", action="store_true")
    claude_cmd.add_argument("--claude-bin", default="claude")
    claude_cmd.add_argument("--anthropic-base-url", default="http://127.0.0.1:8102",
                            help="Local a2r adapter serving the Anthropic protocol")
    claude_cmd.add_argument("--anthropic-api-key", default=os.environ.get("ANTHROPIC_API_KEY") or "dummy-local-a2r",
                            help="Placeholder; the adapter holds the real upstream key")
    claude_cmd.add_argument("--anthropic-auth-token-env",
                            help="Read bearer token from this environment variable instead of using an API key")
    claude_cmd.add_argument("--elan-home", default="/data/projects/math-harness-eval/elan")
    claude_cmd.add_argument("--cache-dir", default=".rlm-cache")
    claude_cmd.set_defaults(func=_cmd_run_claude)

    direct_cmd = sub.add_parser("run-direct", help="Single-shot bare-model generation (no codex harness)")
    direct_cmd.add_argument("--model", required=True)
    direct_cmd.add_argument("--base-url", required=True, help="OpenAI-compatible base URL ending in /v1")
    direct_cmd.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Env var holding the API key")
    direct_cmd.add_argument("--max-tokens", type=int, default=393_216,
                            help="Maximum output tokens (default: 393216)")
    direct_cmd.add_argument("--reasoning-effort", default=None,
                            help="e.g. high/medium/low; omit or pass empty to disable reasoning")
    direct_cmd.add_argument("--no-stream", action="store_true", help=argparse.SUPPRESS)
    direct_cmd.add_argument("--dataset", default=str(DEFAULT_DATASET))
    direct_cmd.add_argument("--indices", required=True, help="e.g. 1,2,3 or all")
    direct_cmd.add_argument("--samples", type=int, default=1)
    direct_cmd.add_argument("--concurrency", type=int, default=1)
    direct_cmd.add_argument("--timeout-seconds", type=int, default=21_600, help="Request timeout in seconds (default: 21600)")
    direct_cmd.add_argument("--max-context-tokens", type=int, default=1_048_576)
    direct_cmd.add_argument("--max-compile-attempts", type=int, help=argparse.SUPPRESS)
    direct_cmd.add_argument("--task", choices=["statement", "proof", "proof-normal"], default="statement")
    direct_cmd.add_argument("--run-id", required=True)
    direct_cmd.add_argument("--artifacts-dir", default="artifacts")
    direct_cmd.add_argument("--workspaces-dir", default="agent_workspaces")
    direct_cmd.add_argument("--elan-home", help=argparse.SUPPRESS)
    direct_cmd.add_argument("--cache-dir", help=argparse.SUPPRESS)
    direct_cmd.set_defaults(func=_cmd_run_direct)

    score_cmd = sub.add_parser("score", help="Typecheck + BEq+ against ground truth")
    score_cmd.add_argument("--dataset", default=str(DEFAULT_DATASET))
    score_cmd.add_argument("--input", required=True)
    score_cmd.add_argument("--output", required=True)
    score_cmd.add_argument("--task", choices=["statement", "proof", "proof-normal"], default="statement")
    score_cmd.set_defaults(func=_cmd_score)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
