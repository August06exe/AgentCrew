# PROTOCOL — 双模式协议与联邦通信标准

> 版本：agentcrew.protocol/v1
> 本文是总管、子助理、面板、脚本共同遵守的**机器可执行契约**。造新助理只需读 PARADIGM.md（怎么造）；本文规定"造好后怎么共处"。
> 一切以**纯文件**为载体：任何运行时（AI 会话、脚本、GUI、人）读得懂就能参与。

---

## §1 术语

| 术语 | 含义 |
|------|------|
| 总管 | 本仓库根目录的 AgentCrew 实例，唯一对主人说话的入口 |
| 子助理 | `02-agents\<id>\` 下的独立助理项目 |
| 主人 | 唯一人类用户（v1 单主人） |
| 收编区 | `02-agents\`，住在里面 = 托管模式 |
| 交办单 | 总管→助理的任务报文（task.json） |
| 回执 | 助理→总管的结果报文（receipt.json） |
| 快照 | 经主人授权、跨助理的只读数据副本 |
| 程序区 | 助理目录下除 `data\` 外的一切（可整体升级替换） |
| 数据区 | 助理的 `data\`（主人数据，升级永不触碰） |

---

## §2 lite / 托管双模式判定

### 判定算法（助理每次开工第一步执行）

```
从助理项目根目录出发逐级向上查找：
  若找到某目录同时满足：
    (a) 含 AGENTS.md
    (b) 含 01-master\
    (c) 含 00-docs\PROTOCOL.md
  则 → 托管模式（该目录即AgentCrew 仓库根）
  到达文件系统根仍未找到 → lite 模式
```

- 判定是**纯目录事实**，无任何配置文件：搬进收编区即托管，搬出即 lite，天然成立。
- **放归区豁免**：路径向上途经 `_standalone\` 组件时，即使更上层存在总管仓库也判 lite——因此放归区可放在仓库内 `_standalone\`（git 忽略），项目产物不出项目文件夹。
- 禁止用环境变量/注册表/隐藏文件等方式判定管辖——那会破坏"搬动即切换"。
- **通道独占**：同一助理 id 同一时刻只允许一份活动实例持有 IM 通道与提醒注册。收编区内的实例优先；`_standalone\` 里的放归副本若仍挂着旧通道属违规，收编/放归操作时总管（或人）须确认旧副本已停止接客。

### 行为矩阵

| 行为 | lite 模式 | 托管模式 |
|------|----------|---------|
| 与主人对话 | 直接对话（自己是全部） | 不直接对话；对主人的输出经总管转达 |
| 领域内服务 | 照常 | 照常（经交办单或主人直连该助理目录时） |
| 超出领域 | 明说"不归我管"并建议去找谁 | 写"转介"回执给总管，由总管路由 |
| 写数据 | 写自己 `data\` | 只写自己 `data\`（铁律不变） |
| 读别家数据 | 禁止 | 仅限有效期内、主人批过的快照 |
| 提醒 | 自查 manifest 自排（能排则排） | 由总管统一翻译注册，可被总管按主人作息调时 |
| 汇报周期性成果 | 主动推给主人 | 写主动汇报报文进 `outbox\`（§4.5），总管汇总转达 |

---

## §3 身份与命名

- 助理 id：`<作者>.<名字>`，小写字母数字与点，全局唯一。例：`demo.sample`、`demo.fitness`。
- id 写进 manifest.json 的 `id` 字段，收编时 registry 以 id 为主键；重名 = 拒收。
- 目录名建议与 id 一致（`02-agents/demo.fitness\`）；不一致时以 manifest.id 为准。

---

## §4 联邦通信（交办单 / 回执）

### 4.1 信箱

```
（逻辑位置；物理上位于存档区 _savegents\<id>\ 下，工具经解析器访问）
<id>/inbox/          待办交办单（总管投放）
<id>/outbox/         已处理回执（助理产出，总管收取）
<id>/inbox/done/     已完成交办单存档（处理完由助理移入）
```

### 4.2 交办单 task.json

```json
{
  "protocol": "agentcrew.protocol/v1",
  "task_id": "t-20260918-223015-a1b2",
  "from": "master",
  "to": "demo.fitness",
  "kind": "record",
  "created_at": "2026-09-18T22:30:15+08:00",
  "priority": "normal",
  "deadline": null,
  "request": {
    "utterance": "早餐吃了一碗牛肉面",
    "intent_hint": "diet",
    "slots": { "meal_type": "breakfast" },
    "context_refs": []
  },
  "reply_to": "outbox"
}
```

- `kind` 枚举：`record | query | report | remind | authorize | custom`。
- `request.utterance`：主人的原话（总管不改写，助理自行理解）。
- `request.slots`：总管已能确定的结构化字段（可空）。
- `context_refs`：授权快照等外部资料在 inbox 内的文件名。

### 4.3 回执 receipt.json

```json
{
  "protocol": "agentcrew.protocol/v1",
  "task_id": "t-20260918-223015-a1b2",
  "from": "demo.fitness",
  "status": "done",
  "created_at": "2026-09-18T22:30:41+08:00",
  "result": {
    "summary": "已记录早餐：牛肉面一碗，约 450kcal",
    "details": { "table": "diet_logs", "rows_added": 1 },
    "user_view": "记好啦，早餐约450大卡，蛋白27克"
  },
  "artifacts": [],
  "next_hints": "距蛋白日目标还差 63g"
}
```

- `status` 枚举：
  - `done` 完成；
  - `need_input` 需主人补充（`result.questions[]` 列出 1-2 个问题，总管代问代答后可下续单）；
  - `referred` 超出领域（`result.refer_to` 指向建议的助理 id 或 "master"）；
  - `refused` 拒绝执行（`result.reason` 人话原因 + `result.reason_code` 机器码，如 `no_authorization`、`out_of_domain`）；
  - `failed` 出错（`result.error` + `result.reason_code`，如 `io_error`、`schema_mismatch`，不吞异常）。
- `result.user_view`：给主人看的**一句话**（总管优先原文转述，保持各助理个性；总管可轻度润色但不得改事实）。

### 4.4 处理语义

1. 投放=原子写：先写 `.tmp` 再改名（防半截文件）。
2. 助理按 `created_at` 先到先处理；处理完把交办单移入 `inbox/done\`，回执写 `outbox\`。
3. 每张交办单**恰好一张回执**；`need_input` 的后续问答以新交办单延续（`task_id` 加后缀 `-r2`、`-r3`）。
4. **幂等防重**：助理见到 `task_id` 已存在于自己 `inbox/done\` → 直接以原回执内容再次回复（或忽略），绝不重复执行；总管未收到回执时，先查助理 `inbox/done\` 确认从未处理过才允许重发——**结果未知不重发**。
5. 总管收取回执后可删除 outbox 内容（已尽转达义务）；助理保留 inbox/done 作自己的台账。
5. 消息排队：主人回复到达时若总管不在岗，通道层落 `01-master/inbox-master\`（工作区级），下次上岗先清。

### 4.5 主动汇报（助理→总管的异步留言）

助理有周期性成果要给主人（如"本周体重降了0.4kg"）时，写 `outbox/report-<ts>.json`，格式同回执（`task_id` 用 `pull-<ts>`），总管上岗时统一转达。lite 模式下无此机制（直接对主人说）。

---

## §5 生命周期

| 阶段 | 动作 | 执行者 |
|------|------|--------|
| 安装 | zip 解压 / 文件夹就位 / git clone 到临时处 → 校验（§5.1）→ 放入 `02-agents\<id>\` | 面板或 `adopt.py` |
| 收编登记 | 校验通过 → 写 registry.json（id 主键）→ 总管向主人介绍 | `adopt.py` + 总管 |
| 暂停/恢复 | registry 中 `status: active ⇄ paused`；暂停=不派单不注册提醒，数据不动 | 总管/面板 |
| 放归 | 移出 `02-agents\` 至仓库内 `_standalone\` 放归区（数据随行），registry 注销，自动回 lite | `release.py` |
| 卸载 | 放归后删程序区；**默认保留数据**并提示主人数据位置；主人明说才删 | 面板/人 |
| 升级 | 仅替换程序区；`manifest.paradigm` 兼容则直接换，`data\` 不动；版本回退=换回旧程序区 | 人/AI |

### 5.1 收编校验清单（adopt.py 自动执行）

1. manifest.json 存在且通过 PARADIGM §2 的 schema 校验；
2. id 不与 registry 冲突；目录名与 id 不一致时以 id 归位；
3. AGENTS.md（助理章程）存在且含双模式自查条款；
4. `data\` 存在（没有则创建并放 `.gitkeep`）；
5. manifest 声明的每个 table 有对应 JSONL 文件或为空表（允许为空）；
6. manifest 声明的每个 tool 文件存在且 `python <tool> --selfcheck` 通过（工具须实现该参数）；
7. dashboard.main 指向的文件存在（若声明了 dashboard）；
8. 许可证文件存在（LICENSE 或 manifest.license 声明）。

任一不过 = 拒收并列出原因；允许 `--force` 仅限开发模式（写 registry 时标注 `unverified: true`）。

---

## §6 担保授权（跨助理数据互通的唯一通道）

### 6.1 流程

```
助理A在回执/交办处理中声明需要B的数据（need_data: B.<table>, purpose）
  → 总管向主人转达并请求批准
  → 批准：authz.py 从B的存档切片导出（托管=<save>/agents/B/data\<table>.jsonl；
         lite 放归助理=B 自带 _save\data\<table>.jsonl）→ 写快照 → 投放A的 inbox\context_refs
  → 拒绝：总管回复A "未授权"，A须以不依赖该数据的方式完成或 refused
```

### 6.2 快照文件 snapshot-<ts>.json

```json
{
  "protocol": "agentcrew.protocol/v1",
  "snapshot_id": "s-20260918-a1b2",
  "source_agent": "demo.fitness",
  "source_table": "sleep_records",
  "purpose": "职业顾问评估近期精力状态",
  "granted_by": "owner",
  "granted_at": "2026-09-18T22:40:00+08:00",
  "expires_at": "2026-09-19T22:40:00+08:00",
  "read_only": true,
  "rows": [],
  "row_count": 14
}
```

- 默认有效期 24 小时；`rows` 为导出时数据副本（此后源数据变化不回传）。
- 快照生成/投放全部记入 `01-master/authorizations.jsonl` 审计日志（谁、给谁、为什么、何时到期）。
- 助理收到快照按 `expires_at` 自查：过期即不得使用并删除。
- **助理间直接互读对方 `data\` = 重大违规**；审核轮会专项检查有无此模式。

---

## §7 提醒协议

- 助理在 manifest 用**人类语言**声明提醒（见 PARADIGM §2.7），不写 cron。
- 总管（或部署宿主）负责：翻译成具体时刻（参考主人作息）→ 在宿主层注册 → 到点投递。
- 投递内容：manifest 提供的固定文案（可含当日数据占位符，由总管填）或总管生成的一句话；**必须单行**（微信等通道约束由通道层负责兜底）。
- 通道：只走 IM。关机错过的 → 宿主开机后补发，前缀「您错过了：」。
- **可推送时段核验**：注册定时任务前与任务触发前，总管必须以 `05-scripts/dnd_check.py --at HH:MM` 核对主人档案 `notifications`（可推送窗口 + 额外勿扰）。违背时**不得静默改期或跳过**，须向主人确认；补发同样过核验，勿扰窗口内排队待发。
- 主人对任何提醒说"别提了"→ 总关 registry 对应 `reminders_enabled=false`，不改助理文件。
- **存档巡检任务**：总管上岗时在宿主层幂等注册（名字含「AgentCrew·存档巡检」便于识别去重），每 6 小时执行 `05-scripts\save.py watch`——干净=零输出零打扰；违规=按 §7 勿扰规则报主人并建议一键归档。与提醒同族：宿主没开则不触发，无静默补偿。

---

## §8 安全底线

1. 默认禁外发：助理与总管不得把主人数据发送到任何网络服务；联网能力必须 manifest `permissions.net` 显式声明且默认 `false`。
2. 写权限白名单（存档架构）：**一切增量信息只进 `_save\`**——助理只可写自己的存档切片（data/inbox/outbox）；总管只可写 `_save\master\` 与各切片信箱；**程序区（`_save\` 之外）对运行时只读**，`save.py watch` 与 doctor 负责确定性抓违规。
3. 删除类操作（删记录/卸载/清库）必须主人二次确认。
4. 快照只读、带有效期、全量审计。
5. 程序区变更（升级/热改）后必须过 `doctor.py`。

---

## §9 版本与兼容

- 本文 `agentcrew.protocol/v1`；助理 manifest 的 `protocol` 字段声明它遵守的版本。
- 兼容规则：主版本号不同 = 不兼容（收编拒收）；次版本号向上兼容。
- 破坏性变更须在 `00-docs/PROTOCOL.md` 顶部维护 CHANGELOG。
