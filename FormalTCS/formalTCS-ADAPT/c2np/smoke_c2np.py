"""Check the pinned upstream C2NP contract offline; not a benchmark run."""

import json
import platform
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def require(condition, message):
    if not condition:
        raise SystemExit(f"C2NP smoke check failed: {message}")


def main(upstream, local_root, revision):
    upstream = upstream.resolve()
    evaluator = upstream / "evaluate"
    sys.path[:0] = [str(evaluator), str(upstream)]
    import dataset
    from grade import rubric

    items = dataset.load_items(upstream / "benchmark")
    require(len(items) == len({item["id"] for item in items}) == 143, "expected 143 unique items")
    item = items[0]
    brief = (evaluator / "prompts" / "c2np.md").read_text(encoding="utf-8")
    config = {"lean_root": None, "lean_jobs": 1}
    fixture = "Offline smoke-test fixture; not a mathematical proof."
    judge_reply = {
        "choices": [{"message": {"content": json.dumps({
            "logic": 1, "complete": 0.5, "correct": 0, "clear": 0.5, "comment": "fixture",
        })}}],
    }

    with TemporaryDirectory(prefix="c2np-", dir=local_root / "tmp") as scratch:
        scratch = Path(scratch)
        workspace = scratch / "workspace"
        fingerprint = dataset.prepare_workspace(config, item, "c2np", workspace, instructions=brief)
        files = {path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file()}
        require(files == {"TASK.md", "input/nl_claim.md", "input/theorem.lean"}, "unexpected model input files")
        require((workspace / "input/nl_claim.md").read_text(encoding="utf-8") == item["nl_theorem"].strip() + "\n",
                "natural-language input differs from upstream")
        require((workspace / "input/theorem.lean").read_text(encoding="utf-8") == item["theorem_source"],
                "Lean input differs from upstream")
        require(dataset.read_output(workspace, "c2np") is None, "workspace already contains an answer")
        output = workspace / "output/nl_proof.md"
        output.write_text(fixture + "\n", encoding="utf-8")
        response = dataset.read_output(workspace, "c2np")
        require(response == fixture, "could not read the proof deliverable")

        with patch.object(rubric, "_request", return_value=judge_reply) as request:
            result = rubric.score(
                judge={"attempts": 1}, base_url="", api_key="", task="c2np", item=item, response=response,
                weights={"logic": 0.4, "complete": 0.3, "correct": 0.2, "clear": 0.1},
            )
        require(request.call_count == 1, "expected one mocked judge request")
        judge_prompt = request.call_args.args[3]
        require(item["nl_proof"].strip() in judge_prompt and fixture in judge_prompt,
                "judge did not receive both the reference and the candidate")
        require(result["status"] == "scored" and result["score"] == 0.6, "wrong mocked rubric result")

        report = json.dumps({
            "status": "passed",
            "dataset": "formaltcs",
            "task": "c2np",
            "upstream_commit": revision,
            "python": platform.python_version(),
            "items_validated": len(items),
            "smoke_item": item["id"],
            "input_sha256": fingerprint,
            "judge": "mocked",
            "network_calls": 0,
        }, indent=2)
        staged_report = scratch / "c2np-smoke.json"
        staged_report.write_text(report + "\n", encoding="utf-8")
        staged_report.replace(local_root / staged_report.name)
    print(report)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
