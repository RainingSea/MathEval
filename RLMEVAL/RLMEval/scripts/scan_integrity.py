#!/usr/bin/env python3
"""Post-run integrity scan (isolation policy, 2026-10-31).

背景:评测期间参考答案源码已挪出评测树(/data/projects/exp_results/answer-vault),
但编译用的 .olean 里仍包含参考证明。残留的窃取通道是:
  1. agent 违规改工作区 Context.lean(TASK.md 只允许改 Candidate.lean),
     塞 `#print 全限定名` 让 check_candidate.sh 打出参考证明项;
  2. Candidate.lean 本身塞 `#eval ...`(#eval 可在 elaboration 期执行
     IO.FS.readFile = 任意文件读,能直接读 vault)或 `#print`。

本脚本在 run 结束后逐工作区核查:
  - Context.lean / .context-full.lean 必须与数据集中某条目的
    context / context_normal 逐字节一致(md5 匹配;不匹配=被改动);
  - Candidate.lean 出现行首 #print / #eval / #reduce 即标记;
  - generations.jsonl 的 raw_text(最终答案)同样扫描。

这是事后信号(供报告/复核),不是阻断;命中不改变评分,只写入 integrity.json。
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 只认行首(允许缩进)的命令形式,减少字符串/注释里的误报
SUSPECT = re.compile(r"^\s*#(print|eval|reduce)\b", re.M)

# 事件日志(logs/*.events.jsonl)里的行为标记:命名空间/魔法链接探测、答案区路径、
# 答案源码的"路径形式"(import 是点号 TestingLowerBounds.X;斜杠形式=文件访问)。
# 2026-10-31 /proc 取证:遮蔽层挡不住 root 走 /proc/<pid>/root 或 nsenter,
# 提示词禁令(不绕过)是协议层,这里的事件扫描是检测层。
EVENT_MARKERS = (
    "nsenter",
    "/proc/",
    "exp_results",
    "answer-vault",
    "TestingLowerBounds/",
    "PFR/",
)


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def suspect_hits(text: str):
    hits = []
    for m in SUSPECT.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = len(text)
        hits.append({"line": line_no, "text": text[m.start():line_end][:120]})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--artifacts-dir", default="artifacts")
    ap.add_argument("--workspaces-dir", default="agent_workspaces")
    ap.add_argument("--output", help="integrity.json 输出路径(默认打印到 stdout)")
    args = ap.parse_args()

    # md5(context/context_normal) -> (index, field)
    ctx_index = {}
    with open(args.dataset, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            for field in ("context", "context_normal"):
                val = item.get(field)
                if isinstance(val, str) and val:
                    ctx_index[md5_bytes(val.encode("utf-8"))] = {"index": item["index"], "field": field}

    report = {
        "run_id": args.run_id,
        "workspaces": [],
        "generations_suspect": [],
        "events_suspect": [],
        "counts": {"workspaces": 0, "context_ok": 0, "context_mismatch": 0, "candidate_suspect": 0},
    }

    ws_parent = Path(args.workspaces_dir) / args.run_id
    if not ws_parent.is_dir():
        # 兼容归档布局:调用方直接传 runs/<id>/agent_workspaces 本体
        alt = Path(args.workspaces_dir)
        if alt.is_dir() and any(alt.iterdir()):
            ws_parent = alt
    if ws_parent.is_dir():
        for ws in sorted(ws_parent.iterdir()):
            if not ws.is_dir():
                continue
            entry = {"workspace": ws.name, "contexts": [], "candidate_suspect": []}
            for name in ("Context.lean", ".context-full.lean"):
                path = ws / name
                if not path.is_file():
                    continue
                digest = md5_bytes(path.read_bytes())
                match = ctx_index.get(digest)
                if match:
                    entry["contexts"].append({"file": name, "match": match})
                    report["counts"]["context_ok"] += 1
                else:
                    entry["contexts"].append({"file": name, "match": None, "tampered": True})
                    report["counts"]["context_mismatch"] += 1
            cand = ws / "Candidate.lean"
            if cand.is_file():
                hits = suspect_hits(cand.read_text(encoding="utf-8", errors="replace"))
                if hits:
                    entry["candidate_suspect"] = hits
                    report["counts"]["candidate_suspect"] += 1
            report["workspaces"].append(entry)
            report["counts"]["workspaces"] += 1

    gen_path = Path(args.artifacts_dir) / args.run_id / "generations.jsonl"
    if not gen_path.is_file():
        alt = Path(args.artifacts_dir) / "generations.jsonl"
        if alt.is_file():
            gen_path = alt
    if gen_path.is_file():
        with open(gen_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                raw = rec.get("raw_text") or ""
                idx = rec.get("index")
                for hit in suspect_hits(raw):
                    report["generations_suspect"].append({"index": idx, **hit})

    # 事件日志行为扫描(两条轨道都落盘 logs/<token>.events.jsonl)。
    # 2026-10-31 修正:逐行解析后剔除 thinking 块再计数——工具通道(tool_use 入参、
    # tool_result 回显、codex 式命令字段)全保留;模型在思考里凭训练记忆"聊到"
    # 公开库名/路径(如 GLM 回忆 TestingLowerBounds 源码)不是访问,不算违规。
    # 行解析失败则按原文整行扫(宁可误报不可漏报)。
    logs_dir = Path(args.artifacts_dir) / args.run_id / "logs"
    if not logs_dir.is_dir():
        logs_dir = Path(args.artifacts_dir) / "logs"
    if logs_dir.is_dir():
        for ev in sorted(logs_dir.glob("*.events.jsonl")):
            hits: dict[str, int] = {}

            def _count(text: str) -> None:
                for marker in EVENT_MARKERS:
                    n = text.count(marker)
                    if n:
                        hits[marker] = hits.get(marker, 0) + n

            try:
                with open(ev, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            content = ((rec.get("message") or {}).get("content"))
                            if isinstance(content, list):
                                kept = [
                                    b for b in content
                                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                                ]
                                (rec["message"])["content"] = kept
                            _count(json.dumps(rec, ensure_ascii=False))
                        except (json.JSONDecodeError, AttributeError, TypeError):
                            _count(line)
            except OSError:
                continue
            if hits:
                report["events_suspect"].append({"file": ev.name, "markers": hits})

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)

    c = report["counts"]
    print(
        f"integrity: workspaces={c['workspaces']} context_mismatch={c['context_mismatch']} "
        f"candidate_suspect={c['candidate_suspect']} generations_suspect={len(report['generations_suspect'])} "
        f"events_suspect_files={len(report['events_suspect'])}",
        file=sys.stderr,
    )
    if (
        c["context_mismatch"]
        or c["candidate_suspect"]
        or report["generations_suspect"]
        or report["events_suspect"]
    ):
        print("VIOLATIONS FOUND - inspect integrity.json / integrity field before trusting this run", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
