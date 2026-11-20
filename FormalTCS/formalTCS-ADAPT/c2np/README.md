# FormalTCS-ADAPT：C2NP 环境

独立运行 FormalTCS C2NP，不依赖旧的 mathharness。目前批跑后端为 Claude Code。

```text
MathEval/FormalTCS/
├── FormalTCS/           随 MathEval 跟踪的上游源码和题库快照
├── dependencies/       可选静态依赖资源（不跟踪，C2NP 不需要）
└── formalTCS-ADAPT/
    └── c2np/           C2NP 的代码、配置、Dockerfile、安装脚本和说明
        ├── .local/     本地 Python 虚拟环境
        └── runs/       挂载到宿主机的运行产物、评分与总分
```

## 从源码重建环境

宿主机需要 Linux（x86-64 或 arm64）、Bash、Git、Python 3.10+（含 `venv`），以及当前用户可访问的本机 Docker daemon（启用 BuildKit）。克隆 MathEval 需要仓库访问权限和 GitHub SSH 配置；克隆后，上游源码和题库已经在本地。首次构建镜像需要访问 Docker Hub 和 `storage.googleapis.com`。不需要模型密钥，也不需要在宿主机安装 Claude CLI、bwrap、Lean、Mathlib 或额外的 Python 包：C2NP 只把 Lean 源码当作文本读取。

```bash
git clone git@github.com:RainingSea/MathEval.git
cd MathEval
cd FormalTCS/formalTCS-ADAPT/c2np
bash bootstrap.sh
bash build-image.sh
```

如果已克隆 MathEval，从仓库根目录进入 `FormalTCS/formalTCS-ADAPT/c2np/`，执行最后两条命令即可。

- `bootstrap.sh` 检查仓库自带的题库文件并创建 `.local/venv/`，不联网，不需要再次克隆 `zirui-HIT/FormalTCS`。`upstream.rev` 记录快照来源；如果以后更新快照，同步更新该文件。默认使用 `python3`，也可设置 `FORMALTCS_PYTHON`；不需要激活 venv。
- `build-image.sh` 只构建镜像。`Dockerfile` 固定 Python 基础镜像摘要，并安装经 SHA-256 校验的 Claude Code `2.1.323`。默认镜像名为 `formaltcs-c2np:claude-2.1.323`，由本机 Docker 管理。可用 `docker run --rm --network none --entrypoint claude formaltcs-c2np:claude-2.1.323 --version` 查看内置版本。

`dependencies/` 可以不存在。`runs/`、`dependencies/`、本地环境和密钥文件均不随 Git 提交。上游快照是普通文件，不是子模块，也没有嵌套的 `.git`。

## Docker 批跑

在同一目录配置生成与裁判服务后运行。两个 endpoint 和模型都可以替换为你有权限访问的服务。不要把密钥提交到 Git。

```bash
read -rsp 'Generation API key: ' ANTHROPIC_AUTH_TOKEN; echo
read -rsp 'Judge API key: ' JUDGE_API_KEY; echo
export ANTHROPIC_AUTH_TOKEN JUDGE_API_KEY
INDICES=1-2 CONCURRENCY=2 SAMPLES=4 \
  MODEL=deepseek-v4.1-flash-1017 ANTHROPIC_BASE_URL=https://discovery-api.intern-ai.org.cn \
  JUDGE_MODEL=gpt-6.1-sol JUDGE_EFFORT=high JUDGE_BASE_URL=https://api.tensoris.ai/v1 \
  bash run.sh
```

`build-image.sh` 只构建镜像，不需要密钥，只需在首次运行或镜像内容变更后执行。`run.sh` 使用已有镜像，不自动构建。实验配置集中在 `run.sh` 开头的 `defaults`，同名非空环境变量优先；使用自定义镜像标签时给两个脚本传入同一个 `IMAGE`。批跑不读取 `model_conf.txt`：生成走 `ANTHROPIC_BASE_URL` 的 Anthropic Messages，裁判走 `JUDGE_BASE_URL` 的 OpenAI Responses，两边分别使用各自的密钥。当前 `BACKEND` 只支持 `claude`。

`INDICES` 按题库原始顺序从 1 编号，支持 `1,3-5` 或 `all`；也可以用 `ITEMS=BNNESS_463232,DMLG_319520` 直接指定题目 ID，非空时优先于 `INDICES`。`CONCURRENCY` 是生成并发；`SAMPLES=4` 只表示每题独立生成四份样本，不计算 pass@k。示例共启动八个容器，每个样本的 `TMO` 默认 43200 秒（12 小时）。

对受信网络内无需认证的 HTTP Messages 服务，可设置 `FORMALTCS_ALLOW_HTTP=1` 和 `ANTHROPIC_AUTH_TOKEN=formaltcs-no-auth`。后者只是满足 Claude Code 的占位值，不要用真实密钥替换；HTTP 不加密题面和回复。裁判仍要求 HTTPS。

镜像只含运行环境和代码，不含题库、凭据或旧运行结果。每个容器只读挂载自己的题面和任务说明，只写自己的输出目录；参考答案仅由宿主机裁判读取。容器内密钥由标准输入传入，不写进镜像、Docker 环境配置或运行清单。每个样本的 `docker.log` 可用 `tail -f` 查看已经发出的工具事件与诊断，不保存逐 token 的推理流。

容器全部退出后，启动脚本调用宿主机评分函数，逐份评分。`runs/batch-*/summary.json` 的 `total_score` 是所有成功评分样本的加权分之和，每份满分 1；`possible_score` 是计划样本数。失败或未获评分的样本分数为 `null`，批次标为 `incomplete` 并返回非零退出码，不把服务错误伪装成 0 分。可用 `python3 -B batch.py grade 'runs/实际批次目录'` 补评失败项并重算总分，不重新生成；补评需在环境中提供原批次的 `JUDGE_BASE_URL` 和对应的 `JUDGE_API_KEY`，模型沿用原批次配置。已成功评分的样本会复用。

## Claude Code 单题运行

需要原生 Claude Code CLI（`claude`）和可用的 `bwrap` 用户命名空间；不需要 Lean 或 Mathlib。从 `formalTCS-ADAPT/c2np/` 目录执行：

```bash
.local/venv/bin/python -I -B run.py --dataset formaltcs --task c2np \
  --backend claude --model deepseek-v4.1-flash-1017 --item BNNESS_463232
```

单题入口从环境读取 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL`。地址需指向兼容 Anthropic Messages 的 HTTPS 网关；runner 会把末尾的 `/v1` 或 `/v1/messages` 归一化为 Claude Code 所用的网关根地址。旧 `--profile` 参数仍可用于兼容已有配置。不要把密钥提交到 Git。

每次生成新建 `runs/<UTC时间>-c2np-<后缀>/`。Claude 在 `workspace/` 里读 `TASK.md` 和 `input/`，用 `Write` 交付 `workspace/output/nl_proof.md`。`bwrap` 将题面设为只读，只挂载该次工作区和必要的运行文件；不会挂载参考答案或加载本机 Claude 配置。`run.json` 记录题号、模型、耗时、状态和用量，另有 Claude 的脱敏日志。`generated` 表示 Claude 正常退出且写出了非空文件，不表示证明正确。

默认单题墙钟时限是 **12 小时**。如需调整 Claude 的输出预算，在启动命令前加 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=318000`；runner 会将该值传给 Claude 并记录在 `run.json`，但不会更改网关的限制。省略该环境变量时沿用 Claude 默认值。runner 不覆盖思考预算或限制工具回合数。C2NP 不编译，因此没有编译次数限制。

## 自动评分与补评

生成成功后默认启动裁判。批跑的裁判参数在 `run.sh` 中配置；直接调用 `run.py` 时，设置 `JUDGE_BASE_URL` 和 `JUDGE_API_KEY` 即可用默认裁判 `gpt-6.1-sol`（`high`），不设置则沿用旧版单题配置。只在评分阶段把参考证明和候选证明交给裁判；生成模型看不到参考证明。评分直接复用上游 C2NP rubric 及其四项权重：逻辑 40%、完整性 30%、正确性 20%、清晰度 10%。

`grade.json` 保存四项分数、加权总分、短评和用量；`run.json` 的 `grading` 单独记录评分状态。评分失败不会改动证明或把 `generated` 改成生成失败。只想生成时加 `--no-judge`；补评或重试失败的评分不用重跑模型，例如：

```bash
.local/venv/bin/python -I -B run.py --grade-run "runs/$RUN_ID"
```

对同一份证明重复补评会复用已有的成功评分。

## 后续后端

Codex 和裸模型尚未接入；后续可以沿用现有的输入文件、证明交付路径和宿主机评分接口。
