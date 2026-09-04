# -*- coding: utf-8 -*-
"""对拍①：golden 标注 result vs τ-bench 硬判分 reward 的一致率。

用法：
    python eval/reward_agreement.py <golden_output.jsonl> <批次目录>
例：
    python eval/reward_agreement.py sut/runs/golden/20260903_151633/golden_output.jsonl runs/20260903_105240_strong_skilldriven_baseline_full
"""
import json
import sys
from collections import Counter
from pathlib import Path


def main():
    golden_path = Path(sys.argv[1])
    run_dir = Path(sys.argv[2])

    golden = {}
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            golden[d["id"]] = d

    # conversation_id -> tau_<tid>_t<trial>（经 trace 桥接）
    cid2key = {}
    for tf in (run_dir / "traces").glob("trace_*.json"):
        t = json.loads(tf.read_text(encoding="utf-8"))
        trial = tf.stem.split("_t")[-1]
        cid2key[t["conversation_id"]] = f"{t['script_id']}_t{trial}"

    rewards = json.loads((run_dir / "tau_reward.json").read_text(encoding="utf-8"))

    both, agree = 0, 0
    conf = Counter()
    for cid, row in golden.items():
        k = cid2key.get(cid)
        if not k or k not in rewards:
            continue
        hard = "通过" if rewards[k]["reward"] == 1.0 else "失败"
        conf[(row["result"], hard)] += 1
        both += 1
        if row["result"] == hard:
            agree += 1

    print(f"golden 条数: {len(golden)}")
    print(f"golden result 分布: {dict(Counter(v['result'] for v in golden.values()))}")
    print(f"可对拍: {both} 条，一致: {agree}，一致率: {agree / both:.1%}" if both else "无可对拍条目")
    print("混淆矩阵 (golden result, 硬判分) -> 条数:")
    for k, v in sorted(conf.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
