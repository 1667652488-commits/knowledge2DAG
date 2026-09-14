# -*- coding: utf-8 -*-
"""对拍①：golden 标注 result vs τ-bench 硬判分 reward 的一致率。

用法：
    python eval/reward_agreement.py <golden_output.jsonl> <批次目录> [--out-dir <目录>]
例：
    python eval/reward_agreement.py runs/20260912_100559_golden/golden_output.jsonl runs/20260903_105240_strong_skilldriven_baseline_full

输出：
- 终端打印汇总 + 混淆矩阵表格；
- --out-dir（默认 runs/<ts>_agreement_<golden目录名>）下落：
    report.md    汇总 + 混淆矩阵
    detail.csv   逐条对比明细（script_id/trial/golden_result/硬判分/是否一致/golden reason）
"""
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    golden_path = Path(sys.argv[1])
    run_dir = Path(sys.argv[2])
    if len(sys.argv) > 4 and sys.argv[3] == "--out-dir":
        out_dir = Path(sys.argv[4])
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = ROOT / "runs" / f"{ts}_agreement_{golden_path.parent.name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    golden = {}
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            golden[d["id"]] = d

    # conversation_id -> (trace文件名, tau_<tid>_t<trial>)（经 trace 桥接）
    cid2info = {}
    for tf in (run_dir / "traces").glob("trace_*.json"):
        t = json.loads(tf.read_text(encoding="utf-8"))
        trial = tf.stem.split("_t")[-1]
        cid2info[t["conversation_id"]] = (tf.name, t["script_id"], trial)

    rewards = json.loads((run_dir / "tau_reward.json").read_text(encoding="utf-8"))

    rows = []
    conf = Counter()
    for cid, row in golden.items():
        info = cid2info.get(cid)
        if not info:
            continue
        trace_file, sid, trial = info
        key = f"{sid}_t{trial}"
        if key not in rewards:
            continue
        hard = "通过" if rewards[key]["reward"] == 1.0 else "失败"
        g = row["result"]
        conf[(g, hard)] += 1
        rows.append({
            "script_id": sid, "trial": trial, "trace": trace_file,
            "golden_result": g, "hard_result": hard,
            "agree": 1 if g == hard else 0,
            "golden_reason": row.get("reason", ""),
        })

    both = len(rows)
    agree = sum(r["agree"] for r in rows)
    rate = agree / both if both else 0.0
    dist = Counter(v["result"] for v in golden.values())

    # ---- 终端汇总 ----
    print(f"golden: {golden_path}")
    print(f"批次:   {run_dir}")
    print(f"golden 条数: {len(golden)} | result 分布: {dict(dist)}")
    print(f"可对拍: {both} | 一致: {agree} | 一致率: {rate:.1%}")
    print()
    print("| golden 判 \\ 硬判分 | 通过 | 失败 |")
    print("|---|---|---|")
    for g in ("通过", "部分通过", "失败"):
        print(f"| {g} | {conf.get((g, '通过'), 0)} | {conf.get((g, '失败'), 0)} |")

    # ---- report.md ----
    lines = [
        "# golden vs 硬判分 对拍报告",
        "",
        f"- golden 产出：`{golden_path}`",
        f"- 批次：`{run_dir}`",
        f"- golden 条数：{len(golden)}，result 分布：{dict(dist)}",
        f"- 可对拍：{both}，一致：{agree}，**一致率：{rate:.1%}**",
        "",
        "## 混淆矩阵",
        "",
        "| golden 判 \\ 硬判分 | 通过 | 失败 |",
        "|---|---|---|",
    ]
    for g in ("通过", "部分通过", "失败"):
        lines.append(f"| {g} | {conf.get((g, '通过'), 0)} | {conf.get((g, '失败'), 0)} |")
    lines += [
        "",
        "## 口径",
        "- 严格相等算一致；『部分通过』在两档硬判分下恒记为不一致（单列观察）。",
        "- 明细见同目录 detail.csv。",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- detail.csv ----
    with open(out_dir / "detail.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)

    print(f"\n产物: {out_dir}/report.md + detail.csv")


if __name__ == "__main__":
    main()
