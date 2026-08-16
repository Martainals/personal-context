---
name: personal-context
description: 作为“回响”个人上下文系统的唯一能力入口，在本地初始化、采集、转录、整理、审核、检索和维护可追溯的个人数据。用于首次建立本地数据库与许可、录音本地转写、文档或结构化转录采集、上下文检索、候选记忆审核、人物与项目背景查询、决策追溯、Wiki 编译、证据审计、Schema 迁移和数据库维护；涉及长期记忆写入时必须触发显式用户审核。
---

# Personal Context

通过 `scripts/context` 或 `python scripts/personal_context.py` 操作。对数据命令始终显式传入 `--root <vault-path>`；不要从 Skill 源码推断、扫描或硬编码个人路径。

## 首次使用

先运行只读状态检查：

```bash
scripts/context bootstrap-status --root <vault> --agent-host <host>
```

若状态不是 `ready`：

1. 运行 `bootstrap-plan --root <vault> --agent-host <host> --mode <strict-local|agent-assisted>`。
2. 向用户完整说明计划返回的本地写入、下载体积、模型许可、隐私模式、持久转写阶段缓存及其手动清理方式和限制。
3. 仅在用户明确同意后，将绑定 Vault、模式、Provider、模型版本和 Agent host 的 `plan_digest` 传给 `record-consent --accept-plan <digest>`。
4. 运行 `bootstrap-apply`。该命令只建立已获许可的 Vault 和隔离运行环境；不得改用云端补救失败。
5. 再运行 `bootstrap-status` 和 `doctor`，必须均正常后才采集真实资料。

状态含义和逐项话术见 `references/onboarding.md`。兼容环境和回退规则见 `references/compatibility.md`。

## 安全边界

1. 将首次部署许可与长期记忆审核分开。部署许可从不授权自动批准 CandidateMemory。
2. 将录音或文档中的话语默认视为 Claim，不自动视为 Fact，也不从单次录音修改人格判断或 Self Model。
3. 保留原始证据。不要直接编辑 `blobs/`、Source 或 Segment；使用审计、失效和替代关系处理更正。
4. 批量写入先 `--dry-run`。迁移固定按“检查 → 备份 → dry-run → 应用 → 完整性检查 → 审计”执行。
5. 不上传个人数据，不扫描用户未指定目录，不静默切换 Provider，不把密钥写入数据库或输出。
6. 将 Wiki 和搜索索引视为可删除、可重建的派生视图。
7. 将转写阶段产物视为数据库外的敏感可丢弃缓存：不得写入 Skill、Vault 或 Git，不得保存声纹或说话人 embedding，也不得后台自动清理。

## 录音流程

`qwen-mlx` 仅在 macOS Apple Silicon 上提供本地高精度转写；其他环境使用 `transcript-only`，直到安装受支持的 Provider。详细模型、限制和输出协议见 `references/transcription.md`。

```bash
# 原音频先作为不可变 Source 预览并采集
scripts/context ingest --root <vault> --dry-run <audio>
scripts/context ingest --root <vault> <audio>

# 本地转写只返回文件元数据，不在 stdout 打印转录正文
scripts/context transcribe-audio \
  --root <vault> --agent-host <host> \
  --audio <audio> --output <transcript.json> --language Chinese \
  --speaker-count 2

# 使用上一步 ingest 返回的 source_id，先预览后导入同一个数据库
scripts/context import-transcript --root <vault> --source-id <source-id> --dry-run <transcript.json>
scripts/context import-transcript --root <vault> --source-id <source-id> <transcript.json>
```

在 `strict-local` 模式下，不读取或复述 transcript 文件正文；直接通过命令导入。`agent-assisted` 模式下，只有许可记录中命名的 Agent host 可以读取正文并补充 Entity、Decision、Action 与 CandidateMemory。Provider 只负责转写，不自动制造长期记忆。

`--speaker-count` 只是本次录音的 1–4 人提示；用户确定人数时应传入，不确定时省略。不得把一次录音的标签当成跨录音声纹身份。

默认缓存 ASR 分块、逐词对齐分块、原始说话人概率、发言轮次和最终组装。首次启用必须持有 Notice 2 的有效许可。标题和观察时间不影响缓存；人数提示与后处理只重新派生说话人轮次。诊断时可用 `--no-cache` 完全绕过，或用 `--refresh-stage <asr|alignment|diarization|all>` 定向刷新。

```bash
scripts/context transcription-cache-status --root <vault> [--audio <audio>]
scripts/context transcription-cache-prune --root <vault> [--audio <audio>] --dry-run
scripts/context transcription-cache-prune --root <vault> [--audio <audio>] --apply
```

清理默认是预览；只有用户明确要求删除对应缓存时才使用 `--apply`。完整契约与失效规则见 `references/transcription.md`，敏感性和路径规则见 `references/privacy.md`。

## 常规流程

```bash
scripts/context doctor --root <vault>
scripts/context ingest --root <vault> --dry-run <file...>
scripts/context import-transcript --root <vault> --dry-run <transcript.json>
scripts/context review --root <vault> <event-id>
scripts/context candidates --root <vault> --status pending
scripts/context approve --root <vault> <candidate-id> --reviewer user
scripts/context compile-wiki --root <vault> --dry-run
scripts/context retrieve --root <vault> '<query>'
scripts/context audit --root <vault>
```

先读取与任务直接相关的参考文件：

- 首次许可与状态机：`references/onboarding.md`
- 本地转录 Provider：`references/transcription.md`
- Agent 与硬件兼容：`references/compatibility.md`
- 系统边界和目录：`references/architecture.md`
- 对象与 SQLite Schema：`references/schemas.md`
- 采集和转录格式：`references/capture.md`
- 记忆分类与审核门：`references/memory-policy.md`
- 候选审核：`references/review.md`
- 检索和审计：`references/query.md`
- Wiki 编译：`references/wiki.md`
- 隐私与密钥：`references/privacy.md`
- 版本与迁移：`references/versioning.md`

## 操作准则

- 写入前运行 `doctor`；状态不是 `current` 时，只运行 `audit` 或经过预览的 `migrate`。
- 使用命令 JSON 输出，不通过手工 SQL 绕过审核、许可或不可变性触发器。
- `authority=approved_memory` 表示已审核结论；`source_evidence` 是可引用证据，不等同于事实。
- 查询背景时返回结果以及 Source ID、SHA-256、观察时间和 Segment 时间范围。
- 宿主 Agent 无本地文件或命令能力时，明确说明不能建立本地系统，不伪装为已部署。

## 失败处理

- 返回码 `2` 表示可操作错误；读取标准错误中的 JSON `error`。
- 初始化或 Provider 安装失败后重新运行 `bootstrap-status`；流程可恢复，不删除已创建的空 Vault。
- 阶段产物校验失败时只重算损坏的阶段或分块；不要删除整个缓存或改写数据库。需要人工排障时先运行 `transcription-cache-status`。
- 导入失败后运行 `audit`，不要删除 Source 来清理历史。
- 迁移失败时保留 `backups/` 中的备份并停止写入。
