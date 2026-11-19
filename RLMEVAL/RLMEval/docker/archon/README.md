# PFR：Docker 生成，宿主机评测

在 `/data/projects/archon-docker` 目录执行。镜像是本机的 `rlmeval-archon:pfr`。
每个容器只跑一道题，默认 GLM-5.3 + Archon/Codex。
**最多 3 轮 plan-action、最长 6 小时；轮次内调用 Lean 不限次数。**

## 1. 启动一道题

下面运行 PFR 第 1 题，使用镜像内的第一把智谱 Key 和官方 URL：

```bash
docker run --rm --init -e http_proxy -e https_proxy -e no_proxy -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -v "$PWD/tasks/PFR/1:/input:ro" -v "$PWD/outputs/PFR-1-s0:/outputs" rlmeval-archon:pfr run --api-profile zhipu --input /input --output /outputs/result --max-iterations 3 --timeout 21600
```

- `tasks/PFR/1` 是第 1 题的输入目录。换题时把 `1` 换成 `tasks.tsv` 中的题号。
- `outputs/PFR-1-s0` 是本机接收输出的目录。每次运行换一个新目录。
- `-v` 中冒号左边是本机目录，右边是容器目录；`:ro` 表示只读。
- `--max-iterations 3` 控制 Archon 整体轮数；`--timeout 21600` 是总时限，单位秒。
- 要后台运行，在 `docker run` 后加 `-d`，去掉 `--rm` 可保留容器记录。

容器把结果写到本机 `outputs/PFR-1-s0/result/`：

| 文件 | 内容 |
|---|---|
| `generation.json` / `generations.jsonl` | 同一个未评分的生成样本；评测时自动选择 JSONL，不会重复计数 |
| `Candidate.lean` | 生成的目标声明和证明 |
| `Task.lean` | 包含题目上下文的完整生成文件 |
| `archon.log`、`workspace/.archon/` | agent 日志和运行记录 |

容器没有自定义 checker，不做最终编译验收，也不输出“证明通过”。
agent 写证明期间仍可自行运行 `lean Task.lean` 获取反馈。

也可以用简便脚本，自动创建新的输出目录：

```bash
./run.sh 1
```

## 2. 在宿主机评测

容器结束后，执行：

```bash
./evaluate.sh outputs/PFR-1-s0 --output evaluations/PFR-1-s0
```

这一条命令完成数据组装，并直接调用本项目已有的评测、后处理：

1. 将一个或多个容器的输出整理成标准 `generations.jsonl`，按题号选出完整的 `RlmItem` 数据写入 `items.jsonl`。
2. 调用现有 `rlmv-eval score --task proof` 对应的 `rlm_agent.cli score`，由宿主机 Lean 编译评分。
3. 调用现有 `scripts/summarize_codex_run.py`，生成原有格式的 `summary.json`、`summary.md`。

**评分器 `src/rlm_agent/scoring.py` 和汇总脚本均没有修改。**
只有一条数据也可以评测，不要求完整数据集，不会补充虚构样本。
生成声明中的 `sorry`、修改后的定理等会原样交给现有评分器，不会先被组装脚本改成空预测。

结果在指定的 `evaluations/PFR-1-s0/`：

| 文件 | 内容 |
|---|---|
| `scores.jsonl` | 现有评分器直接写出的完整结果，包含 `score_status`、`generation_status`、`parse_ok`、`proof_ok`，以及适用的 `error` / `typecheck_error` |
| `summary.json` | 原有汇总格式：状态分布、通过率、逐题结果、`generation_error_details` |
| `summary.md` | 原有可读报告，包含生成失败原因和逐题状态 |
| `generations.jsonl` / `items.jsonl` | 整理后供现有评测读取的输入 |
| `source_generations.jsonl` | 未改动的原始生成记录副本 |
| `logs/*.events.jsonl` | 从容器路径映射、汇集到宿主机的事件日志 |
| `generation_diagnostics.jsonl` | 每个样本的原始状态、退出码、`error_detail`、失败事件和日志截断位置 |
| `sample_sources.jsonl` / `assembly.json` | 原始样本及日志路径映射、样本数量 |
| `evaluator.json` | 实际执行命令、现有评分器和汇总脚本的 SHA256 |

现有 `score_status` 分类保留，例如：

| 状态 | 含义 |
|---|---|
| `passed` | 证明通过 |
| `placeholder_in_proof` | 证明含 `sorry`、`admit` 或 `apply?` |
| `statement_modified` | 生成的定理声明与目标不一致 |
| `generation_error` | 生成阶段未正常完成；具体 API / 运行错误看报告中的生成失败原因和诊断日志 |
| `parse_error` | 无可用的生成文本 |
| `failed` / `proof_lean_error` | 编译或 Lean 检查失败，查看该条的 `typecheck_error` |
| `context_error` / `checker_error` | 评测上下文或评测环境异常，查看该条的 `error` |

API 错误不再因容器内日志路径不可读取而丢失。已有报告会从汇集后的 `turn.failed` 事件
读取错误消息，例如 HTTP 401、429；其他失败事件、重试期间发生的错误也保存在
`generation_diagnostics.jsonl`。普通模型元信息警告不会被组装脚本擅自改成 API 失败。

评测多个容器的输出：

```bash
./evaluate.sh outputs/PFR-1-s0 outputs/PFR-2-s0 --output evaluations/batch-01
```

也可以传入一个包含多次容器输出的父目录，或者一个 `generation.json` / `generations.jsonl`。
评测输出目录必须是新目录，放在容器挂载目录之外。

本机的宿主机评测环境已经配置好：

- 代码：`/data/projects/math-harness-eval/RLMEval`
- Python：`/root/miniconda3/envs/rlmeval/bin/python`
- 数据集：`/data/projects/math-harness-eval/RLMEval/data/rlm25_pfr_tlb.jsonl`
- 编译环境：`/data/projects/math-harness-eval/rlm-env/pfr`

宿主机评测不调用模型，不需要 API Key，也不需要上游最初脚本使用的 blueprint / 参考证明 trace。
搬到其他机器时，可用 `RLMEVAL_ROOT`、`RLMEVAL_PYTHON` 指定现有 RLMEval 代码和 Python，
命令支持 `--dataset`、`--projects-root`、`--elan-home`。

## 选择 Key 和 URL

镜像按 `RLMEval/secret.txt` 内的配置保留 Key/URL。修改启动命令中的 `--api-profile`：

| 名称 | 配置 | URL |
|---|---|---|
| `zhipu` | 智谱第一把 Key | `https://open.bigmodel.cn/api/v1` |
| `zhipu-2` | 智谱第二把 Key | `https://open.bigmodel.cn/api/v1` |
| `external` | 外部中转 | `https://api.deepseek.com` |
| `internal` | 内部中转 | `https://token.pjlab.org.cn/v1` |
| `platform` | 自己平台 | `https://api.tensoris.ai/v1` |
| `reverse` | 反代平台 | `https://lhcodex.zeabur.app/v1` |
| `env` | 环境变量 | `ARCHON_API_BASE`，默认智谱官方地址 |

例如 `./run.sh 1 --api-profile zhipu-2`。
如在启动时提供 Key/URL，用 `--api-profile env`，并在镜像名前添加
`-e ARCHON_API_KEY -e ARCHON_API_BASE`。模型默认 `glm-5.3`，可用 `--model` 修改。

## 题目和镜像

`tasks/PFR/` 含 144 道题；缺少 informal proof 的第 16 题已经排除，详见 `excluded.tsv`。
镜像只有 PFR 环境，约 6.65 GB。Lean 4.14.0-rc3、Mathlib 和公共依赖由镜像共享，每题不复制 Mathlib。
本次只更新运行脚本，没有重新安装编译环境。

需要搬运生成镜像时：

```bash
docker save rlmeval-archon:pfr | gzip -1 > image.tar.gz
```

在另一台机器加载：

```bash
docker load -i image.tar.gz
```

本机验证：此前 PFR 第 1 题的输出经本项目现有评测得到 `score_status=passed`、`proof_ok=true`，
结果在 `evaluations/pfr-1-project-format-20261110/`。另用构造数据验证了
`sorry`、修改声明、API 429、空输出、类型错误的完整分类与报告；没有重新调用模型。
此前 `native/` 路径下的上游最初格式结果是历史测试产物，默认入口现已恢复为本项目的增强评测流程。

## 从源码维护

容器生成模块：`src/rlm_pipeline/`；宿主机数据适配入口：`scripts/eval_agent_outputs.py`。
正式评分使用现有 `src/rlm_agent/scoring.py`，报告使用现有 `scripts/summarize_codex_run.py`。
不要用简化结果替代现有 `scores.jsonl` 和 `summary.json` 格式。
构建脚本：`docker/archon/prepare_build.py`，PFR 环境使用 `--repos PFR --secrets-file secret.txt`。
TLB 后续整合经验见 `TLB-INTEGRATION-NOTES.md`，本次没有合并 TLB 镜像。


## CC / Codex 切换（2026-11-10 更新）

镜像同时提供 Claude Code 2.1.322 和 Codex。默认使用 Codex，`cc` 是 `claude-code` 的简写；参数由 `run.sh` 传给运行器。

```bash
# Codex：Responses 协议；此 DS 地址已验证支持该协议
export ARCHON_API_KEY=unused
export ARCHON_API_BASE=http://s-20261102201825-hhmn4.ailab-ma4agismall.pjh-service.org.cn/v1
bash run.sh 1 --harness codex --api-profile env --model deepseek-v4-pro-0813
# 等价的 DS 快捷入口
bash run.sh_dsv4pro.sh 1

# CC：Anthropic Messages 协议；用你的 Anthropic 兼容服务根地址
export ANTHROPIC_AUTH_TOKEN='替换为实际Key'
export ANTHROPIC_BASE_URL='https://你的Anthropic兼容服务根地址'
bash run.sh 1 --harness cc --api-profile env --model '你的模型ID'

# 使用内置 Key，但必须明确指定该服务的 CC 地址
bash run.sh 1 --harness cc --api-profile zhipu --api-base https://open.bigmodel.cn/api/anthropic --model glm-5.3
```

**Base URL 注意：** Codex 使用 OpenAI Responses（常见 Base URL 以 `/v1` 结尾，请求 `/responses`）；CC 使用 Anthropic Messages（客户端请求 `/v1/messages`）。两种地址不能直接混用，不要把完整 `/responses` 或 `/v1/messages` 请求路径当 Base URL。服务的具体前缀以部署配置为准；只支持 Chat Completions 的服务不能直接接入这两个引擎。

CC 模式要求 `--api-base` 或 `ANTHROPIC_BASE_URL`，不会自动套用内置的 Codex URL。`--api-profile env` 时，Key 优先读取 `ARCHON_API_KEY`（或 `--api-key-env` 指定的变量），其次读取 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`；请清除不适用的旧 `ARCHON_API_KEY`，避免误选。`--api-base` 优先于环境变量。`--api-profile zhipu` 等内置配置仍使用该配置的 Key。

最多轮数是上限：证明占位符清零且 Lean 编译通过后可提前退出。编译失败或超时则继续。容器仍只负责生成，正式评分在宿主机进行。

### 自部署 DS 的 CC 实测

`http://s-20261102201825-hhmn4.ailab-ma4agismall.pjh-service.org.cn` 的 Anthropic Messages 接口及 Claude Code 2.1.322 均已实测：普通问答返回正确答案，Read 工具调用也成功完成（两轮交互）。模型为 `deepseek-v4-pro-0813`，无需真实 API Key，客户端填 `unused`。

```bash
# 通过 DS 快捷脚本选择 CC，并显式覆盖为根地址
bash run.sh_dsv4pro.sh 1 --harness cc --api-base http://s-20261102201825-hhmn4.ailab-ma4agismall.pjh-service.org.cn
```

相同服务的 Codex Base URL 是根地址加 `/v1`。CC 可能提示自定义模型名未收录，此提示未阻止本次问答和工具调用。上述实测验证了接口和工具交互，不代表已完成整道 Lean 题目。

### 非正式证明求助：仅允许主模型

保留 `python3 .claude/tools/archon-informal-agent.py --provider main "问题"`。单题工作区会安装工具入口，运行器自动绑定本次 `--model`、选定 API Key、Base URL 和引擎对应协议。Codex 走同一 Responses 接口；CC 走同一 Messages 接口。没有额外 Key 配置步骤。

镜像设置 `ARCHON_INFORMAL_MAIN_ONLY=1`。默认 `--provider auto` 同样指向主模型；显式选择 OpenAI/Gemini 等提供商或另一个模型会在发请求前失败。缺配置或请求失败时直接报错，不会尝试 GPT 或其他服务。工具仍是按需求助，不强制每轮调用。

自部署 DS 的两种求助协议均做过真实请求验证，均正确返回 391。正式 Lean 评分仍由宿主机完成。
