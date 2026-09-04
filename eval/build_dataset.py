# -*- coding: utf-8 -*-
"""固化数据集 eval/dataset_v1.json 并输出报告所需统计。

合并 trace_stats.jsonl + failure_classification.jsonl + task_groups.json。
include=false 规则：user_simulator_fault=true 的轨迹剔除，其余保留。
"""
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = os.environ.get("TAG", "v1")
_sfx = f"_{TAG}" if TAG else ""
STATS_PATH = ROOT / "eval" / f"trace_stats{_sfx}.jsonl"
CLS_PATH = ROOT / "eval" / f"failure_classification{_sfx}.jsonl"
GROUPS_PATH = ROOT / "eval" / f"task_groups{_sfx}.jsonl"
OUT_PATH = ROOT / "eval" / f"dataset_{TAG or 'v1'}.json"
SUMMARY_PATH = ROOT / "eval" / f"dataset_{TAG or 'v1'}_summary.json"


def main():
    rows = [json.loads(l) for l in STATS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    groups = json.loads(GROUPS_PATH.read_text(encoding="utf-8"))["groups"]
    task_group = {}
    for g, tids in groups.items():
        for t in tids:
            task_group[t] = g

    cls_map = {}
    if CLS_PATH.exists():
        for l in CLS_PATH.read_text(encoding="utf-8").splitlines():
            if l.strip():
                d = json.loads(l)
                cls_map[(d["task_id"], d["trial"])] = d

    dataset = []
    for r in rows:
        key = (r["task_id"], r["trial"])
        group = task_group.get(r["task_id"], "分化")
        if r["reward"] == 1.0:
            failure_type, usf, note = None, False, "成功轨迹"
        else:
            c = cls_map.get(key)
            if c is None:
                failure_type, usf, note = "未分类", False, "缺少 LLM 分类结果，需复核"
            else:
                failure_type = c["failure_type"]
                usf = c["user_simulator_fault"]
                note = c.get("evidence", "")
                if c.get("classify_error"):
                    note = "LLM 分类失败，需人工复核"
        include = not usf
        dataset.append({
            "trace": r["trace"],
            "task_id": r["task_id"],
            "trial": r["trial"],
            "reward": r["reward"],
            "group": group,
            "failure_type": failure_type,
            "user_simulator_fault": usf,
            "include": include,
            "note": note,
        })

    OUT_PATH.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")

    # 统计
    fail_rows = [d for d in dataset if d["reward"] == 0.0]
    ft_counter = Counter(d["failure_type"] for d in fail_rows)
    usf_count = sum(1 for d in fail_rows if d["user_simulator_fault"])
    include_count = sum(1 for d in dataset if d["include"])

    # 全挂任务甄别
    hopeless = groups["稳定坏"]
    hopeless_detail = {}
    for tid in hopeless:
        fs = [d for d in dataset if d["task_id"] == tid and d["reward"] == 0.0]
        n_usf = sum(1 for d in fs if d["user_simulator_fault"])
        hopeless_detail[tid] = {"n_fail": len(fs), "n_user_sim_fault": n_usf,
                                "types": [d["failure_type"] for d in fs]}
    n_hopeless_noisy = sum(1 for t, v in hopeless_detail.items() if v["n_user_sim_fault"] >= 2)
    n_hopeless_agent = len(hopeless) - n_hopeless_noisy

    summary = {
        "total": len(dataset),
        "pass": sum(1 for d in dataset if d["reward"] == 1.0),
        "fail": len(fail_rows),
        "pass_rate": round(sum(1 for d in dataset if d["reward"] == 1.0) / len(dataset), 4),
        "groups": {k: len(v) for k, v in groups.items()},
        "failure_type_dist": dict(ft_counter.most_common()),
        "user_simulator_fault_failures": usf_count,
        "include": include_count,
        "exclude": len(dataset) - include_count,
        "hopeless_tasks": {"count": len(hopeless),
                            "user_sim_noise_or_task_issue": n_hopeless_noisy,
                            "agent_capability_issue": n_hopeless_agent,
                            "detail": hopeless_detail},
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "hopeless_tasks"}, ensure_ascii=False, indent=2))
    print(json.dumps({"hopeless": {k: v for k, v in summary["hopeless_tasks"].items() if k != "detail"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
