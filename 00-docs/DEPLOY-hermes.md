# DEPLOY-hermes — 部署到 Hermes 的映射笔记

> 面向转正期：把本框架搬到 Hermes（常驻 + 官方微信通道 + 定时推送）。
> 事实来源：Hermes 官方文档（`_refs\hermes-agent\` 仓库外副本 + 精读笔记见 `RESEARCH.md` §1.1）。

## 一、为什么转正期选 Hermes

| 需求 | Hermes 对应能力（官方文档核实） |
|------|------------------------------|
| 7×24 常驻接消息 | gateway 常驻进程 + 持久记忆 |
| 微信收发 | 官方 weixin 通道（iLink Bot API，长轮询，家用电脑免公网） |
| 定时提醒推送到微信 | cron + `WEIXIN_HOME_CHANNEL`（已知 ret=-2 怪癖官方已兜底重发） |
| 多通道未来扩展 | 官方 36+ 通道（Telegram/飞书/QQ/钉钉…），同一套助理不用改 |

已知限制（写进预期管理）：iLink 机器人身份=单聊可靠、群聊基本不可用；登录会话过期需重扫码；一个 token 只允许一个网关实例。

## 二、映射表：AgentCrew 概念 → Hermes 落点

| AgentCrew | Hermes 落点 | 说明 |
|-----------|------------|------|
| 总管章程 AGENTS.md | Hermes 的 profile 指令/系统提示区 | 让 gateway 的 agent 以总管身份行事（读 01-master 档案后上岗） |
| 子助理章程/人设 | Hermes skill（每个助理一个 skill 目录） | 助理的 manifest+AGENTS.md+persona 可整体作为 skill 资源；SKILL.md 引导加载它们 |
| 交办单/回执（inbox/outbox 文件） | 原样保留 | 协议是文件，Hermes 会话按 PROTOCOL.md 读写即可，零改动 |
| 提醒（manifest 人类规则） | Hermes cron（prompt 里引用 manifest 规则） | 晨间/晚间/周报三条 cron，deliver 到微信；固定文案直推不唤醒大脑 |
| 担保授权 | 总管流程内执行（authz.py） | 不变 |
| 管家面板 | 原样保留（本地 127.0.0.1:7530） | 与 Hermes 互不依赖 |
| 收编区 02-agents\ | 原样放 workdir 下 | 位置即管辖判定不受宿主影响 |

## 三、迁移步骤（转正期执行清单）

1. `python 05-scripts/make-instance.py --to <实例目录> --name <名字>` fork 个人实例；
2. 实例内完成初始化（IM 问答或面板表单）；
3. Hermes 里把实例目录设为工作区/挂载总管指令；
4. 按 manifest.reminders 建 3 条 cron（晨间 08:30 / 晚间 21:00 / 周日 20:00），deliver=微信；
5. 实测一条 1 分钟后的定时消息确认推送链路（转正期验收 ③）；
6. zcode 网关保留为备用入口（本仓库 AGENTS.md 仍生效）。

## 三.5、存档巡检 cron（转正期照抄）

| 项 | 值 |
|----|----|
| cron | `0 */6 * * *` |
| script | `python 05-scripts/save.py watch` |
| deliver | 微信（违规时才有内容） |
| Prompt | 你是 AgentCrew 总管的巡检岗。上一条 script 的 watch 若干净（exit 0）**不要输出任何内容**直接结束；若违规，先跑 `python 05-scripts/dnd_check.py --at <当前时刻>`，允许则向主人报告：「存档巡检发现 N 处增量数据散落程序区（列前 3 处），要我一键归档吗？」；勿扰窗口内则只记录待窗口开启再报。 |

## 四、备胎关系（诚实声明）

- 自建常驻守护进程方案在 v1 被裁撤（无常驻组件）；若 Hermes 不可用，zcode 网关的"每消息新会话"模式仍可跑全部协议（换班管家），只是没有 7×24 提醒。
- 微信桥若哪天失效，Telegram 通道是零依赖备胎（本框架侧无需任何改动，只是宿主换通道）。
