#!/usr/bin/env python3
"""run_all.py — 一键回归：体检 + 模板冒烟 + 样板冒烟 + 健身全流程 + 理财冒烟/全流程 + 联邦模拟器。"""
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

SUITES = [
    ("doctor(全检含自检)", [PY, os.path.join(ROOT, "05-scripts", "doctor.py")]),
    ("template冒烟", [PY, os.path.join(ROOT, "03-template", "tests", "smoke.py")]),
    ("sample冒烟", [PY, os.path.join(ROOT, "02-agents", "demo.sample", "tests", "smoke.py")]),
    ("fitness全流程", [PY, os.path.join(ROOT, "02-agents", "agentcrew.fitness", "tests", "test_flow.py")]),
    ("finance冒烟", [PY, os.path.join(ROOT, "02-agents", "agentcrew.finance", "tests", "smoke.py")]),
    ("finance全流程", [PY, os.path.join(ROOT, "02-agents", "agentcrew.finance", "tests", "test_flow.py")]),
    ("联邦模拟器", [PY, os.path.join(HERE, "simulator.py")]),
    ("存档导出导入", [PY, os.path.join(HERE, "test_savepack.py")]),
]


def main() -> int:
    failed = []
    for name, cmd in SUITES:
        print(f"\n=== {name} ===")
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600, cwd=ROOT)
        tail = (r.stdout or "").strip().splitlines()[-3:]
        print("\n".join(tail) if tail else "(无输出)")
        if r.returncode != 0:
            failed.append(name)
            print(f"  [stderr] {(r.stderr or '')[:400]}")
    print("\n" + "=" * 40)
    if failed:
        print("失败套件：" + "、".join(failed))
        return 1
    print(f"全部 {len(SUITES)} 个套件通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
