# 架构 — AgentCrew 是怎么运转的

> 本文面向想读懂/改代码的开发者。只想用 → 看 [README](../README.md)；想造自己的助理 → 看 [PARADIGM](PARADIGM.md)。

## 总览

```mermaid
flowchart LR
    Owner([主人])

    subgraph Host["宿主运行时（OpenClaw / Hermes / zcode — 可替换）"]
        M[<b>总管 Agent</b><br>章程 AGENTS.md<br>路由 · 拆单 · 择时 · 汇报]
        CRON[[哑时钟<br>manifest 人话规则 → cron]]
    end

    subgraph Crew["02-agents 顾问团（随插随拔）"]
        F[健身顾问]
        S[样板顾问]
        N[你的下一块积木…]
    end

    Panel[管家面板<br>本地网页 · 127.0.0.1]

    IM[IM 通道<br>微信 / Telegram / …]

    Owner <-- 日常对话 --> IM
    IM --> M
    CRON -. 到点直推固定文案 .-> IM
    M <-- 交办单 / 回执<br>（纯文件协议） --> F
    M <-- 交办单 / 回执 --> S
    M <-- 交办单 / 回执 --> N
    M --- Panel
    F --- Panel
```

三个角色，三种"存在方式"：

| 角色 | 存在方式 | 谁读它 |
|---|---|---|
| 总管 | 一份章程（AGENTS.md）+ 存档里的名册与档案 | 任何 AI 会话读章程即上岗 |
| 顾问 | 完整的独立项目（manifest+章程+人设+工具+看板） | 交办单来了就干活 |
| 存档 | `_save\` 文件夹（一切增量信息） | 只有工具经解析器读写 |

## 一次对话的生命周期

```mermaid
sequenceDiagram
    participant O as 主人(IM)
    participant M as 总管
    participant A as 顾问(inbox/outbox)
    participant T as 工具(存档解析器)

    O->>M: "吃了一碗面还花了 30 块"
    M->>M: 归属判断 → 涉及两家 → 拆单
    M->>A: 交办单 t-001（饮食部分）
    M->>A: 交办单 t-002（花费部分 → 转介财务，暂无）
    A->>T: data.py append（schema 校验+原子写）
    T-->>A: ok
    A->>A: 单子移入 inbox/done（台账）
    A-->>M: 回执 done + user_view"记好了：一碗面"
    M->>O: 一句话确认（按所选人格）
    Note over M,T: 结果未知不重发：回执丢失时先查 inbox/done 台账
```

## lite / 托管：位置即管辖

```mermaid
flowchart TD
    Start([顾问项目被放到某处]) --> Up{向上找：<br>AGENTS.md + 01-master + PROTOCOL<br>三者齐备的目录？}
    Up -- 找到 --> Managed[<b>托管模式</b><br>听总管调度：接交办单、<br>提醒归总管择时、超界转介]
    Up -- 没找到 --> Lite[<b>lite 模式</b><br>自己当家：直接服务、<br>自查提醒、自带 _save 存档]
    Managed -. "release.py 移出收编区" .-> Lite
    Lite -. "adopt.py 收编入库" .-> Managed
```

判定是**纯目录事实**，无配置文件参与——搬动即切换。

## 存档三定律（程序与存档分离）

```mermaid
flowchart TD
    subgraph Program["程序区（可整体替换，git 管理）"]
        Code[代码/章程/人设/manifest/模板/皮肤/种子库]
    end
    subgraph Save["_save 存档区 — 删除即回到原点"]
        Master["master/ 主人档案·名册·授权审计"]
        AData["agents 数据 — 表·配置·回收站"]
        AMail["agents 信箱 inbox+outbox"]
        ADash["agents 看板生成物 data.js"]
    end
    Update([主程序更新]) -->|替换程序区| Program
    Update -.->|save_version 阶梯迁移| Save
    Reset([删档回原点]) -->|删除 _save| Save
    Reset -.->|程序区原样| Program
    Owner2([日常使用]) -->|一切增量信息| Save
```

- `save.json` 记录 `save_version`；`migrate_save.py` 阶梯升档（v1→v2→…）
- `save.py watch` 确定性扫描程序区违规增量（工具层硬约束，不靠 LLM 自觉）；doctor 体检含同款
- 速率型目标（kg/周类）不自动判达成——达成要主人亲口说

## 担保授权（顾问间数据互通的唯一通道）

顾问 A 想要 B 的数据 → 总管拿去问主人 → 主人点头 → `authz.py` 从 B 导出**只读快照**（来源/用途/24h 有效期）投进 A 的信箱 → 过期即清扫（`authz.py --sweep`）→ 全程审计进存档。顾问间私下互读 = 重大违规。

## 工具箱（05-scripts，仅 Python 标准库）

| 工具 | 干什么 |
|------|--------|
| `agentcrew_lib.py` | 公共库：路径判定/存档解析器/manifest 校验/原子写 |
| `adopt.py` / `release.py` | 收编（八项校验+存档并入）/ 放归（存档迁出回 lite） |
| `authz.py` | 担保授权快照 + 过期清扫 |
| `doctor.py` | 体检：结构/名册/顾问完整性/防腐/存档审计 |
| `dnd_check.py` | 可推送时段核验（注册 cron 前/触发前必查） |
| `save.py` / `migrate_save.py` | 存档 status/watch/reset / 阶梯迁移 |
| `make-instance.py` | 从框架生成个人实例（开发痕迹零携带） |

## 设计决策记录

- **为什么纯文件协议而不是 API 调用**：文件谁都能读——AI 会话、脚本、GUI、人。换宿主零改动。
- **为什么位置即管辖**：零配置，物理事实不可伪造；搬动即切换有测试背书。
- **为什么存档不进 git**：主人数据离开主人才是事故；程序区随时可公开。
- **设计系统**：界面采用 OpenDesign 官方库 `warm-editorial`（暖纸/赤陶土/森林绿），规格见 `02-agents/agentcrew.fitness/dashboard/DESIGN.md`。
