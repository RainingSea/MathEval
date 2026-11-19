# TLB 后续独立镜像整合记录

2026-11-09 用户决定停止 PFR/TLB 合并镜像，先交付 PFR，之后单独整合 TLB。
合并镜像的导出已取消，未完成的压缩包已删除。

- TLB 必须使用 `leanprover/lean4:v4.13.0-rc3`；PFR 使用 `v4.14.0-rc3`。两种版本的 `.olean` 不能混用。
- 公共 Mathlib 及包依赖放在环境镜像 `/opt/rlm/envs/tlb/lib`，不要每题复制 Mathlib。
- 单题只挂 `/input:ro` 和自己的 `/outputs`，不挂整个数据集或其他任务输出。
- 单题的 `lib/` 仅保留必要的 benchmark 项目依赖闭包；原目标文件的 `.olean` 不应导入。
- TLB 原始 124 题，排除 20 道缺少 informal proof 的题，保留 104 题。
- 排除编号：155, 157, 158, 160, 162, 178, 186, 197, 198, 224, 233, 234, 239, 240, 241, 242, 258, 261, 262, 263。
- 已导出并验证的 TLB 单题输入保存在 `/data/projects/.cache/rlmeval-tlb-prepared/`。
- 完整 TLB 公共编译依赖约 3.5 GiB，工具链约 1.2 GiB；Mathlib 本身约 3.4 GiB。
- 在默认 Docker 下用 TLB 161 完成过加载和上下文编译检查。冷缓存并行导出时 300 秒曾超时；暖缓存重试约 31 秒通过。启动示例用 `--compile-timeout 600 --timeout 1800`。
- Lean 搜索路径为 `/input/lib:/opt/rlm/envs/tlb/lib`；工具链路径为 `/opt/elan/toolchains/leanprover--lean4---v4.13.0-rc3/bin/lean`。
- `module_deps.lean` 使用 `getBuildDir` 初始化搜索路径，避免拿到宿主 PATH 中其他 Lean 版本。
- 默认模型为 `glm-5.3`，Archon 0.3.3 + Codex 0.163.0，智谱 Codex URL 为 `https://open.bigmodel.cn/api/v1`。
- 本机默认 Docker 已修复，可以普通 `docker run`，无需专用 socket。继承本机代理变量可访问外网。
- 构建器支持 `prepare_build.py --repos TLB`。共有工具链、Mathlib 不能只在镜像后续层删除来瘦身，必须从一开始只打包所选项目。
- PFR 已增加 `--api-profile` 选择内置 Key/URL；TLB 后续可复用同一实现，构建时添加 `--secrets-file secret.txt`。配置生成器为 `prepare_credentials.py`，使用 `secret.txt` 中的 Codex URL。

原始编译环境：`/data/projects/math-harness-eval/rlm-env/tlb`。
先前的共享构建上下文保存在 `/data/projects/.cache/rlmeval-archon-shared-build`，可用于复用文件和构建缓存。
