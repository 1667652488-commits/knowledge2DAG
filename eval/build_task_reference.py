# -*- coding: utf-8 -*-
"""从 τ-bench 任务集机械渲染裁判参考真值（零人工）。

输出 eval/task_reference.jsonl，每行：
{
  "script_id": "tau_<task_id>",
  "task_id": <int>,
  "instruction": <任务指令原文>,
  "expected_actions_text": <expected actions 渲染文本，供裁判比对 EB 的 should 段>,
  "expected_action_names": [<工具名>, ...]
}

用法（在 agent/.venv 环境下，tau_bench 已安装）：
    agent/.venv/Scripts/python eval/build_task_reference.py
可选：--tasks <tasks_test.py 路径>  --out eval/task_reference.jsonl
若 import tau_bench 失败则退化为正则解析（与 adapter 同策略）。
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS = ROOT / "agent" / "tau_bench" / "tau_bench" / "envs" / "retail" / "tasks_test.py"
DEFAULT_OUT = ROOT / "eval" / "task_reference.jsonl"


def render_action(action: dict) -> str:
    """单个 expected action 渲染为一行文本。"""
    name = action.get("name", "?")
    kwargs = action.get("kwargs", {}) or {}
    if kwargs:
        args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in kwargs.items())
        return f"{name}({args})"
    return f"{name}()"


def render_task(task_id: int, instruction: str, actions: list) -> dict:
    lines = [f"{i + 1}. {render_action(a)}" for i, a in enumerate(actions)]
    actions_text = "期望的工具调用序列（顺序即约束）：\n" + "\n".join(lines)
    return {
        "script_id": f"tau_{task_id}",
        "task_id": task_id,
        "instruction": instruction,
        "expected_actions_text": actions_text,
        "expected_action_names": [a.get("name", "?") for a in actions],
    }


def load_via_import(tasks_path: Path) -> list:
    """优先真 import（τ-bench 已装在 venv 时）。"""
    spec = importlib.util.spec_from_file_location("tasks_mod", tasks_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # noqa: S102 — 本地受控文件
    tasks = mod.TASKS_TEST
    out = []
    for i, t in enumerate(tasks):
        d = t.model_dump() if hasattr(t, "model_dump") else vars(t)
        out.append(render_task(i, d.get("instruction", ""), d.get("actions", [])))
    return out


def load_via_regex(tasks_path: Path) -> list:
    """退化路径：正则解析 Task(...) 块（tau_bench 未安装时）。

    只提取 instruction 和 actions 的 name/kwargs（ast.literal_eval 解析 kwargs dict）。
    """
    import ast

    src = tasks_path.read_text(encoding="utf-8")
    blocks = re.findall(r"Task\((.*?)\n\s*\)", src, re.DOTALL)
    out = []
    for i, blk in enumerate(blocks):
        m_inst = re.search(r'instruction\s*=\s*("""(.*?)"""|"(.*?)"|\'(.*?)\')', blk, re.DOTALL)
        instruction = next((g for g in (m_inst.groups()[1:] if m_inst else []) if g), "") or ""
        actions = []
        for am in re.finditer(r"Action\(\s*name\s*=\s*\"([^\"]+)\"\s*,\s*kwargs\s*=\s*(\{.*?\})\s*\)", blk, re.DOTALL):
            try:
                kwargs = ast.literal_eval(am.group(2))
            except Exception:
                kwargs = {}
            actions.append({"name": am.group(1), "kwargs": kwargs})
        out.append(render_task(i, instruction, actions))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(DEFAULT_TASKS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    tasks_path = Path(args.tasks)
    try:
        rows = load_via_import(tasks_path)
        print("经 import 加载任务集")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] import 失败（{e}），退化为正则解析")
        rows = load_via_regex(tasks_path)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"共 {len(rows)} 条任务参考真值 -> {out_path}")


if __name__ == "__main__":
    sys.exit(main())
