# SAVE-DESIGN — 存档架构设计（提案 v1，待主人拍板）

> 需求（主人 9/20 提出的"游戏存档"定律）：
> ① 部署后填写的**一切增量信息**，完整存储在**同一个文件夹**（=存档）；
> ② **主程序更新兼容旧存档**（换程序不换档）；
> ③ **删除存档 = 回到原点**（出厂状态）。
> 子助理也必须遵循此逻辑 → 开发范式要写清楚。

---

## 一、现状盘点：今天"存档"散落在哪（必须收敛的证明）

| 增量数据 | 现在的位置 | 性质 |
|---|---|---|
| 主人档案 profile.json | `01-master\` | 源数据 |
| 顾问名册 registry.json | `01-master\` | 源数据 |
| 担保授权审计 authorizations.jsonl | `01-master\` | 源数据 |
| 总管信箱 inbox-master\、放归流水 releases.jsonl | `01-master\` | 源数据 |
| 各顾问全部数据表/配置/回收站/周报 | `02-agents\<id>\data\` | 源数据 |
| 各顾问信箱（交办单/回执） | `02-agents\<id>\{inbox,outbox}\` | 运行状态 |
| 看板生成数据 data.js（含全量数据副本！） | `02-agents\<id>\dashboard\` | **派生**（可重建） |
| 新装顾问的程序本体 | `02-agents\<id>\` | 程序（类比"装 MOD"） |
| 宿主层定时任务注册（zcode 自动化 / Hermes cron） | 仓库之外 | **派生**（可重建） |

问题：源数据分散在 6 类位置；派生数据混进程序区；"删档回原点"今天做不到一刀切。

**关键分类原则**：只有**源数据**（不可重建的）进存档；**派生态**（看板 data.js、daily_records 派生表、宿主 cron 注册）不入档——它们能从源数据+程序重建，入档反而制造不一致。这与游戏存档只存进度、不存缓存是同一道理。

## 二、路径分析（广开思路，含否决理由）

### 路径 1：目录联接（Junction/符号链接）——"障眼法"
保留现有路径（`02-agents\<id>\data` 等），把它们做成指向存档区的 junction，程序以为在原地写，物理上落进 `_save\`。
- 优点：工具零改动。
- **否决**：Windows 符号链接要管理员权限、junction 不跨平台、git/zip/拷贝备份全会把链接搞断——"拷走文件夹=备份"这个卖点直接阵亡。脆弱的魔法不如显式的规则。

### 路径 2：存档根重定向（Save Root Resolver）——工具层硬约束 ✅ 推荐
所有工具里"我的数据在哪"不再各自硬编码，统一经**存档根解析器**（`agentcrew_lib.save_root()`）：
- 实例内 → `<仓库>\_save\`；lite 独立助理 → `<助理目录>\_save\`（自包含公理不破）；
- 环境变量 `AGENTCREW_SAVE` 可整体外置（见路径 4）。
- data.py / fitlib / 面板 / 总管脚本的全部读写改走解析器；程序区对工具而言**不存在写路径**（想写也没有函数能写进去）。
- 优点：硬保证、跨平台、可测试（现有沙箱机制就是它的前身——ASSISTANT_DATA_DIR 已验证此路可行）。
- 成本：一次性改所有写路径（约 8 个文件）+ 面板静态映射一处。

### 路径 3：纯提示词软约束——"君子协定"
只在章程/范式里写"增量信息必须写到 _save"，靠 AI 自觉。
- **只能当第二层，不能单独用**：AI 会漂移、会忘；且现有工具本身就往程序区写（data.py serve 写 dashboard），提示词管不住代码。软约束用来管"绕过工具徒手写文件"的冲动（本就有"一切经 data.py"红线，顺势加强）。

### 路径 4：外置存档（存档搬家）——路径 2 的部署策略
`AGENTCRAFT_SAVE=..\MySaves\crew1` 把存档指到项目外（像游戏存档进 Documents）：
- 场景：多实例共存、项目目录进 git/网盘同步而存档不想进去、换机迁移只拷存档。
- 实现成本几乎为零（解析器认 env 即可），**第一期就带上**，但默认仍是仓库内 `_save\`。

### 路径 5：单文件存档包（导出/导入）——叠加功能
`save.py export` 把 `_save\` 打成一个 `crew-save-2026-09-20.zip`（像 .sav 文件），`save.py import` 读回。
- 用于备份、跨机迁移、给朋友"发个档"。建在路径 2 之上，第二期做也行，接口第一期预留。

### 最终架构：路径 2 为主体 + 路径 3 为第二层 + 路径 4 首发内建 + 路径 5 预留
**双层保证 + 一层审计**：工具层（硬，想违规都难）→ 章程/范式层（软，管 AI 行为）→ doctor 审计层（抓漏网：程序区发现数据文件即告警）。

## 三、存档区结构（提案）

```
_save\                              ← 存档（gitignored；删我=回原点）
  save.json                         ← 档案卡：{save_version, created_at, agents:{id:{version,...}}}
  master\
    profile.json  registry.json
    authorizations.jsonl  releases.jsonl
    inbox-master\  trash\
  agents\
    agentcrew.fitness\
      data\          ← 全部表+config+trash+reports（源数据）
      inbox\ outbox\ ← 信箱（运行状态，随档走）
      dashboard\     ← serve 生成的 data.js（派生，但放档区避免污染程序区）
    demo.sample\…
```

游戏机制对照：

| 游戏 | AgentCrew |
|---|---|
| 新游戏 | `_save` 不存在 / `save.py reset`（二次确认后删除） |
| 读档 | 程序启动时解析器指向 `_save`，全部工具自动从档区读写 |
| 主程序更新 | 替换 `_save` 以外的一切 → doctor 体检 → 需要时 `migrate_save.py` 升档 |
| 存档备份/搬家 | 拷 `_save` 文件夹 / `AGENTCREW_SAVE` 外置 / （二期）zip 导出导入 |
| 装 MOD | adopt 顾问：程序进 `02-agents\`，其自带 lite 存档**并入**实例 `_save\agents\<id>\` |
| 卸 MOD 带走进度 | release：程序去 `_standalone\`，其存档切片**随行**迁回助理自己的 `_save\` |

## 四、子助理范式怎么写（PARADIGM 修订要点）

1. **§5 数据规范重写**：助理声明 `tables`（逻辑层）；**物理落盘位置 = 宿主存档区**（实例 `_save\agents\<id>\data\`；lite 模式 `<自身>\_save\`）——助理永远不该感知也不该依赖物理路径，一切经 `tools\data.py`（它内置解析器）。**"程序区只读"成为第五公理**：助理目录里除 `_save\` 外一切文件对运行时 AI 只读。
2. **收编/放归 = 存档迁移**：adopt 时若助理自带 `_save`（lite 期间攒的）→ 并入实例存档；release 反向带回。范例章程红线同步加一条："禁止向 `_save\` 之外的程序区写任何文件"。
3. **兼容承诺**：manifest 增加 `save_schema` 版本字段（缺省 1）；程序升级若改表结构，必须同步提供迁移并在 adopt/doctor 时执行——**旧档永远能被新程序读**（读不了就是程序的 bug，不是用户的损失）。
4. **总管章程**：初始化建档写 `_save\master\profile.json`；"删档回原点"写进面板说明与 `开始这里.md`。

## 五、兼容与迁移机制（更新不丢档的保障）

- `save.json` 记 `save_version`；程序侧记 `SAVE_VERSION_CURRENT`。
- 启动/doctor 时比对：相等→通过；存档旧→提示并执行 `05-scripts\migrate_save.py`（游戏式逐版本阶梯迁移 v1→v2→…）；存档比程序新（降级安装）→明确警告。
- 每次 commit 若涉及存档结构变更，CHANGELOG 必须记迁移步骤——写进贡献规范。

## 六、诚实边界（哪些"增量"不进档，为什么）

- **宿主层定时任务注册**（zcode workspace 自动化、Hermes cron）：本来就在仓库外；且是**派生态**——manifest 里的人话提醒规则是源，注册只是投影，删档重装后总管按 manifest 重新注册即可（章程已如此规定）。
- **面板生成的 data.js**：派生态，serve 可随时重建（但会写入档区对应位置，避免碰程序区）。
- **`daily_records` 派生表**：随源表幂等重建（现状已如此）。

## 七、实施切片（拍板后执行，预计一轮完成）

1. `agentcrew_lib.py`：save_root() 解析器（仓库内默认 / AGENTCREW_SAVE 外置 / lite 回退）+ 全部写路径改造；
2. `data.py`×3、`fitlib.py`、面板 server（含 dashboard 静态映射）、总管脚本档案路径 → 走解析器；
3. `save.py`：status / reset（新游戏）/（预留 export-import）；`migrate_save.py` 阶梯迁移框架 + v1 落位迁移；
4. PARADIGM/两份章程/PROTOCOL/开始这里.md 改写 + doctor 三项新审计（程序区散落数据、save 版本、孤儿档切片）；
5. make-instance：新实例生成空 `_save`（save.json v1）+ 排除框架仓档区；
6. **3004.1 原地迁档**：现散落数据无损并入 `_save\`（14 周数据全程校验行数）；
7. 回归 5 套件 + 存档专项测试（新游戏/删档回原点/更新兼容演练）+ 一轮审核。

## 八、需要主人拍板的三个点

1. **目录名**：`_save`（推荐：下划线前缀=实例态家族，与程序区的数字目录直观区分）｜`00-save`（排最前但与 00-docs 撞号）｜`SAVE`｜自定义。
2. **外置存档**（AGENTCREW_SAVE）首发就带（推荐，几乎零成本）还是二期？
3. **3004.1 迁档时机**：架构落地后立即迁（推荐，一次到位）。
