"""Adapt the local JSONL dataset and artifact format to RLMEval generation."""

from __future__ import annotations

import json
import os
import re
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import litellm

from scripts import eval_proof_autoformalization as proof_eval
from scripts import eval_statement_autoformalization as statement_eval

from .items import RlmItem
from .runner import MAX_CANDIDATE_BYTES, utc_now


def _generate(item: RlmItem, output: Path, args: Namespace) -> tuple[str, dict]:
    def chat_completion(**request):
        request["model"] = "openai/" + args.model
        request.update(caching=False, num_retries=0, stream=False)
        if args.timeout_seconds > 0:
            request["timeout"] = args.timeout_seconds
        if args.reasoning_effort:
            request["reasoning_effort"] = args.reasoning_effort
        return litellm.completion(**request)

    context = item.context_normal if args.task == "proof-normal" else item.context
    node = {"label": item.label, "processed_text": item.informal_stmt, "stmt_type": "theorem"}
    declaration = {"file": item.file, "name": item.name, "start_idx": len(context), "theorem_info": {}}
    node["lean_declarations"] = [declaration]
    common = dict(
        node=node,
        blueprint_graph=None,
        lean_declarations=[],
        output_folder=str(output),
        verbose=False,
        nb_attempts=1,
        temperature=0.0,
        top_p=0.95,
        model="openai/" + args.model,
        use_chat_prompt=True,
        stopwords=[],
        api_key=os.environ.get(args.api_key_env) or "EMPTY",
        api_base_url=args.base_url,
        max_total_tokens=args.max_context_tokens,
        max_generated_tokens=args.max_tokens,
        chat_completion=chat_completion,
    )
    if args.task == "statement":
        predictions, _, _ = statement_eval._formalize_node(
            lean_files={item.file: context},
            prompt_context=statement_eval.PromptContext.FILE_CONTEXT,
            **common,
        )
        candidate = predictions[0] or ""
    else:
        # The exported declaration ends in ':= by'. The official prompt adds it.
        signature = re.sub(r"\s*:=\s*by\s*$", "", item.decl).rstrip()
        if not signature:
            raise ValueError("Missing theorem declaration")
        source = context.rstrip() + "\n" + signature
        declaration["theorem_info"]["proof_start_idx"] = len(source)
        node["proof"] = {"text": item.informal_proof}
        predictions, _, _ = proof_eval._prove_node(
            lean_files={item.file: source},
            prompt_context=proof_eval.PromptContext.FILE_CONTEXT,
            nl_proof_hint=bool(item.informal_proof),
            **common,
        )
        prediction = predictions[0] or ""
        candidate = _proof_candidate(signature, prediction) if prediction.strip() else ""

    completion_path = output / "raw_completion.json"
    response = json.loads(completion_path.read_text(encoding="utf-8")) if completion_path.exists() else {}
    return candidate, response


def _proof_candidate(signature: str, prediction: str) -> str:
    # Keep full declarations for the scorer to validate if a model echoes one.
    if re.match(r"\s*(?:theorem|lemma)\b", prediction):
        return prediction
    proof = re.match(r"\s*:=\s*(.*)", prediction, re.DOTALL)
    if proof:
        body = proof[1].rstrip()
        # Official first-line normalization may indent the ':=' line.
        return f"{signature} := {body}"
    if re.match(r"\s*by", prediction):
        return f"{signature} := {prediction.lstrip()}"
    lines = prediction.rstrip().splitlines()
    if len(lines) == 1:
        lines[0] = "  " + lines[0].lstrip()
    return signature + " := by\n" + "\n".join(lines)


def _run_sample(item: RlmItem, sample_id: int, run_dir: Path, args: Namespace) -> dict:
    output = run_dir / "logs" / f"{item.index:06d}-{sample_id:04d}"
    workspace = Path(args.workspaces_dir).resolve() / args.run_id / output.name
    started_at = utc_now()
    started = time.monotonic()
    status = "ok"
    error = None
    candidate = ""
    usage = None
    raw_text = None
    truncated = False
    try:
        output.mkdir(parents=True)
        candidate, response = _generate(item, output, args)
        usage = response.get("usage")
        choices = response.get("choices") or []
        raw_text = choices[0]["message"]["content"] if choices else None
        truncated = len(candidate.encode("utf-8")) > MAX_CANDIDATE_BYTES
        if truncated:
            candidate = ""
            error = "candidate exceeded MAX_CANDIDATE_BYTES"
        if not candidate:
            status = "model_error"
            error = error or "Official generation produced no usable prediction"
        else:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "Candidate.lean").write_text(candidate + "\n", encoding="utf-8")
    except (litellm.Timeout, TimeoutError) as exc:
        status, error = "model_timeout", str(exc)
    except Exception as exc:
        status, error = "model_error", f"{type(exc).__name__}: {exc}"

    completion = output / "raw_completion.json"
    return {
        "run_id": args.run_id,
        "track": "direct_model",
        "item_index": item.index,
        "item_name": item.label,
        "item_repo": item.repo,
        "sample_id": sample_id,
        "model": args.model,
        "status": status,
        "exit_code": int(status != "ok"),
        "termination_reason": "timeout" if status == "model_timeout" else None,
        "error_detail": error,
        "started_at": started_at,
        "finished_at": utc_now(),
        "wall_ms": int((time.monotonic() - started) * 1000),
        "workspace": str(workspace) if workspace.is_dir() else "",
        "events_path": str(completion) if completion.exists() else "",
        "compile_attempts": 0,
        "output_truncated": truncated,
        "raw_text": candidate if status == "ok" else "",
        "agent_final_message": raw_text,
        "usage": usage,
    }


def run_direct(items: list[RlmItem], args: Namespace) -> Path:
    if min(args.concurrency, args.samples) < 1:
        raise ValueError("--concurrency and --samples must be positive")
    if not 0 < args.max_tokens < args.max_context_tokens:
        raise ValueError("Output token limit must be positive and below the context token limit")
    run_dir = Path(args.artifacts_dir).resolve() / args.run_id
    generations = run_dir / "generations.jsonl"
    run_dir.mkdir(parents=True, exist_ok=True)
    with generations.open("x", encoding="utf-8") as handle, ThreadPoolExecutor(args.concurrency) as pool:
        futures = [
            pool.submit(_run_sample, item, sample_id, run_dir, args)
            for item in items
            for sample_id in range(args.samples)
        ]
        for future in as_completed(futures):
            handle.write(json.dumps(future.result(), ensure_ascii=False) + "\n")
            handle.flush()
    return generations
