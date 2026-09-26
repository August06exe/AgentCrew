# 03-template — 子助理脚手架

造新助理的最短路径（详见 `00-docs/PARADIGM.md` §12）：

1. 整个目录复制为 `02-agents/<你的id>`（或放任意处先 lite 开发）；
2. 改 `manifest.json`（id/name/domain/routing_examples/tables/reminders）；
3. 改 `AGENTS.md` 章程四节与 `persona.md` 口吻；
4. 按领域加 `tools/<domain>.py`（`tools/data.py` 通用工具直接可用，勿改）；
5. `python tools/data.py init && python tools/data.py selfcheck`；
6. 仓库根跑 `python 05-scripts/adopt.py --path 02-agents/<你的id>` 收编。

文件清单：
- `manifest.json` — 营业执照（字段说明见范式 §2）
- `AGENTS.md` — 章程骨架（含双模式自查条款，范式 §3）
- `persona.md` — 人设骨架
- `tools/data.py` — 通用数据工具（append/query/stats/delete/update/serve/selfcheck，范式 §6）
- `dashboard/index.html` + `butler.css` — 看板骨架与统一皮肤 lite 回退拷贝（范式 §7）
- `tests/smoke.py` — 最小冒烟（init/append/query/selfcheck）
