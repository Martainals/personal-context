<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="personal-context：可自举本地转录与证据数据库的开放 Agent Skill">
</p>

<p align="center">
  <img alt="Skill version 0.3.0" src="https://img.shields.io/badge/skill-0.3.0-0f766e?style=flat-square">
  <img alt="Schema version 1" src="https://img.shields.io/badge/schema-1-b7791f?style=flat-square">
  <img alt="Agent Skills open format" src="https://img.shields.io/badge/format-Agent%20Skills-334155?style=flat-square">
  <img alt="Python 3.9 or newer" src="https://img.shields.io/badge/core-Python%203.9%2B-334155?style=flat-square">
  <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-334155?style=flat-square">
</p>

`personal-context` 是“回响”个人上下文系统的唯一能力入口：一个遵循开放 Agent Skills 格式、能由不同本地 Agent 驱动、可在用户许可后建立本地数据库和本地录音转写环境的开源 Skill。

它最重要的约束不是“记得更多”，而是：**原话不自动成为事实，候选不自动成为记忆，结论必须能回到来源。**

## 它现在能完成什么

- 首次运行时生成机器可读的部署计划，向用户说明路径、下载、许可和隐私边界；
- 用摘要锁定用户明确同意的范围，随后建立唯一 SQLite Vault；
- 在 Apple Silicon 上部署隔离的 Qwen3-ASR BF16 本地转写环境；
- 将录音或文档变成不可变 Source、带时间的 Segment 与默认 Claim；
- 审核 CandidateMemory 后才形成长期 Memory；
- 以来源哈希、观察时间和录音时间范围检索、审计并重新编译 Wiki；
- 通过相同 JSON CLI 被 Codex、Claude Code、Gemini CLI 或其他具备本地执行能力的 Agent 使用。

<p align="center">
  <img src="./assets/readme/local-bootstrap.svg" width="100%" alt="兼容 Agent 通过许可状态机建立私有转录环境和唯一 SQLite Vault">
</p>

## 为什么它不同

| 原则 | 实现 |
|---|---|
| 首次许可不是一句提示词 | `status → plan → notice digest → receipt → apply` 确定性状态机 |
| 本地转录没有静默云端降级 | `qwen-mlx` 失败就停止；不自动改用 API |
| 模型环境不污染系统 | 私有 Python、虚拟环境、缓存和模型均位于用户配置目录 |
| 原始证据不可静默修改 | Source 按 SHA-256 寻址；Source 与 Segment 受 SQLite 触发器保护 |
| “某人说过”不等于“事实” | 每个语音 Segment 默认形成 `Claim` |
| 采集不等于形成长期记忆 | 只有显式 `approve` 才创建 `Memory` |
| 模型可替换，数据库不分裂 | Provider 统一输出 `transcript.v1.json`，再进入同一个 Schema 1 |
| Agent 可换，协议不变 | 标准 `SKILL.md`、显式路径、JSON stdout/stderr 和稳定退出码 |

## 兼容性

“跨 Agent”与“跨硬件”是两件事：

| 环境 | 数据库与转录导入 | 自动本地录音转写 |
|---|---:|---:|
| Codex / Claude Code / Gemini CLI，且可运行本地命令 | 完整 | 取决于电脑 |
| macOS Apple Silicon | 完整 | `qwen-mlx` 高精度配置 |
| Linux / Windows / 普通 CPU | 完整 | 本版使用 `transcript-only` |
| 纯网页 Agent、无本地文件或进程权限 | 无法自动部署 | 无法自动部署 |

`auto` 只会在 macOS Apple Silicon 选择 `qwen-mlx`，其他环境选择 `transcript-only`，永远不会自动选择云服务。

## 安装 Skill

克隆仓库后，把 `personal-context/` 复制到宿主的 Skill 目录：

```bash
git clone https://github.com/Martainals/personal-context.git
cd personal-context

# Codex
rsync -a personal-context/ "${CODEX_HOME:-$HOME/.codex}/skills/personal-context/"

# Claude Code
rsync -a personal-context/ "$HOME/.claude/skills/personal-context/"

# Gemini CLI
rsync -a personal-context/ "$HOME/.gemini/skills/personal-context/"
```

也可不安装，直接从源码调用：

```bash
./personal-context/scripts/context version
```

Windows 或没有 POSIX shell 的宿主使用：

```text
py personal-context/scripts/personal_context.py version
```

## 第一次使用

先选择一个新的或现有 Vault 路径，只读检查当前状态：

```bash
CONTEXT_ROOT="/path/to/personal-context-vault"

./personal-context/scripts/context bootstrap-status \
  --root "$CONTEXT_ROOT" \
  --agent-host codex
```

然后生成完整计划。`strict-local` 不允许 Agent 读取转录正文；`agent-assisted` 允许当前命名的 Agent 按其宿主数据政策整理正文：

```bash
./personal-context/scripts/context bootstrap-plan \
  --root "$CONTEXT_ROOT" \
  --agent-host codex \
  --mode strict-local
```

只有用户明确接受计划后，Agent 才能把返回的摘要写入许可记录：

```bash
./personal-context/scripts/context record-consent \
  --root "$CONTEXT_ROOT" \
  --agent-host codex \
  --mode strict-local \
  --accept-plan '<bootstrap-plan 返回的 plan_digest>'

./personal-context/scripts/context bootstrap-apply \
  --root "$CONTEXT_ROOT" \
  --agent-host codex
```

在 Apple Silicon 上，首次 `bootstrap-apply` 会在私有目录安装约 6.5 GB 模型与隔离运行环境，至少预留 10 GB 空间。流程可恢复执行，不修改系统 Python，不启动后台服务。

## 直接发送一段录音之后

Agent 应将录音落为一个明确的本地文件路径，再运行：

```bash
# 1. 原始录音成为不可变 Source
./personal-context/scripts/context ingest \
  --root "$CONTEXT_ROOT" --dry-run /path/to/recording.m4a

./personal-context/scripts/context ingest \
  --root "$CONTEXT_ROOT" /path/to/recording.m4a

# 2. 本地 ASR、时间对齐和最多四人的说话人分离
./personal-context/scripts/context transcribe-audio \
  --root "$CONTEXT_ROOT" \
  --agent-host codex \
  --audio /path/to/recording.m4a \
  --output /private/path/recording.transcript.json \
  --language Chinese \
  --speaker-count 2

# 3. 使用 ingest 返回的 source_id 预览并导入同一数据库
./personal-context/scripts/context import-transcript \
  --root "$CONTEXT_ROOT" \
  --source-id '<source-id>' \
  --dry-run /private/path/recording.transcript.json

./personal-context/scripts/context import-transcript \
  --root "$CONTEXT_ROOT" \
  --source-id '<source-id>' \
  /private/path/recording.transcript.json
```

`transcribe-audio` 的标准输出只包含文件路径、哈希和大小，不打印转录正文。严格本地模式下，Agent 可以直接完成导入而不读取正文。

`--speaker-count` 是单次录音的可选提示；确定人数时应显式填写 1–4，不确定时省略并自动检测。已录完的文件默认使用高上下文 Sortformer 配置，再依据整段录音的置信度统一说话人通道、清除短暂跳变并组装发言轮次。

> 本地模型不上传音频，但如果用户先把附件上传到云端聊天页面，该宿主可能已经收到文件。敏感录音应尽量以本地路径交给具备本地访问能力的 Agent。

## 本地模型配置

版本全部锁定在 [`qwen-mlx.lock.json`](./personal-context/assets/providers/qwen-mlx.lock.json)：

| 环节 | 模型 | 作用 |
|---|---|---|
| 转写 | Qwen3-ASR-1.7B-bf16 | 中文与多语言高精度识别 |
| 对齐 | Qwen3-ForcedAligner-0.6B-bf16 | 字/词级时间戳 |
| 多人分离 | Streaming Sortformer v2.1 fp32 | 高上下文推理、全局人数约束与最多四个录音内标签 |

说话人标签只在单次录音中使用 `S01`–`S04`，不是声纹身份。五人以上、强噪声、重叠发言、超长录音或非英语多人会议可能降低分离质量。

## 证据与记忆

<p align="center">
  <img src="./assets/readme/evidence-flow.svg" width="100%" alt="从不可变 Source 到用户审核、长期 Memory 和带来源查询的证据流水线">
</p>

常规审核与检索：

```bash
./personal-context/scripts/context candidates --root "$CONTEXT_ROOT" --status pending
./personal-context/scripts/context review --root "$CONTEXT_ROOT" '<event-id>'
./personal-context/scripts/context approve \
  --root "$CONTEXT_ROOT" '<candidate-id>' \
  --reviewer user --reason "已核对来源和有效时间"

./personal-context/scripts/context compile-wiki --root "$CONTEXT_ROOT" --dry-run
./personal-context/scripts/context compile-wiki --root "$CONTEXT_ROOT"
./personal-context/scripts/context retrieve --root "$CONTEXT_ROOT" "周五发布"
```

检索严格区分：

- `authority=approved_memory`：经过明确审核的长期记忆；
- `authority=source_evidence`：来源证据，不自动等同于事实。

## Vault 与私有运行环境

Vault 由用户选择，是唯一权威数据层：

```text
<vault>/
├── context.sqlite3
├── blobs/          # 不可变 Source
├── inbox/
├── wiki/           # 可重建阅读视图
└── backups/
```

许可、私有 Python、模型和缓存位于操作系统用户配置目录，也可以用 `--config-dir` 显式指定。它们不进入 Vault、Git 仓库或数据库导出。

## 版本与维护

| 项目 | 当前值 |
|---|---|
| Skill | `0.3.0` |
| SQLite Schema | `1` |
| Consent notice | `1` |
| Provider contract | `1` |
| qwen-mlx profile | `2` |

本次新增许可与本地转写能力没有改变权威数据库表，因此 Schema 保持 1；模型与包的来源信息进入既有 `processing_runs.parameters_json`。

项目根目录的 [`AGENTS.md`](./AGENTS.md) 是后续 Agent 开发和升级的唯一维护章程。`CLAUDE.md`、`GEMINI.md` 只引用它，避免多份规则漂移；Codex 的 `personal-context/agents/openai.yaml` 仅是可选界面元数据。

## 开发与验证

数据库与初始化控制层只依赖 Python 3.9+ 标准库和 SQLite。模型推理依赖只安装进私有运行环境。

```bash
PYTHONPYCACHEPREFIX=/tmp/personal-context-pycache \
  python3 -m unittest discover -s tests -v
```

自动测试使用临时路径、中文合成数据和模拟 Provider；不会下载模型，也不会接触真实个人 Vault。

## 项目结构

```text
.
├── AGENTS.md                     # 唯一项目维护章程
├── CLAUDE.md / GEMINI.md         # 指向 AGENTS.md 的宿主薄入口
├── personal-context/
│   ├── SKILL.md
│   ├── VERSION
│   ├── agents/openai.yaml        # 可选 Codex UI 元数据
│   ├── scripts/
│   │   ├── context
│   │   ├── personal_context.py
│   │   ├── personal_context_bootstrap.py
│   │   └── providers/qwen_mlx.py
│   ├── assets/providers/         # 锁定包、模型和限制
│   ├── assets/templates/
│   └── references/
├── assets/readme/
└── tests/
```

## 明确不做

- 未经审核自动形成长期记忆；
- 声纹身份识别或跨录音追踪说话人；
- 后台监听、持续采集或常驻转录服务；
- 静默上传、静默云端回退或自动配置 API Key；
- 自动人格建模；
- 把 Wiki 或搜索索引当成唯一权威来源。

## License

[MIT](./LICENSE) © 2026 Martainals

模型权重遵循各自锁定文件中标明的上游许可；安装前应阅读对应模型卡。
