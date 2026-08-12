<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="personal-context：证据先于记忆的本地个人上下文 Codex Skill">
</p>

<p align="center">
  <img alt="Skill version 0.1.0" src="https://img.shields.io/badge/skill-0.1.0-0f766e?style=flat-square">
  <img alt="Schema version 1" src="https://img.shields.io/badge/schema-1-b7791f?style=flat-square">
  <img alt="Python 3.9 or newer" src="https://img.shields.io/badge/python-3.9%2B-334155?style=flat-square">
  <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-334155?style=flat-square">
</p>

`personal-context` 是“回响”个人上下文系统的唯一能力入口：一个本地优先、证据驱动的 Codex Skill，用统一命令采集文档与结构化转录、审核候选记忆、查询人物或项目背景、追溯决策，并从数据库重新编译 Wiki。

它最重要的约束不是“记得更多”，而是：**原话不自动成为事实，候选不自动成为记忆，结论必须能回到来源。**

## 一张图看懂

<p align="center">
  <img src="./assets/readme/evidence-flow.svg" width="100%" alt="从不可变 Source 到用户审核、长期 Memory 和带来源查询的证据流水线">
</p>

## 为什么它不同

| 原则 | V1 中的实现 |
| --- | --- |
| 原始证据不可静默修改 | Source 按 SHA-256 内容寻址；Source 与 Segment 受 SQLite 不可变触发器保护 |
| “某人说过”不等于“事实” | 转录片段默认形成 `Claim`，不会自动提升为 `Fact` |
| 采集不等于形成长期记忆 | 导入只产生 `CandidateMemory(pending)`；只有显式 `approve` 才创建 `Memory` |
| 所有长期写入可审计 | Memory 关联 Candidate、Review、Source，并可进一步定位 Segment 时间范围 |
| 时间不是单一字段 | 同时记录 `observed_at`、`valid_from`、`valid_to` |
| 派生视图不是权威数据 | Wiki 和搜索索引均可删除、可重建；SQLite 与不可变 Source 才是权威来源 |
| 批量写入先预览 | ingest、转录导入、Wiki 编译、索引重建和迁移均支持 dry-run/预览 |

## 五分钟上手

### 1. 安装 Skill

在仓库根目录执行：

```bash
rsync -a personal-context/ "${CODEX_HOME:-$HOME/.codex}/skills/personal-context/"
```

也可以先直接从源码运行：

```bash
./personal-context/scripts/context version
```

### 2. 初始化一个空数据仓库

先用临时或专用目录试运行，不要把测试指向真实个人资料：

```bash
CONTEXT_ROOT="/tmp/personal-context-demo"
./personal-context/scripts/context init-vault --root "$CONTEXT_ROOT"
./personal-context/scripts/context doctor --root "$CONTEXT_ROOT"
```

初始化后会得到：

```text
<root>/
├── context.sqlite3   # 权威结构化数据库
├── blobs/            # 按 SHA-256 保存的不可变 Source
├── inbox/            # 可选的用户暂存区
├── wiki/             # 可重新编译的阅读视图
└── backups/          # Schema 迁移前备份
```

### 3. 预览并导入结构化转录

仓库包含一个不需要 API Key 的 UTF-8 JSON 模板：
[`personal-context/assets/templates/transcript.v1.json`](./personal-context/assets/templates/transcript.v1.json)。

```bash
./personal-context/scripts/context import-transcript \
  --root "$CONTEXT_ROOT" \
  --dry-run \
  ./tests/fixtures/'合成 转录.json'

./personal-context/scripts/context import-transcript \
  --root "$CONTEXT_ROOT" \
  ./tests/fixtures/'合成 转录.json'
```

### 4. 审核候选，再形成记忆

```bash
./personal-context/scripts/context candidates \
  --root "$CONTEXT_ROOT" \
  --status pending

./personal-context/scripts/context review \
  --root "$CONTEXT_ROOT" \
  <event-id>

./personal-context/scripts/context approve \
  --root "$CONTEXT_ROOT" \
  <candidate-id> \
  --reviewer user \
  --reason "已核对来源和有效时间"
```

`approve` 命令本身代表明确批准。未经这一步，候选不会进入正式 Memory。

### 5. 编译 Wiki，并带来源检索

```bash
./personal-context/scripts/context compile-wiki \
  --root "$CONTEXT_ROOT" \
  --dry-run

./personal-context/scripts/context compile-wiki \
  --root "$CONTEXT_ROOT"

./personal-context/scripts/context retrieve \
  --root "$CONTEXT_ROOT" \
  "周五发布"
```

检索结果会区分：

- `authority=approved_memory`：经过明确审核的长期记忆；
- `authority=source_evidence`：来源证据，不自动等同于事实。

每条结果都可以带回 Source ID、SHA-256、观察时间、说话人以及 Segment 时间范围。

## 命令地图

| 命令 | 作用 |
| --- | --- |
| `init-vault` | 初始化空 V1 数据仓库 |
| `doctor` | 检查目录、SQLite 完整性、Schema 与兼容状态 |
| `ingest` | 建立不可变 Source 与内容寻址 blob |
| `import-transcript` | 无 API Key 导入结构化 JSON 转录 |
| `review` | 查看一次 Event 及完整证据链 |
| `candidates` | 列出待审核、已批准或已拒绝的候选记忆 |
| `approve` | 追加批准 Review 并创建一条 Memory |
| `reject` | 追加拒绝 Review，不创建 Memory |
| `retrieve` | 返回带来源引用的 Memory 与证据 |
| `audit` | 检查孤立记录、来源缺失、哈希异常和 Schema 问题 |
| `compile-wiki` | 从数据库重新生成 Markdown Wiki |
| `rebuild-index` | 删除并确定性重建搜索索引 |
| `migrate` | 默认预览、备份优先地执行 Schema 迁移 |
| `version` | 显示 Skill 与 Schema 兼容范围 |

所有数据命令都显式要求：

```text
--root <vault-path>
```

可复用源码不包含任何用户个人数据库绝对路径。

## 核心对象

```text
Source ── Segment ── Event
   │          │         │
   └──────────┴─────────┼── Statement / Entity / Relationship
                        ├── Decision / Action / Claim
                        └── CandidateMemory
                                  │
                              User Review
                                  │
                                Memory
```

V1 正式实现：`Source`、`Segment`、`Entity`、`Event`、`Statement`、`CandidateMemory`、`Memory`、`Relationship`、`Action`、`Decision`、`Claim` 和 `SchemaMetadata`。

`Hypothesis`、`Pattern`、`Experiment`、`SelfModelEntry` 只保留为未来概念，V1 不会自动生成。

## 版本与迁移

| 项目 | 当前值 |
| --- | --- |
| Skill | `0.1.0` |
| 当前 Schema | `1` |
| 最低支持 Schema | `1` |
| 最高支持 Schema | `1` |

兼容策略：

- 数据库过旧：停止写入，要求先备份和迁移；
- 数据库比 Skill 更新：拒绝写入，避免旧 Skill 破坏新数据；
- Schema 未知或损坏：进入审计模式；
- 迁移顺序固定为检查、备份、dry-run、应用、完整性检查、审计。

## 隐私与安全边界

- 本项目只在本地工作，不上传个人数据；
- 密钥不得写入源码、数据库或输出；
- 测试只使用临时目录和中文合成数据；
- 不扫描用户未指定的 Obsidian Vault 或父目录；
- 单次录音不能自动改变长期人格判断或 Self Model；
- 不使用无依据的小数置信度，优先保留支持证据、反面证据、来源数量和审核状态。

仓库中不包含真实个人数据库或录音。

## V1 暂不包含

- 向量数据库或语义检索；
- 自动录音转写与声纹识别；
- 后台文件监听、持续采集；
- 自动人格建模；
- 未经审核的长期记忆自动写入；
- 云端个人数据同步。

## 开发与验证

项目只依赖 Python 标准库和 SQLite，兼容 Python 3.9+。

```bash
PYTHONPYCACHEPREFIX=/tmp/personal-context-pycache \
  python3 -m unittest discover -s tests -v
```

当前自动测试覆盖初始化、中文和空格路径、重复导入、结构化转录、Claim/Fact 边界、候选审核、引用查询、Wiki 重编译、版本兼容、迁移预览、孤立记录审计和事务回滚。

## 项目结构

```text
.
├── personal-context/          # 可安装的 Codex Skill 包
│   ├── SKILL.md
│   ├── VERSION
│   ├── agents/openai.yaml
│   ├── scripts/context
│   ├── scripts/personal_context.py
│   ├── references/
│   └── assets/templates/
├── assets/readme/             # GitHub-safe SVG 视觉资产
└── tests/                     # 合成数据自动测试
```

详细规则采用渐进式披露：核心操作入口在 [`SKILL.md`](./personal-context/SKILL.md)，Schema、隐私、采集、审核、检索、Wiki 和版本规范位于 [`references/`](./personal-context/references/)。

## License

[MIT](./LICENSE) © 2026 Martainals
