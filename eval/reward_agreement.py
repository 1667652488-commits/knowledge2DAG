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
    conf = Counter()          # result 三档 × 硬判分
    conf_oc = Counter()       # outcome_result 两档 × 硬判分
    n_outcome = 0             # 有 outcome_result 的条数
    n_with_issues = 0         # 有流程问题的条数
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
        oc = row.get("outcome_result")
        if oc in ("通过", "失败"):
            n_outcome += 1
            conf_oc[(oc, hard)] += 1
        issues = row.get("process_issues") or []
        if issues:
            n_with_issues += 1
        rows.append({
            "script_id": sid, "trial": trial, "trace": trace_file,
            "golden_result": g, "outcome_result": oc or "",
            "process_issues": "；".join(issues), "hard_result": hard,
            "agree": 1 if g == hard else 0,
            "golden_reason": row.get("reason", ""),
        })

    both = len(rows)
    agree = sum(r["agree"] for r in rows)
    rate = agree / both if both else 0.0
    # outcome 轴一致率（golden v3 双维度产出时才有）
    oc_both = sum(conf_oc.values())
    oc_agree = conf_oc.get(("通过", "通过"), 0) + conf_oc.get(("失败", "失败"), 0)
    oc_rate = oc_agree / oc_both if oc_both else None
    dist = Counter(v["result"] for v in golden.values())

    # ---- 新样式混淆矩阵：行=硬判分（真实情况），列=golden 判断，带合计与对错标注 ----
    def build_table():
        tp = conf.get(("通过", "通过"), 0)      # 真通过，golden 判通过
        fp = conf.get(("通过", "失败"), 0)      # 真失败，golden 判通过（漏判）
        t  = {}
        for g in ("通过", "部分通过", "失败"):
            for h in ("通过", "失败"):
                t[(h, g)] = conf.get((g, h), 0)
        total_hp = sum(t[("通过", g)] for g in ("通过", "部分通过", "失败"))
        total_hf = sum(t[("失败", g)] for g in ("通过", "部分通过", "失败"))
        col_p = t[("通过", "通过")] + t[("失败", "通过")]
        col_pp = t[("通过", "部分通过")] + t[("失败", "部分通过")]
        col_f = t[("通过", "失败")] + t[("失败", "失败")]
        rows_out = [
            "| 真实情况 ↓ | golden 判通过 | golden 判部分通过 | golden 判失败 | 合计 |",
            "|---|---|---|---|---|",
            f"| **硬判分：通过** | {t[('通过','通过')]} [判对] | {t[('通过','部分通过')]} | {t[('通过','失败')]} [误杀] | {total_hp} |",
            f"| **硬判分：失败** | {t[('失败','通过')]} [漏判] | {t[('失败','部分通过')]} | {t[('失败','失败')]} [判对] | {total_hf} |",
            f"| **合计** | {col_p} | {col_pp} | {col_f} | {total_hp + total_hf} |",
        ]
        return rows_out

    table = build_table()

    # outcome 轴两档表（v3）
    oc_table = []
    if oc_both:
        oc_tp = conf_oc.get(("通过", "通过"), 0)
        oc_fp = conf_oc.get(("失败", "通过"), 0)
        oc_fn = conf_oc.get(("通过", "失败"), 0)
        oc_tn = conf_oc.get(("失败", "失败"), 0)
        oc_table = [
            "| 真实情况 ↓ | golden outcome=通过 | golden outcome=失败 | 合计 |",
            "|---|---|---|---|",
            f"| **硬判分：通过** | {oc_tp} [判对] | {oc_fn} [误杀] | {oc_tp + oc_fn} |",
            f"| **硬判分：失败** | {oc_fp} [漏判] | {oc_tn} [判对] | {oc_fp + oc_tn} |",
        ]

    # ---- 终端汇总 ----
    print(f"golden: {golden_path}")
    print(f"批次:   {run_dir}")
    print(f"golden 条数: {len(golden)} | result 分布: {dict(dist)}")
    print(f"可对拍: {both} | 一致: {agree} | 一致率(result 三档口径): {rate:.1%}")
    if oc_both:
        print(f"outcome 轴可对拍: {oc_both} | 一致: {oc_agree} | **一致率(outcome 口径): {oc_rate:.1%}**")
        print(f"有流程问题(process_issues 非空): {n_with_issues} 条")
    print()
    if oc_table:
        print("【outcome 轴（对齐硬判分的结果对错）】")
        print("\n".join(oc_table))
        print()
        print("【result 三档轴（golden 原始三档，含流程考量）】")
    print("\n".join(table))
    print(f"\n误杀 {conf.get(('失败', '通过'), 0)} 条（实际成功但 golden 判失败）| "
          f"漏判 {conf.get(('通过', '失败'), 0)} 条（实际失败但 golden 判通过）")

    # ---- report.md ----
    lines = [
        "# golden vs 硬判分 对拍报告",
        "",
        f"- golden 产出：`{golden_path}`",
        f"- 批次：`{run_dir}`",
        f"- golden 条数：{len(golden)}，result 分布：{dict(dist)}",
    ]
    if oc_both:
        lines += [
            f"- **一致率（outcome 口径，主指标）：{oc_rate:.1%}**（{oc_agree}/{oc_both}）——golden v3 双维度产出的结果轴",
            f"- 一致率（result 三档口径，参考）：{rate:.1%}（{agree}/{both}）",
            f"- 有流程问题（process_issues 非空）：{n_with_issues} 条",
            "",
            "## outcome 轴混淆矩阵（结果对错，对齐硬判分）",
            "",
            *oc_table,
            "",
            "## result 三档轴混淆矩阵（含流程考量，参考）",
            "",
            *table,
        ]
    else:
        lines += [
            f"- 可对拍：{both}，一致：{agree}，**一致率：{rate:.1%}**",
            "",
            "## 混淆矩阵（行=硬判分真实情况，列=golden 判断）",
            "",
            *table,
        ]
    lines += [
        "",
        f"- **误杀 {conf.get(('失败', '通过'), 0)} 条**（实际成功但 golden 判失败，result 三档口径）",
        f"- **漏判 {conf.get(('通过', '失败'), 0)} 条**（实际失败但 golden 判通过，result 三档口径）",
        "",
        "## 口径",
        "- outcome 口径（v3 起，主指标）：golden 的 outcome_result 仅判定核心目标是否达成，与硬判分同域对拍；严格两档。",
        "- result 三档口径：golden 原始三档（含流程考量），严格相等算一致；『部分通过』恒记不一致（单列观察）。",
        "- 明细见同目录 detail.csv（含 outcome_result / process_issues 列）。",
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
