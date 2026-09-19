# PARADIGM — 子助理开发范式（生态标准）

> 版本：agentcrew.paradigm/v1 ｜ 协议依赖：agentcrew.protocol/v1
> 读者：任何想造一个子助理的**人或 AI**。照本文做完，你的助理即可被任何 AgentCrew 总管收编；离开总管也能独立干活（lite 模式）。
> 一句话哲学：**子助理 = 一家独立的小公司**。有章程（AGENTS.md）、有营业执照（manifest.json）、有员工手册（人设）、有工具箱（tools）、有账本（data）、有门面（dashboard）。被总管收编是"加入集团"，搬走即"独立经营"，全程零配置。

---

## §0 四条公理（违反任何一条都不算合格助理）

1. **自包含**：整包拷走即能用，不依赖总管仓库的任何文件。
2. **管辖靠位置**：向上找总管（判定算法见 PROTOCOL §2）定模式；不写任何"我在被管理"的隐藏状态。
3. **数据神圣**：`data\` 是主人的，程序区升级/卸载/重装永不触碰；格式人能直接打开读。
4. **边界诚实**：`domain` 说清管什么，`boundaries` 说清不管什么；超界老实转介，不硬答。
5. **程序区只读**：部署后的一切增量信息（数据/信箱/档案/看板生成物）只进宿主的存档区 `_save\`（lite 助理则进自己的 `_save\`）——助理目录里的程序文件对运行时 AI 永远只读；删除存档 = 整个项目回到原点。

---

## §1 项目结构标准

```
<assistant>/                      ← 目录名 = manifest.id（如 demo.fitness）
├── manifest.json                 # 营业执照（§2，必填）
├── AGENTS.md                     # 章程（§3，必填；任何 AI 会话在此目录启动即"成为"这个助理）
├── persona.md                    # 人设：对主人说话的性格（§4，必填）
├── docs/                         # 设计文档（可选；原版迁移分析等放这里）
├── tools/                        # 工具箱（§6；至少含 data.py）
│   ├── data.py                   # 数据读写通用工具（必填）
│   └── <domain>.py               # 领域工具（按需）
├── dashboard/                    # 看板（§7，可选）
│   └── index.html
├── inbox/  outbox/               # 联邦信箱（PROTOCOL §4，必填目录）
│   └── done/                     # （inbox 内）
├── data/                         # 数据区（§5；git 忽略；随包发布时为空+ .gitkeep）
└── tests/                        # 自测脚本（§10 验收用，推荐）
```

---

## §2 manifest.json 全字段规范

```jsonc
{
  "protocol": "agentcrew.protocol/v1",       // 必填，遵守的协议版本
  "paradigm": "agentcrew.paradigm/v1",       // 必填，遵守的本范式版本

  "id": "demo.fitness",                    // 必填，<作者>.<名字>，全局唯一（PROTOCOL §3）
  "name": "健身助理",
  "version": "1.0.0",                        // 语义化版本：破坏性改动升主版本
  "description": "一句话说清它是干什么的",
  "author": "demo",
  "license": "MIT",

  "domain": ["体重与身体数据记录", "饮食记录与营养估算", "训练记录"],
  "boundaries": ["医疗诊断与用药建议", "食物过敏判断"],   // 明确不管什么，防总管错派

  "routing_examples": {                      // 给总管路由用的正反例（各≥3条）
    "positive": ["早上称了168斤", "午饭吃了鸡胸肉西兰花", "今天练了胸，卧推80公斤5组"],
    "negative": ["帮我把这个月吃饭开销汇总一下", "明天下午提醒我开会", "我心情不好"]
  },

  "capabilities": ["record", "query", "report", "remind"],   // ⊆ record/query/report/remind/custom
  "tools": ["tools/data.py"],                // 本助理工具清单（缺省按 ["tools/data.py"]）；收编时逐个 --selfcheck
  "first_run": ["tools/xxx import-seed"],    // 可选：收编时执行一次的初始化命令（幂等为宜），如导入种子库
  "tables": [                                // §5 数据表声明（可空列表）
    {
      "name": "diet_logs",
      "description": "每餐饮食明细",
      "columns": [
        {"name": "meal_type", "type": "string", "required": true,  "enum": ["breakfast","lunch","dinner","snack"]},
        {"name": "food_name", "type": "string", "required": true},
        {"name": "portion",   "type": "string", "required": false},
        {"name": "calories",  "type": "number", "required": false, "unit": "kcal"}
      ]
    }
  ],

  "reminders": [                             // §8 提醒：人类语言，不写 cron
    {
      "name": "晨间称重提醒",
      "rule": "每天早上提醒称体重",
      "default_time": "08:30",
      "message": "早呀～该上秤啦，站上去就知道这周的趋势～",
      "when_data_missing": ["body_stats"],
      "priority": 1                          // 同刻多条冲突时小者优先
    }
  ],

  "dashboard": {                             // §7，没有看板就整个省略
    "main": "dashboard/index.html",
    "up": true,                              // true=愿意被总管面板收录跳转；false/缺省=不收录
    "data_endpoint": "tools/data.py serve"   // 看板取数的标准命令（可选）
  },

  "permissions": {                           // §9 安全底线：缺省全关
    "net": false,                            // 是否允许联网
    "write_outside_data": false              // 是否可写自己目录之外（几乎总应为 false）
  },

  "onboarding": [                            // 助理级初始化问题（主人启用该助理时总管代问）
    {"q": "您的身高？", "slot": "height_cm"},
    {"q": "目标体重多少？", "slot": "target_weight_kg"}
  ]
}
```

### 校验规则（adopt.py 逐条执行）

- **必填（缺=拒收）**：`protocol`/`paradigm`（主版本 v1 兼容）、`id`（合法命名不冲突）、`name`、`version`、`description`、`domain`；`dashboard.main` 指向的文件须存在（若声明）；reminders 的 `rule` 须为人类语句、`default_time` 须为 `HH:MM`；tables 字段类型合法。**必填刻意极简**（行业共识）：少填少错，收编门槛低。
- **警告（不拒收，doctor 可见）**：`capabilities` 缺省按 `record/query`；`routing_examples` 不足各 3 条；`author`/`license` 未填。**routing_examples 是路由质量的生命线**——不强制但总管路由不准时先查它。

---

## §3 章程 AGENTS.md（助理的宪法）

必含四节，写法约束：

1. **你是谁**：一句话身份 + 领域边界（与 manifest.domain 一致，不复制粘贴出两套真相——直接写"见 manifest"亦可）。
2. **管辖自查（第一动作）**：开工先执行 PROTOCOL §2 判定，声明"我现在是 lite 还是托管"。模板句：
   > 从本目录逐级向上找「AGENTS.md + 01-master\ + 00-docs\PROTOCOL.md」三者齐备的目录；找到=托管模式，服从总管；找不到=lite 模式，自己当家。
3. **两套行为**：lite（直接对主人服务+自查提醒）与托管（按交办单干活、超界写 referred 回执、周期成果写主动汇报）分别写清。
4. **红线**：数据只进自己 `data\`；不读别家（除非有效快照）；不联网（除非 permissions.net）；不装教练/医生/理财师——记录、计算、趋势、客观陈述。

风格要求：全中文、≤150 行、不许出现绝对路径、不许出现具体某人的名字或城市。

**渐进披露三级**（控制任何 AI 会话的上下文成本）：章程只写"是什么、怎么行为"（常驻摘要级）；manifest 字段细节按需查；数据流水永远按需经工具查询，**任何文件都不该把数据行内嵌进来**。

---

## §4 人设 persona.md

- 只管"怎么说话"：称呼、口吻、确认风格、提醒风格、玩笑尺度。
- 与章程分离：改性格不改制度。
- lite/托管通用——同一颗心，两种场合。

---

## §5 数据规范（逻辑与物理分离，存档架构）

- **逻辑层**：助理在 manifest.tables 声明表；"我的数据"是逻辑概念，助理**不感知也不依赖物理路径**。
- **物理层**：一切落盘经 `tools\data.py` 的存档解析器——托管模式 → 实例 `_savegents\<id>\data\`；lite 模式 → `<助理自身>\_save\data\`。环境变量 `AGENTCREW_SAVE` 可把整个存档外置。
- **一表一文件**：`<table>.jsonl`，每行一条记录；公共字段 `_id` / `created_at`；业务时间 `recorded_at`（ISO8601 本地时区）。
- 配置放 `data/config.json`（改参数=改数据≠改代码）；派生表必须可全量重建（`_derived` 头行声明）。
- 删除 = 移入存档区 `data	rash\`；**信箱**（inbox/outbox）同样物理位于存档切片。
- **存档版本承诺**：manifest 声明 `save_schema`（缺省 1）；程序升级若改表结构，必须同步提供迁移（宿主 `migrate_save.py` 阶梯执行）——**旧档永远能被新程序读**，读不了是程序的 bug，不是主人的损失。
- **监控不靠自觉**：宿主提供 `save.py watch` 确定性扫描程序区违规增量（可挂计划任务/CI/面板启动）；doctor 体检含同款检查。

## §6 工具规范（tools\）

- 只用 **Python 标准库**；每个工具独立 CLI，参数化，不依赖交互输入。
- 必须实现 `--selfcheck`（自检：能连自己的 data、schema 匹配、写测试行再回滚）——收编校验靠它。
- 通用工具 `data.py` 至少支持：`append <table> --json '...'`、`query <table> --where ... --last N`、`stats <table>`、`serve`（给看板输出 JSON）。
- 约定：成功退出码 0 且 stdout 输出 JSON（`{"ok": true, ...}`）；**技术性失败**（IO/schema/参数错）退出码非 0 且 `{"ok": false, "error": "..."}`；**业务性失败**（need_input 类，如 unknown_food/need_portion）允许退出码 0 但必须 `ok:false + reason_code`——它们是正常对话流的一部分，不是错误；写操作支持 `--dry-run`。
- 工具是"助理的手"，不是"助理的脑"：判断与生成留给 AI 会话，工具只做确定性计算与落盘。
- **单写者假设**：同一助理的 data\ 同一时刻只有一个执行体在写（lite=本人，托管=处理交办单的那个会话）。读-改-写型操作（update/delete/派生重建）与并发 append 之间无锁——这是设计边界，不是缺陷；多执行体并行写同一助理属违规用法。

---

## §7 看板规范（GUI 统一范式）

- 位置 `dashboard/index.html`，**静态 HTML+CSS+原生 JS，零构建零外部 CDN**（内网可用、永久可开）。
- 统一皮肤：`<link rel="stylesheet" href="../../04-panel/skin/butler.css">`——住进收编区时相对路径命中总管皮肤；lite 模式下回退本目录 `dashboard/butler.css`（模板自带一份拷贝）。**要求两种位置都能渲染**。
- 页面必含：页头（助理 name+version+id）、数据卡片 ≥2 个、一张趋势或分布图（纯 CSS/SVG 即可，不引图表库）、数据明细表。
- 取数：调 manifest.dashboard.data_endpoint（本地受信进程执行后注入或 fetch 本地端口）；看板自身**不直接读 data\ 文件**（file:// 跨域限制），由 `data.py serve` 生成 `dashboard/data.json` 供拉取。
- 收录开关：manifest `dashboard.up: true` → 总管面板"全家福"出现跳转卡片；`false`/缺省 → 不收录（没有任何 shame，纯个人选择）。
- 禁止：外链字体/统计脚本/任何网络请求（permissions.net=false 的物理体现）。

---

## §8 提醒声明规范

- 一条提醒 = `{name, rule(完整人类语句), default_time, message, when_data_missing?, priority?}`。
- `rule` 写得越"人话"，总管翻译越准：✅「每天早上提醒称体重，主人超过 10 点没记就顺带催一句」；❌「0 8 * * *」。
- `when_data_missing` 指向自家表：晚间检查类提醒由总管按"缺什么提醒什么、只提最缺一项"执行。
- 提醒文案遵守通道礼仪：单行、≤100 字、像人话不像系统通知。

---

## §9 安全底线（继承 PROTOCOL §8，助理视角重述）

0. **数据黄金源**：凡回答涉及主人数据，必须当场经工具查询后作答；禁止凭对话记忆/印象报数字——本生态第一铁律（源于初代健身助理"拿记忆当数据库"的失败教训，所有助理章程必须含此条）。
1. 主人数据不出本机；`permissions.net=false` 时工具不得 import 任何网络库发起请求。
2. 只写自己 `data\ inbox\ outbox\`；对别家目录只读快照、且仅限有效期内。
3. 删除先移 trash；一切落库操作可回放（JSONL append-only 精神）。
4. 不假装专业：医疗/法律/投资口径一律"客观信息+建议咨询专业人士"。

---

## §10 上岗验收清单（adopt 后人工/AI 过一遍）

- [ ] 纯拷贝到任意空目录，lite 模式下：能对话、能记录、能查询、能自答"你是谁"
- [ ] 超界问题得到诚实转介/拒绝，没有硬答
- [ ] manifest 校验全过；routing_examples 正反例贴切（拿正例问总管能路由到你）
- [ ] 工具 `--selfcheck` 全绿；`--dry-run` 不落盘
- [ ] data\ 里数据打开是人话；trash 机制工作
- [ ] 看板两种位置（收编区/lite）都能打开；无任何网络请求
- [ ] 提醒规则总管能翻译成时刻表；文案单行像人话
- [ ] 升级演练：替换程序区，data\ 原样无损
- [ ] 卸载演练：程序区删除后，data\ 仍完整在原处或已随迁

---

## §11 发布规范（生态）

- **git 仓库即发布单元**：任何助理项目独立成仓（或作为模板 fork），clone 下来放进 `02-agents\` 即可收编。
- 版本：语义化（破坏 manifest 兼容=升主版本）；`CHANGELOG.md` 推荐。
- 必带 LICENSE；建议带 `README.md`（用途+对话示例截图）。
- 提交给总管面板安装时支持三种形态：`.zip` 包 / 本地文件夹 / git URL（总管 clone 后走同一校验）。

---

## §12 造助理的最短路径

1. 面板「安装收编」页 ⓪「从模板新建」填 id/名字/描述一键生成，或 `cp -r 03-template <你的目录>`；
2. 改 manifest（id/domain/routing/tables/reminders）→ 写章程四节 → 调人设语气；
3. 设计 2-5 张表，让 AI 生成 `tools/<domain>.py`（照 §6 规范）；
4. 跑 §10 清单 → `adopt.py` 收编 → 总管介绍你入编。

> 给 AI 的指令模板：「请严格按 00-docs/PARADIGM.md 范式，在 02-agents/<id> 造一个<b领域>助理，需求如下：…」
