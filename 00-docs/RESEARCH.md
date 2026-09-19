# 同类项目调研（RESEARCH）

> 调研日期：2026-09-17。调研对象为 AgentCrew（联邦式个人助理框架）的直接可比项目与范式来源。所有事实均附信源 URL；无法核实的细节如实标注「未能核实」。

## 1. 各项目逐个分析

### 1.1 NousResearch/hermes-agent

**是什么**：Nous Research 出品的自我改进型个人 AI agent（MIT 协议，约 246k stars）。模型无关（300+ 模型可切换），可在 $5 VPS、本地或 serverless 上运行；用户通过 Telegram/Discord 等消息平台指挥云端 agent。官方提供从 OpenClaw 一键迁移的命令（`hermes claw migrate`），可视为 OpenClaw/Claude Code 一脉的演进品。（https://github.com/NousResearch/hermes-agent ）

**核心机制**：
- **Skill 格式**：`~/.hermes/skills/<类别>/<技能名>/` 目录，必需 `SKILL.md`，可选 `references/ templates/ scripts/ examples/ assets/`。frontmatter 字段：`name`、`description`（≤60 字符）、`version`、`platforms`（限制操作系统，不兼容则自动隐藏）、`metadata.hermes.tags/category`、`metadata.hermes.requires_toolsets / fallback_for_toolsets`（依赖工具集可用时才显示/被替代时隐藏）、`metadata.hermes.config`（非密钥配置项）、`required_environment_variables`（name/prompt/help/required_for，安装时安全录入并透传沙箱）。遵循 agentskills.io 开放标准。（https://hermes-agent.nousresearch.com/docs/user-guide/features/skills ）
- **渐进披露三级加载**：Level 0 `skills_list()`（约 3k tokens 的名称+描述列表）→ Level 1 `skill_view(name)`（全文）→ Level 2 `skill_view(name, path)`（按需读 references/）。查询成本与答案大小成正比而非源材料大小。（同上）
- **Profile（多助理隔离）**：一个 profile = 一个独立 Hermes home 目录（`~/.hermes/profiles/<name>/`），内含 `config.yaml`、`.env`、`SOUL.md`、memories、sessions、skills、cron jobs、state.db、gateway 状态。实现原理是 `HERMES_HOME` 环境变量切换根目录。凭证完全独立、绝不继承；bot token 独占——"a bot can only belong to one profile"，两个 profile 抢同一 token 会被拒绝并报冲突。（https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/profiles.md ）
- **网关多通道**：每个 profile 可运行自己的 gateway 进程（systemd/launchd/Docker 服务），接入 Telegram、Discord、Slack、Matrix、Signal、WhatsApp（轮询类）与 Twilio、LINE、Teams、飞书 webhook 等（回调类）。支持多路复用网关：一个进程服务所有 profile，会话键按 `agent:<profile>:…` 命名空间隔离，`gateway.profile_routes` 把特定 guild/频道/群组路由到不同 profile。（https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/multi-profile-gateways.md ）
- **cron 定时任务**：每 profile 独立 cron 存储；复用模式下由共享调度器打点；投递仅允许映射到该 profile 的已启用路由（需含 chat_id/thread_id），投到未授权频道会被拒绝；`HERMES_CRON_TIMEOUT`、`HERMES_CRON_MAX_PARALLEL` 等调优项从各 profile 自己的 `.env` 读取。（同上）
- **记忆/持久化**：单一 SQLite `state.db`（WAL 模式）存会话全量历史；MEMORY.md/USER.md 只在会话启动时注入；会话结束前自动保存记忆与技能；`session_search` 用 FTS5 全文检索（含 CJK）召回掉出上下文的内容。文档明确警告：网关会话永不过期会导致记忆机制失效，建议任务完成即 `/new` 开新会话。（https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md ）
- **Bot 模式**："Bot 就是一个 profile"，桌面 UI 只是薄层。群聊房间 2–6 个 Bot 同处，@提及者回复；每轮最多 3 轮、单次最多 10 条消息的硬上限防失控。投递失败带机器可读原因码（如 `provider_auth_or_access`、`context_overflow`），瞬时故障最多重试一次，auth/quota 类从不自动重试，**结果未知的投递不要重发**。房间权威转移有防脑裂流程。（https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/bot-mode.md ）
- **egress（更正）**：实测该目录下的 `egress/` 是**出站网络代理**而非通知外发——`iron-proxy` 是 TLS 拦截代理，沙箱内只持有不透明代理令牌，真实 API 密钥永不离开主机。（https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/egress/index.md ）通知外发实际由网关通道 + cron 投递承担。

**对 AgentCrew 可借鉴**：profile=目录即状态的隔离模型；token 独占与冲突检测；失败原因码+不重发原则；渐进披露三级；技能写入审批暂存目录（`pending/skills/` + approve/reject）；cron 投递白名单。
**明确不取**：SQLite 单库中心化状态（AgentCrew 坚持纯文本 JSONL）；复杂的多路复用网关；云端 VM 架构。

### 1.2 OpenClaw（原 Clawdbot/Moltbot）

**是什么**：自托管个人助理框架（MIT，OpenClaw Foundation 维护），架构以 Gateway 为中心——"The Gateway is the single source of truth for sessions, routing, and channel connections"，一个网关进程服务 Discord/iMessage/Signal/Slack/Telegram/WhatsApp/WebChat 等全部聊天渠道。（https://docs.openclaw.ai/ ）

**核心机制**：
- **Workspace 即 agent 的家**：默认 `~/.openclaw/workspace`，"treat it as memory"。标准文件：`AGENTS.md`（操作指令/规则/优先级，每会话加载）、`SOUL.md`（人格语气边界，每会话加载）、`USER.md`（用户画像，带日期条目，独立 4000 字符预算）、`IDENTITY.md`、`BOOTSTRAP.md`（首次运行仪式，完成后应删除）、`memory/YYYY-MM-DD.md`（每日记忆）、`MEMORY.md`（精选长期记忆）、`skills/`（workspace 专属技能，优先级最高）。注入有截断保护：单文件默认 20000 字符、总量 60000。（https://docs.openclaw.ai/concepts/agent-workspace ）
- **记忆**："The model only remembers what gets saved to disk; there is no hidden state." 分层：常驻层 MEMORY.md 每会话注入；工作层日文件只索引不注入，靠 `memory_search`（向量+关键词混合）与 `memory_get` 按需召回。压缩（compaction）前自动跑一次静默 memory flush 提醒 agent 存盘。后台 "dreaming" 进程把日记忆中通过分数/召回频率门槛的候选固化提升进 MEMORY.md，审查记录写 `DREAMS.md`。涉及审批/移交/过期的记忆要求写明何时可行动、何时过期、来源与所有者；偏好变更时**原地覆盖而非追加矛盾条目**。（https://docs.openclaw.ai/concepts/memory ）
- **技能**："Teach the agent repeatable procedures it loads on demand"，含加载、优先级、门控、allowlist 与环境注入机制。（https://docs.openclaw.ai/tools/skills ）
- **自动化**：automations（`openclaw cron` 为别名）支持一次性时间、cron 规则、事件触发器（condition watchers）、入站 webhook（`POST /hooks/wake`）；输出可送达聊天频道、webhook 或命令。（https://docs.openclaw.ai/automation/cron-jobs ）

**对 AgentCrew 可借鉴**：workspace 文件族与分层加载预算；BOOTSTRAP 一次性仪式；"记忆=写进磁盘的文件"哲学与 dreaming 式固化；action-sensitive 记忆元数据；heartbeat 与 automations 的分工。
**明确不取**：Gateway 单一事实来源的中心化架构（与 AgentCrew 联邦式相反）；人格文件与数据混在同一 workspace。

### 1.3 elizaOS（Eliza）

**是什么**：TypeScript 多 agent 框架，以 character 文件（JSON/TS）定义 agent 人格与能力，插件以 npm 包扩展。

**核心机制**：Character（静态蓝图）与 Agent（运行时实例，增加 `enabled`/`status`/时间戳）分离。character 字段：`name`、`bio`（复杂人格建议数组分行）、`system`（覆盖默认系统提示）、`templates`、`adjectives`、`topics`、`knowledge`（字符串/文件 `{path, shared}`/目录，`shared: true` 表示所有 agent 可用）、`messageExamples`（二维数组对话样例，用户用 `{{user}}` 占位）、`postExamples`、`style`（`all`/`chat`/`post` 分场景）、`plugins`（npm 包名或本地路径数组）、`settings`（model、temperature、voiceEnabled、avatar 等）、`secrets`（要求从环境变量读取，勿硬编码）。插件按环境变量条件加载（展开运算符判断 API key 存在性）、按依赖顺序加载，通过 settings/secrets 取配置与凭证。（https://docs.elizaos.ai/agents/character-interface ）

**对 AgentCrew 可借鉴**：声明（manifest）与运行实例分离；`knowledge.shared` 显式共享标记；`secrets` 与配置分离；bio 用数组化条目。
**明确不取**：以人格化对话为中心的产品方向（源自 AI 网红场景）；插件=代码包（npm 依赖）的重机制——AgentCrew 子助理是独立项目+文件协议，不是进程内插件。

### 1.4 Anthropic Claude Skills（SKILL.md）

**是什么**：Anthropic 的 agent 技能开放格式（agentskills.io 标准的超集），SKILL.md + 附属文件构成目录。

**核心机制**：frontmatter 全部可选，仅推荐 `description`；字段包括 `name`（默认取目录名）、`description`（与 `when_to_use` 合计截断 1536 字符）、`when_to_use`、`argument-hint`、`disable-model-invocation`（仅用户可触发）、`user-invocable`（仅模型自动调用）、`allowed-tools`/`disallowed-tools`、`model`、`context: fork`（隔离子代理运行）、`background` 等。**渐进披露三级**：① 描述常驻上下文（模型知道有哪些技能）；② 调用时才加载 SKILL.md 正文（压缩后每技能保留 5000 token、总预算 25000）；③ 辅助文件（reference.md 等）按需读取，脚本被执行而非载入上下文。正文建议 ≤500 行，细节移到附属文件。作用域由目录位置决定（个人/项目/插件级）。（https://code.claude.com/docs/en/skills ）

**对 AgentCrew 可借鉴**：manifest 极简主义（必填字段最少化）；三级渐进披露直接映射到 AgentCrew 的「manifest 摘要 → AGENTS.md 章程 → 数据表/附属文件」；description 截断预算；调用控制字段（谁能触发）。
**明确不取**：纯工具型技能定位，无数据表/提醒/看板等个人助理域概念。

### 1.5 Khoj

**是什么**：开源自托管"AI 第二大脑"（AGPL-3.0，约 37k stars，Python/Docker）。支持接入 Obsidian/Notion/PDF 等文档做语义检索，客户端覆盖 Web、Obsidian、Emacs、桌面、手机、WhatsApp。（https://github.com/khoj-ai/khoj ）

**核心机制**：自定义 agent = 自定义知识库 + 人设（persona）+ 聊天模型 + 工具四要素（https://github.com/khoj-ai/khoj ）；automations 支持定时/循环任务，自动产出"个人新闻简报与智能通知投递到你的收件箱"（personal newsletters and smart notifications delivered to your inbox）。agent 字段 schema 与 automation 调度实现细节：官方文档站相关页面返回 404，**未能核实**。（https://docs.khoj.dev/ ）

**对 AgentCrew 可借鉴**：把定时自动化的产物当" Newsletter/通知"投递到收件箱的产品心智；agent 四要素（知识/人设/模型/工具）作为 manifest 分类参考。
**明确不取**：以向量索引为中心的重型技术栈；单体应用形态（非插件/联邦范式）；AGPL 许可证（若 AgentCrew 追求更宽泛的采用）。

### 1.6 泛搜索：行业范式收敛（Agent Plugins 1.0.0 等）

**是什么**：2026 年 Vercel 联合 OpenAI、AWS、Cursor、GitHub、Microsoft 等发布 **Agent Plugins 1.0.0** 开放标准——把 SKILL.md 技能、MCP 服务器打包为可移植插件的厂商中立规范。（https://vercel.com/blog/introducing-agent-plugins ；https://thenewstack.io/agent-plugins-open-standard/ ）

**核心机制**：`plugin.json` manifest 最低只要求 `$schema` + `name` 两个字段；固定目录布局 `skills/` + `mcp.json` + 客户端命名空间目录（如 `com.example.client/`，其他客户端忽略，避免私有行为污染通用格式）；规范刻意只定义「发现、验证、加载」的确定性契约，安装/分发/策略留给各客户端；**组件独立验证**——单个组件无效不使其他组件失效；v1 仅涵盖 Skills+MCP。Microsoft 的 Agent-Skills 仓库亦用 `plugin.json` 支持一键安装。（https://github.com/MicrosoftDocs/Agent-Skills ）

**对 AgentCrew 可借鉴**：manifest 字段极简；客户端命名空间隔离私有扩展；组件独立验证；目录布局即契约。
**明确不取**：面向开发者工具链（IDE/CLI）的定位，不涉及个人数据与生活助理场景。

**关于"子助理即插件、数据隔离、目录即状态"**：泛搜索未发现与 AgentCrew 完全同构的项目——hermes profile 是"多份同一个 agent"，OpenClaw multi-agent 是"一个网关路由到多个 workspace"（https://docs.openclaw.ai/concepts/agent-workspace ），均未把子助理做成可独立执业、以文件协议协作的联邦成员。**未能核实**存在更接近的同类项目。

## 2. 范式启示清单

### manifest 字段
1. **极简必填集**：Agent Plugins 仅强制 `$schema`+`name`，SKILL.md 仅推荐 `description`。AgentCrew 的 manifest.json 应划分"必填/可选"，可选字段缺省即有合理默认，降低子助理作者的犯错成本。
2. **声明可见性条件**：借鉴 hermes 的 `requires_toolsets`/`platforms`/`fallback_for_toolsets`——manifest 可声明依赖（通道、环境变量、数据表），条件不满足时总管自动隐藏该子助理而非报错。
3. **声明与运行时分离**：elizaOS 的 Character vs Agent。manifest.json 是静态"法人资格证"，运行产生的状态一律写入数据文件，绝不回写 manifest。
4. **组件独立验证**：单个子助理 manifest 损坏不应拖垮整个联邦——总管逐个加载、逐个降级（标记为不可用而非崩溃）。

### 章程写法（AGENTS.md）
5. **分层章程**：OpenClaw 把规则（AGENTS.md）、人格（SOUL.md）、用户画像（USER.md）分成三个文件各给预算。AgentCrew 章程建议分节：职责边界 / 数据表说明 / 提醒规则 / 对外协议，每节独立可替换。
6. **注入预算与截断保护**：OpenClaw 单文件 20000 字符、总 60000；SKILL.md 建议 ≤500 行。章程应规定长度预算，超限即拆分到按需加载的附属文件。
7. **一次性仪式文件用完即删**：BOOTSTRAP.md 模式——初始化向导与常驻章程分离，避免章程被初始化垃圾撑爆。
8. **授权条目带元数据**：OpenClaw 的 action-sensitive memories（何时可行动/何时过期/来源所有者）与"原地覆盖而非追加矛盾条目"——直接适用于 AgentCrew 的担保授权快照设计。

### 数据格式
9. **没有隐藏状态**：OpenClaw "model only remembers what gets saved to disk"。AgentCrew 的纯文本 JSONL 路线被同行验证：状态即文件，天然可审计、可 git、可迁移。
10. **分层记忆**：常驻层（小而精、每次注入）+ 工作层（日文件/流水、按需检索）。JSONL 流水对应工作层，应定期蒸馏出摘要型常驻文件。
11. **上下文成本与数据规模解耦**：hermes 三级渐进披露 + FTS5 检索。读取协议应为"摘要常驻、流水按需"，绝不全量载入 JSONL。
12. **导出与互操作**：hermes sessions 可导出 jsonl/md/html 且能与 Claude Code/Codex 互导。纯文本格式的红利必须产品化成导出命令。

### 通信协议
13. **文件即消息**：OpenClaw 用文件做记忆；AgentCrew 的 inbox/outbox 交办单/回执更进一步——把 agent 间通信也做成文件，天然持久化、可人工介入、可 diff。业界无直接同款，是差异化点，应坚持。
14. **通道独占与冲突检测**：hermes "a bot can only belong to one profile"，重复 token 拒绝启动。托管/lite 双模式切换时必须防止同一消息通道被两处同时抢占。
15. **失败带原因码、结果未知不重发**：hermes 的机器可读原因码（`provider_auth_or_access` 等）与瞬时故障最多重试一次的原则——回执应含 `status` + `reason_code`，避免重复交办造成重复副作用。
16. **命名空间隔离**：hermes 复用网关的 `agent:<profile>:…` 会话键——总管收编多个子助理后，内部信箱路径与交办单 ID 必须按子助理命名空间隔离。

### 提醒
17. **调度与投递解耦，投递需白名单**：hermes cron 只投递到已映射路由的频道、未授权频道拒绝；OpenClaw automations 支持频道/webhook/命令多种输出。manifest 声明提醒时应同时声明投递目标，投递失败可解释。
18. **自动任务设防失控上限**：hermes 群聊每轮最多 3 轮、单次 10 条消息。子助理的自动提醒/循环任务应内置频率与输出量上限。

### 看板
19. **看板=派生视图**：未发现把看板作为 agent 框架一等公民的先例（**未能核实**有同类）。最接近的心智是 hermes Bot 面板（每助理一行+最新动态预览）与 Khoj 的 newsletter/inbox。看板应由子助理数据表实时派生，而非独立数据源，保证单一事实来源。

### 安全
20. **凭证隔离 + 不透明代理**：hermes egress/iron-proxy 让沙箱只持不透明令牌、真实密钥不出主机。lite 模式子助理独立持凭证；托管模式下凭证应留在总管层，子助理只拿代理令牌。
21. **写入审批暂存区**：hermes `skills.write_approval` 把待写内容存 `pending/` 目录，人工 approve/reject。担保授权快照可加同样的"待审批暂存"环节。
22. **私密数据不进共享上下文**：OpenClaw MEMORY.md 只在私密主会话加载、不进群组。子助理数据表默认互不可见，共享需在 manifest 显式声明（参考 elizaOS `knowledge.shared` 标记）。

## 3. 差异化定位

上述项目的共同假设是"一个 agent 进程 + 一堆挂载的能力"（skills/plugins 是 agent 的附件），或"一个中心网关 + 多份会话"（hermes profile、OpenClaw multi-agent 本质都是同一个 agent 程序的多个实例，共享同一套代码与通道设施）。AgentCrew 把这个假设倒了过来：**能力即主体**——每个子助理本身是带章程（AGENTS.md）、自有数据表（纯文本 JSONL）、自有提醒与看板的完整"个人助理"，manifest.json 是它的法人资格证，inbox/outbox 纯文件协议让助理之间像同事一样互发交办单与回执，担保授权快照约束越权，托管/lite 双模式让同一份助理代码既可以被总管收编（住进收编区）也可以独立执业（单干）。数据隔离与文件协议在上述项目中至多是隐式副作用，在 AgentCrew 中是框架的第一承诺；hermes/OpenClaw 是"一个管家雇一堆临时工具"，elizaOS 是"一个剧组多个角色共用设施"，AgentCrew 则是"一家总管公司与多名持照独立承包商的联邦"。

## 附：信源清单

- https://github.com/NousResearch/hermes-agent
- https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/profiles.md
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/multi-profile-gateways.md
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/bot-mode.md
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/egress/index.md
- https://docs.openclaw.ai/
- https://docs.openclaw.ai/concepts/agent-workspace
- https://docs.openclaw.ai/concepts/memory
- https://docs.openclaw.ai/tools/skills
- https://docs.openclaw.ai/automation/cron-jobs
- https://docs.elizaos.ai/agents/character-interface
- https://code.claude.com/docs/en/skills
- https://github.com/khoj-ai/khoj
- https://docs.khoj.dev/
- https://vercel.com/blog/introducing-agent-plugins
- https://thenewstack.io/agent-plugins-open-standard/
- https://github.com/MicrosoftDocs/Agent-Skills
