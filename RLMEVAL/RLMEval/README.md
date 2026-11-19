# RLMEval: Evaluating Research-Level Neural Theorem Proving & Autoformalization

This repository features the code for the evaluation of neural theorem proving and autoformalization methods on the RLM25 benchmark.

## Project setup

> [!IMPORTANT]
> Set the environment variables `GITHUB_ACCESS_TOKEN` (see [GitHub documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#personal-access-tokens-classic)) and `OPENAI_API_KEY`

### Local installation

Requirements:

- Python >= 3.10
- git
- [Lean 4](https://leanprover-community.github.io/get_started.html)

Install Python project:

    pip install -e .

## RLM25

Prepare the RLM25 dataset:

    python scripts/extract_benchmark.py  --config configs/benchmark/rlm25.yaml

Run statement autoformalization evaluation:

    python scripts/eval_statement_autoformalization.py --benchmark-config configs/benchmark/rlm25.yaml --model-config configs/models/gpt-4o_greedy.yaml

Run proof autoformalization evaluation:

    python scripts/eval_proof_autoformalization.py --benchmark-config configs/benchmark/rlm25.yaml --model-config configs/models_proof/gpt-4o_greedy.yaml

## Claude Code with Zhipu

The dedicated launcher reads the token from the `## 智谱API KEY` section at the
end of `secret.txt`. It uses an isolated Claude configuration, does not change
global settings, and does not pass the token on the command line. By default it
only runs a one-turn, tool-free connectivity test:

```bash
/root/miniconda3/envs/rlmeval/bin/python scripts/claude_zhipu_experiment.py
```

The default endpoint is `https://open.bigmodel.cn/api/anthropic`, with
`glm-5.2[1m]` for the main model and `glm-4.7` for the Haiku slot. Override the
main model with `--model` if needed. A successful connectivity test is required
before `--run` starts generation and scoring for the indices in
`run-logs/pfr-rerun-list.md`:

```bash
/root/miniconda3/envs/rlmeval/bin/python scripts/claude_zhipu_experiment.py \
  --run --run-id zhipu-pfr-smoke --indices 24 --timeout-seconds 300
```

Omit `--indices` to use the full rerun list. Concurrency defaults to one;
`--concurrency` overrides it. Benchmark execution retains the existing mount
namespace isolation and requires permission to run `unshare` and `mount`.
This launcher generates and scores only; use the existing integrity scan and
archive scripts before subsequent experiments.

## Citation

If you use this code or the RLM25 benchmark in your research, please consider citing one of the following papers. If you use the benchmark for evaluating automated theorem proving or proof autoformalization methods, please cite the first paper. If you use it for evaluating statement autoformalization methods, please cite the second paper.

[RLMEval: Evaluating Research-Level Neural Theorem Proving](https://arxiv.org/abs/2510.25427), EMNLP 2025 Findings

```bibtex
@misc{poiroux2025rlmevalevaluatingresearchlevelneural,
    title={RLMEval: Evaluating Research-Level Neural Theorem Proving}, 
    author={Auguste Poiroux and Antoine Bosselut and Viktor Kunčak},
    year={2025},
    eprint={2510.25427},
    archivePrefix={arXiv},
    primaryClass={cs.CL},
    url={https://arxiv.org/abs/2510.25427}, 
}
```

[Reliable Evaluation and Benchmarks for Statement Autoformalization](https://arxiv.org/abs/2406.07222), EMNLP 2025

```bibtex
@misc{poiroux2025reliableevaluationbenchmarksstatement,
    title={Reliable Evaluation and Benchmarks for Statement Autoformalization}, 
    author={Auguste Poiroux and Gail Weiss and Viktor Kunčak and Antoine Bosselut},
    year={2025},
    eprint={2406.07222},
    archivePrefix={arXiv},
    primaryClass={cs.CL},
    url={https://arxiv.org/abs/2406.07222}, 
}
```
