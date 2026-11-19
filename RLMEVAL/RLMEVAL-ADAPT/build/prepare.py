"""Materialize pinned task bundles and a host-local scoring dataset."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ADAPT = HERE.parent
sys.path.insert(0, str(ADAPT.parent / "RLMEval/src"))
from rlm_pipeline.inputs import export_item


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deps", type=Path, default=ADAPT.parent / "dependencies")
    root = parser.parse_args().deps.resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(line) for line in (ADAPT.parent / "RLMEval/data/rlm25_pfr_tlb.jsonl").read_text().splitlines()]
    items = {row["index"]: row for row in rows}
    bundles = root / "题目详细信息"
    checked = {}
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        stage = Path(tmp) / "tasks"
        shutil.copytree(HERE / "manifests", stage)
        for manifest in sorted(stage.glob("*/tasks/*/*/dependencies.json")):
            lock = json.loads(manifest.read_text())
            item = items[lock["item_index"]]
            project = root / "projects" / item["repo"].lower()
            toolchain = (project / "lean-toolchain").read_text()
            if toolchain.strip() != lock["toolchain"]:
                raise ValueError(f"Toolchain mismatch: {project}")
            task = manifest.parent
            generated = Path(tmp) / "item"
            export_item(item, generated)
            for file in generated.iterdir():
                file.rename(task / file.name)
            generated.rmdir()
            (task / "lean-toolchain").write_text(toolchain.strip() + "\n")
            (task / "lib").mkdir()
            for relative, digest in lock["files"].items():
                source = project / ".lake/build" / relative
                if source not in checked:
                    checked[source] = hashlib.sha256(source.read_bytes()).hexdigest()
                if checked[source] != digest:
                    raise ValueError(f"Compiled dependency checksum mismatch: {source}")
                dest = task / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, dest)
        if bundles.exists():
            raise FileExistsError(f"Choose an empty build destination: {bundles}")
        stage.rename(bundles)
    for row in rows:
        row["project_dir"] = str(root / "projects" / row["repo"].lower())
    (root / "dataset.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    print(f"Prepared {len(list(bundles.glob('*/tasks/*/*/dependencies.json')))} tasks from {len(checked)} distinct modules")


if __name__ == "__main__":
    main()
