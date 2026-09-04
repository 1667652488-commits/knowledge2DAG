# -*- coding: utf-8 -*-
"""baseline 全量批次机械统计分析（纯脚本，不调 LLM）。

输入:
- runs/20260820_161933_weak_baseline_turbo_full/traces/trace_<task_id>_t<trial>.json
- runs/20260820_161933_weak_baseline_turbo_full/tau_reward.json
- eval/task_reference.jsonl

输出:
- eval/trace_stats.jsonl : 每条轨迹一行的机械统计
- eval/task_groups.json  : 按 task 分组（稳定好/稳定坏/分化）
"""
import json
import os
import re
import sys
from pathlib import Path
from difflib import SequenceMatcher

ROOT = Path(__file__).resolve().parent.parent
# 可用环境变量覆盖批次与输出后缀（默认 v1 基线）
RUN_DIR = ROOT / "runs" / os.environ.get("BATCH_DIR", "20260820_161933_weak_baseline_turbo_full")
TAG = os.environ.get("TAG", "")
_sfx = f"_{TAG}" if TAG else ""
TRACE_DIR = RUN_DIR / "traces"
REWARD_PATH = RUN_DIR / "tau_reward.json"
REF_PATH = ROOT / "eval" / "task_reference.jsonl"
OUT_STATS = ROOT / "eval" / f"trace_stats{_sfx}.jsonl"
OUT_GROUPS = ROOT / "eval" / f"task_groups{_sfx}.jsonl"


def load_references():
    refs = {}
    with open(REF_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            refs[d["task_id"]] = d
    return refs


def extract_trace(trace_path):
    """返回 (tool_name_seq, tool_calls_detail, num_messages, last_assistant_text, tool_errors)"""
    data = json.loads(trace_path.read_text(encoding="utf-8"))
    messages = data["messages"] if isinstance(data, dict) else data
    name_seq = []
    calls_detail = []
    errors = []
    last_assistant = ""
    pending_tools = []  # 待配对的 tool 返回消息队列
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            tcs = msg.get("tool_calls") or []
            if msg.get("content"):
                last_assistant = msg["content"]
            for tc in tcs:
                name_seq.append(tc.get("name"))
                calls_detail.append({"name": tc.get("name"), "arguments": tc.get("arguments")})
                pending_tools.append(tc.get("name"))
        elif role == "tool":
            name = pending_tools.pop(0) if pending_tools else None
            content = msg.get("content")
            content_str = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            low = content_str.lower()
            if "error" in low or "not found" in low or "invalid" in low or content_str.strip().startswith("Error"):
                errors.append({"tool": name, "output": content_str[:300]})
            if calls_detail:
                # 把返回内容挂到最近一条同名调用上
                for cd in reversed(calls_detail):
                    if cd["name"] == name and "output" not in cd:
                        cd["output"] = content_str[:300]
                        break
    return name_seq, calls_detail, len(messages), last_assistant, errors


def compare_actions(actual, expected):
    """基于 SequenceMatcher 的对齐，返回首个分叉点、缺失动作、多余动作。"""
    sm = SequenceMatcher(a=expected, b=actual, autojunk=False)
    first_divergence = None
    missing = []
    extra = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if first_divergence is None:
            first_divergence = {
                "position": j1,
                "expected_at_pos": expected[i1] if i1 < len(expected) else None,
                "actual_at_pos": actual[j1] if j1 < len(actual) else None,
            }
        if tag in ("delete", "replace"):
            missing.extend(expected[i1:i2])
        if tag in ("insert", "replace"):
            extra.extend(actual[j1:j2])
    return first_divergence, missing, extra


def main():
    refs = load_references()
    rewards = json.loads(REWARD_PATH.read_text(encoding="utf-8"))
    rows = []
    for trace_path in sorted(TRACE_DIR.glob("trace_*.json")):
        m = re.match(r"trace_(\d+)_t(\d+)\.json", trace_path.name)
        task_id, trial = int(m.group(1)), int(m.group(2))
        key = f"tau_{task_id}_t{trial}"
        reward = rewards.get(key, {}).get("reward")
        name_seq, calls_detail, n_msg, last_assistant, errors = extract_trace(trace_path)
        expected = refs.get(task_id, {}).get("expected_action_names") or []
        first_div, missing, extra = compare_actions(name_seq, expected)
        rows.append({
            "trace": str(trace_path.relative_to(ROOT)).replace("\\", "/"),
            "task_id": task_id,
            "trial": trial,
            "reward": reward,
            "num_messages": n_msg,
            "tool_call_sequence": name_seq,
            "tool_calls_detail": calls_detail,
            "tool_errors": errors,
            "last_assistant_message": last_assistant[:800],
            "expected_action_names": expected,
            "first_divergence": first_div,
            "missing_actions": missing,
            "extra_actions": extra,
        })
    with open(OUT_STATS, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 按 task 分组
    groups = {"稳定好": [], "稳定坏": [], "分化": []}
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)
    for task_id, rs in sorted(by_task.items()):
        n_pass = sum(1 for r in rs if r["reward"] == 1.0)
        if n_pass == len(rs):
            groups["稳定好"].append(task_id)
        elif n_pass == 0:
            groups["稳定坏"].append(task_id)
        else:
            groups["分化"].append(task_id)
    summary = {
        "total_traces": len(rows),
        "total_tasks": len(by_task),
        "pass_count": sum(1 for r in rows if r["reward"] == 1.0),
        "fail_count": sum(1 for r in rows if r["reward"] == 0.0),
        "pass_rate": round(sum(1 for r in rows if r["reward"] == 1.0) / len(rows), 4),
        "groups": {k: sorted(v) for k, v in groups.items()},
        "group_sizes": {k: len(v) for k, v in groups.items()},
    }
    OUT_GROUPS.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("total_traces", "total_tasks", "pass_count", "fail_count", "pass_rate", "group_sizes")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
