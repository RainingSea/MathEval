# RLMEval 生成与评测

使用 Codex 或 Claude Code 在独立的 Docker 容器中逐题生成 Lean 证明，随后由宿主机编译候选证明并汇总评分。原始题库随代码提供，位于 [RLMEval/data/rlm25_pfr_tlb.jsonl](RLMEval/data/rlm25_pfr_tlb.jsonl)。

![RLMEval 流程：逐题容器或裸模型生成候选，由宿主机 Lean 评分并统计逐题 pass@k](pipeline.png)

首次使用请准备 Linux x86-64、可运行容器的 Docker 和 Python 3.11。以下依次介绍宿主机环境配置、从源码构建和正式评测。已有预构建发布包的用户可直接阅读文末的快速开始。

## 宿主机 Python 环境

在含有 `requirements.txt` 的仓库目录中，使用同一个 Bash 终端执行。宿主机使用 Python 3.11 读题、启动 Docker、收集结果和评分。

```bash
python3.11 -m venv "$HOME/.venvs/rlmeval"
source "$HOME/.venvs/rlmeval/bin/activate"
export PYTHON_BIN="$VIRTUAL_ENV/bin/python"
"$PYTHON_BIN" -m pip install -r requirements.txt
PYTHONPATH="$PWD/RLMEval/src" "$PYTHON_BIN" -c 'from rlm_agent.items import load_items; from rlm_agent.scoring import RepoScorer'
```

## 从源码构建

从 GitHub 克隆本仓库后，进入本 README 所在的目录，先完成上面的 Python 环境安装。宿主机需要 Linux、Docker（BuildKit）、Elan、git、rsync 和 Lean 项目的原生构建工具。首次构建使用空的 `dependencies/`。如需使用独立数据盘，将下面的 `RLMEVAL_DEPS` 设为该盘上的绝对路径。

镜像构建使用宿主机的 Linux x86-64 Codex 和 Claude Code。安装位置不同时，先设置 `CODEX_VENDOR`（含 `bin/codex` 和 `codex-path/` 的目录）和 `CLAUDE_BIN`（Claude 原生可执行文件）。构建需要 Python 3.12 基础镜像和 wheels，离线 wheels 可通过 `WHEEL_DIR` 指定。

```bash
export RLMEVAL_DEPS="$PWD/dependencies"
bash RLMEVAL-ADAPT/build/build-libraries.sh "$RLMEVAL_DEPS"
"$PYTHON_BIN" RLMEVAL-ADAPT/build/prepare.py --deps "$RLMEVAL_DEPS"
docker pull python:3.12-slim-bookworm
./RLMEVAL-ADAPT/docker/build.sh
```

第一步在随附的 PFR 和 TLB 源码目录中分别执行 `lake exe cache get` 和 `lake build`，获取锁定版本的 Mathlib 缓存并编译项目。第二步根据逐题清单校验项目模块，以硬链接生成每题的 `lib/`，并写出含本机评分路径的 `dataset.jsonl`。

从源码构建的镜像是 `rlmeval-native:light`。启动器从 `RLMEVAL_DEPS` 只读挂载本题对应的 Lean 工具链，并在该依赖盘上用临时硬链接汇集公共包的 .olean；每个容器仍只挂载一份单题输入。临时目录在本批次结束后清理。默认优先选择轻量镜像；只有旧发布包时自动使用 `rlmeval-native:portable`，也可通过 `RLMEVAL_DOCKER_IMAGE` 明确选择。轻量镜像实测约 1.79 GB（未压缩）。PFR 第 10 题和 TLB 第 174 题均通过离线编译预检；GLM-5.3 分别通过 Codex 和 Claude Code 生成的第 174 题候选均通过宿主机评分。换终端时，请重新导出该目录的绝对路径和 `PYTHON_BIN`。

## 生成方式与采样配置

| 生成方式 | 入口 | 模型服务配置 |
|---|---|---|
| Codex | Docker 启动器，`BACKEND=codex` | `MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY`，使用 Responses 接口 |
| Claude Code | Docker 启动器，`BACKEND=claude` | `MODEL`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` |

**裸模型**使用宿主机脚本 `RLMEVAL-ADAPT/start-direct.sh`。安装本仓库的 `requirements.txt` 并准备好前述宿主机依赖和逐题数据后，在本 README 所在目录运行：

```bash
export MODEL=deepseek-v4.1-flash
export OPENAI_BASE_URL=https://token.pjlab.org.cn/v1
read -rsp 'API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
INDICES=1 SAMPLES=1 CONCURRENCY=1 ./RLMEVAL-ADAPT/start-direct.sh
```

裸模型使用 OpenAI 兼容的 Chat Completions API。启动器依次生成、评分、汇总，在 `RLMEVAL-ADAPT/runs/direct-*/` 下保存 `generations.jsonl`、`scores.jsonl`、`summary.json` 和 `summary.md`。配置集中在脚本顶部，同名环境变量优先。`SAMPLES` 是每题采样数，`CONCURRENCY` 是全批次同时生成的样本数，`MAX_OUTPUT_TOKENS` 是单次请求的输出上限。

Docker 示例：在启动前执行 `export INDICES=10,146 SAMPLES=4 CONCURRENCY=2 TMO=21600`。两个题目最多同时运行两个容器，每题在自己的容器中顺序生成 4 个样本，共 8 个候选；每个样本的超时为 21600 秒。

**逐题 pass@k** 由 `SAMPLES=k` 指定采样次数。该题的 k 个候选中至少一个通过评分，即记为该题通过。汇总中的 `observed_pass_at_k_rate` 是通过题数占汇总题数的比例。`CONCURRENCY` 控制同时运行的题数，不改变 k。

## 预检与正式评测

先设置本轮题号。预检只编译初始候选，不调用模型；正式评测沿用同一个 `INDICES`。

```bash
export INDICES=10,146
./RLMEVAL-ADAPT/start-docker.sh --prepare-only
```

预检通过后，选择一个后端，在同一终端设置模型和服务地址。下例以 Codex 的 Responses 接口为例，执行时输入实际值：

```bash
export BACKEND=codex
read -rp '模型名: ' MODEL
read -rp 'Responses API base URL: ' OPENAI_BASE_URL
read -rsp 'API key: ' OPENAI_API_KEY; echo
export MODEL OPENAI_BASE_URL OPENAI_API_KEY
./RLMEVAL-ADAPT/start-docker.sh
```

使用 Claude Code 时，设置 `BACKEND=claude`、`MODEL`、`ANTHROPIC_BASE_URL` 以及 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`，然后运行同一个启动脚本。采样数、并发和超时可分别用 `SAMPLES`、`CONCURRENCY` 和 `TMO` 设置。每题使用独立容器，题内样本顺序运行；宿主机收集并评分，结果写入 `RLMEVAL-ADAPT/runs/<RUN_ID>/`。

首次评分时，宿主机上的 lean-interact 需要获取与 Lean 版本匹配的 REPL，或使用预先准备的缓存。

## 快速开始：使用预构建发布包

从 GitHub 克隆本仓库，获取代码、题库和启动脚本。选择与仓库版本对应的两个预构建压缩包，并行下载到同一目录。

| 文件 | 用途 | 下载地址 |
|---|---|---|
| `rlmeval-images.tar.gz` | 当前发布包包含内置 Lean 和公共库的 rlmeval-native:portable，以及模型 API 代理镜像。要使用轻量版，请按上文从源码构建。 | |
| `rlmeval-dependencies.tar.gz` | 宿主机使用的 Lean 工具链、PFR/TLB 项目和已编译的 Mathlib，用于准备逐题依赖和评分。 | |

在克隆的仓库中进入本 README 所在的目录，执行前文的[宿主机 Python 环境](#宿主机-python-环境)命令。在同一终端中，将 `DIST` 设为两个压缩包的下载目录，加载镜像并准备逐题输入：

```bash
export DIST="$HOME/Downloads"  # 按实际下载位置修改
docker load -i "$DIST/rlmeval-images.tar.gz"
export RLMEVAL_DEPS="$PWD/dependencies"
mkdir -p "$RLMEVAL_DEPS"
tar -xzf "$DIST/rlmeval-dependencies.tar.gz" -C "$RLMEVAL_DEPS"
"$PYTHON_BIN" RLMEVAL-ADAPT/build/prepare.py --deps "$RLMEVAL_DEPS"
```

准备完成后直接进入[预检与正式评测](#预检与正式评测)。预构建包省去了下载和编译 Mathlib、安装容器内的 harness，以及制作 Docker 镜像的时间。
