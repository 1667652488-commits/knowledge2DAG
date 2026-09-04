#!/usr/bin/env python3
"""
validate_rules.py — 用 53 条 trace 验证 Python 规则准确率(对比 golden)

口径:
  golden result: 通过/失败/部分通过/NA
  rules 输出:    通过/失败(取 evaluate_summary.result)
  - NA、部分通过 从主指标分母剔除(单列), 部分通过按"失败方向"软统计
  - 主指标 = (golden二值 通过/失败 中命中数) / (golden二值总数)
"""
import json, sys, os, glob, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.config import get_config

_cfg = get_config()
TRACE_DIR = str(_cfg.paths.traces)
GOLDEN = str(_cfg.paths.golden / "golden_output.jsonl")
RULES_DIR = str(_cfg.paths.root / "rules_python")

def _load_evaluator(rules_dir):
    sys.path.insert(0, rules_dir)
    from rules import evaluate_summary
    return evaluate_summary


def load_golden():
    g = {}
    for line in open(GOLDEN, encoding="utf-8"):
        d = json.loads(line)
        g[d["script_id"]] = d
    return g


def main():
    global TRACE_DIR, GOLDEN
    ap = argparse.ArgumentParser(description="验证 Python 规则准确率")
    ap.add_argument("--rules-dir", default=RULES_DIR, help="rules.py 所在目录")
    ap.add_argument("--trace-dir", default=TRACE_DIR)
    ap.add_argument("--golden", default=GOLDEN)
    args = ap.parse_args()

    TRACE_DIR = args.trace_dir
    GOLDEN = args.golden
    evaluate_summary = _load_evaluator(args.rules_dir)

    golden = load_golden()
    rows = []
    for tf in sorted(glob.glob(os.path.join(TRACE_DIR, "*.json"))):
        sid = os.path.basename(tf).replace("trace_", "").replace(".json", "")
        trace = json.load(open(tf, encoding="utf-8"))
        try:
            pred = evaluate_summary(trace)
            pres = pred["result"]
        except Exception as e:
            pres = "NA"
            pred = {"error": str(e)}
        g = golden.get(sid, {})
        gres = g.get("result", "?")
        rows.append({"sid": sid, "golden": gres, "pred": pres, "pred_obj": pred,
                     "expected": g.get("expected_behavior", "")[:80]})

    # 主指标: golden 二值
    binary = [r for r in rows if r["golden"] in ("通过", "失败")]
    hit = sum(1 for r in binary if r["pred"] == r["golden"])
    # 分项
    tp = sum(1 for r in binary if r["golden"] == "失败" and r["pred"] == "失败")
    fn = sum(1 for r in binary if r["golden"] == "失败" and r["pred"] == "通过")  # 漏报
    fp = sum(1 for r in binary if r["golden"] == "通过" and r["pred"] == "失败")  # 误报
    tn = sum(1 for r in binary if r["golden"] == "通过" and r["pred"] == "通过")
    partial = [r for r in rows if r["golden"] == "部分通过"]
    na = [r for r in rows if r["golden"] == "NA"]

    out = sys.stdout
    def p(*a): print(*a, file=out)
    p("=" * 70)
    p(f"trace 总数: {len(rows)}  golden二值: {len(binary)}  部分通过: {len(partial)}  NA: {len(na)}")
    p(f"主指标准确率: {hit}/{len(binary)} = {hit/len(binary):.1%}")
    p(f"  golden失败(共{tp+fn}): 命中TP={tp}  漏报FN={fn}  召回={tp/(tp+fn) if tp+fn else 0:.1%}")
    p(f"  golden通过(共{tn+fp}): 正确TN={tn}  误报FP={fp}  误报率={fp/(tn+fp) if tn+fp else 0:.1%}")
    p("-" * 70)
    p("漏报(golden失败 但 规则判通过):")
    for r in binary:
        if r["golden"] == "失败" and r["pred"] == "通过":
            p(f"  {r['sid']}: {r['expected']}")
    p("误报(golden通过 但 规则判失败):")
    for r in binary:
        if r["golden"] == "通过" and r["pred"] == "失败":
            p(f"  {r['sid']}: hit={r['pred_obj'].get('rules_hit')} skill={r['pred_obj'].get('top_skill')} | {r['expected']}")
    p("部分通过 轨迹:")
    for r in partial:
        p(f"  {r['sid']}: pred={r['pred']} | {r['expected']}")
    p("=" * 70)

    # 落盘明细
    detail_path = os.path.join(os.path.dirname(__file__), "..", "validation", "validation_report.json")
    os.makedirs(os.path.dirname(detail_path), exist_ok=True)
    json.dump({"summary": {"total": len(rows), "binary": len(binary), "accuracy": hit/len(binary),
                          "tp": tp, "fn": fn, "fp": fp, "tn": tn},
               "rows": [{k: v for k, v in r.items() if k != "pred_obj"} | {"pred_detail": r["pred_obj"]}
                        for r in rows]},
              open(detail_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    p(f"明细已写: {detail_path}")


if __name__ == "__main__":
    main()
