---
name: personal-context
description: 作为“回响”个人上下文系统的唯一能力入口，在本地采集、整理、审核、检索和维护可追溯的个人数据。用于个人记录与文档采集、录音或结构化转录整理、上下文检索、候选记忆审核、人物与项目背景查询、决策追溯、Wiki 编译、证据审计、Schema 迁移和个人数据库维护；涉及长期记忆写入时必须触发显式用户审核。
---

# Personal Context

通过 `scripts/context` 操作用户指定的数据仓库。始终显式传入 `--root <vault-path>`；不要从 Skill 源码推断或硬编码个人路径。

## 安全边界

1. 将采集与长期记忆分为两个阶段。导入只创建 Source、Event、Statement 和 CandidateMemory；仅在用户明确批准后运行 `approve`。
2. 将录音或文档中的话语默认视为 Claim，不自动视为 Fact。不要从单次录音修改人格判断或 Self Model。
3. 保留原始证据。不要直接编辑 `blobs/`、Source 或 Segment；使用审计、失效和替代关系处理更正。
4. 先对批量写入运行 `--dry-run`，向用户展示范围，再执行实际命令。
5. 任何迁移都按“检查 → 备份 → dry-run → 应用 → 完整性检查 → 审计”执行。数据库过旧时保持只读；数据库过新、未知或损坏时拒绝写入。
6. 将 Wiki 和搜索索引视为可删除、可重建的派生视图，不把它们当作唯一权威来源。
7. 不上传个人数据，不扫描用户未指定的目录，不把密钥写入数据库或输出。

## 标准流程

```bash
scripts/context init-vault --root <vault-path>
scripts/context doctor --root <vault-path>
scripts/context ingest --root <vault-path> --dry-run <file...>
scripts/context ingest --root <vault-path> <file...>
scripts/context import-transcript --root <vault-path> --dry-run <transcript.json>
scripts/context import-transcript --root <vault-path> <transcript.json>
scripts/context review --root <vault-path> <event-id>
scripts/context candidates --root <vault-path> --status pending
scripts/context approve --root <vault-path> <candidate-id> --reviewer user
scripts/context compile-wiki --root <vault-path> --dry-run
scripts/context compile-wiki --root <vault-path>
scripts/context retrieve --root <vault-path> '<query>'
scripts/context audit --root <vault-path>
```

先读取与任务直接相关的参考文件：

- 系统边界、目录和流水线：`references/architecture.md`
- 对象与 SQLite Schema：`references/schemas.md`
- 记忆分类、证据和审核门：`references/memory-policy.md`
- 采集、幂等导入和转录格式：`references/capture.md`
- 候选审核、批准、拒绝和冲突：`references/review.md`
- 检索、引用和审计：`references/query.md`
- Wiki 编译与派生索引：`references/wiki.md`
- 隐私、路径和密钥边界：`references/privacy.md`
- Skill/Schema 版本和迁移：`references/versioning.md`

## 操作准则

- 在写入前运行 `doctor`；若状态不是 `current`，只运行 `audit` 或经过预览的 `migrate`。
- 使用命令的 JSON 输出，不通过手工 SQL 绕过审核或不可变性触发器。
- 将 `retrieve` 中 `authority=approved_memory` 视为已审核结论；将 `source_evidence` 视为可引用证据，不等同于事实。
- 查询人物、项目或决策背景时，返回结果文本以及 Source ID、SHA-256、观察时间和 Segment 时间范围。
- 若用户要求 V1 未实现的向量检索、声纹识别、后台监听、自动人格建模或云同步，明确说明边界，不伪装为已支持。

## 失败处理

- 命令返回码 `2` 表示可操作错误；读取标准错误中的 JSON `error`。
- 导入失败后运行 `audit`。不要删除原始 Source 来“清理”历史。
- 迁移失败时保留 `backups/` 中的备份，并停止写入，直到审计或恢复完成。
