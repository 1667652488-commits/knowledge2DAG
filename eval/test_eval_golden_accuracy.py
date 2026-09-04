"""eval_golden_accuracy.py 内联自测（不需要真实 LLM / embedding 服务）。

构造 3 条 fake candidate golden（tau_0/tau_1/tau_2，其中 tau_2 的 result=失败、
并故意给一条不在 task_reference 里的 tau_999 验证跳过逻辑），
用真实 eval/task_reference.jsonl 做标准侧，monkeypatch：
- _judge_one    -> 返回固定判定（tau_0 等价、tau_1 部分、tau_2 不等）
- embed_texts   -> 返回固定单位向量
- _load_judge_config -> 假配置（不依赖 eval/eval_judge_config.json 是否存在）

验证：配对数、汇总指标、eval_report_<ts>.md / eval_detail_<ts>.csv 落盘与内容。

运行（项目根目录，任意 python3，无需 httpx）：
    python eval/test_eval_golden_accuracy.py
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_golden_accuracy as eva


def _fake_judge(instruction, expected_actions_text, policy_text, candidate_eb, cfg):
    # 通过 candidate_eb 里的标记区分三档
    tag = candidate_eb.split("|")[-1].strip()
    score = {"EQ": 2, "PART": 1, "NEQ": 0}[tag]
    elem = {
        "EQ": (2, 2, 2),
        "PART": (2, 1, 2),
        "NEQ": (2, 0, 2),
    }[tag]
    out = {"overall": score, "tolerant": score == 2, "reason": f"fake judge: {tag}"}
    for e, s in zip(("scenario", "should", "should_not"), elem):
        out[e] = {
            "standard": f"std-{e}",
            "candidate": f"cand-{e}",
            "score": s,
            "reason": "",
        }
    return out


def _fake_embed(texts, key, base, model):
    # 每条返回不同向量：第 i 条为 one-hot 第 i%4 维，保证 cosine 有确定值
    vecs = []
    for i in range(len(texts)):
        v = [0.0] * 4
        v[i % 4] = 1.0
        vecs.append(v)
    return vecs


def _fake_config():
    return {
        "judge_api_key": "fake",
        "judge_base_url": "http://fake",
        "judge_model": "fake-judge",
        "embed_api_key": "fake",
        "embed_base_url": "http://fake",
        "embed_model": "fake-embed",
    }


def main() -> None:
    eva._judge_one = _fake_judge
    eva.embed_texts = _fake_embed
    eva._load_judge_config = _fake_config

    tmp = Path(tempfile.mkdtemp(prefix="eval_selftest_"))
    cand_path = tmp / "golden_output.jsonl"
    candidates = [
        {
            "script_id": "tau_0",
            "expected_behavior": "在交换订单商品时，应该先认证用户并核对订单与商品信息，不应该跳过身份核验。| EQ",
            "result": "通过",
            "reason": "golden ok",
            "inputs": ["You are Yusuf Rossi..."],
        },
        {
            "script_id": "tau_1",
            "expected_behavior": "在交换订单商品时，应该先认证用户。| PART",
            "result": "通过",
            "reason": "",
            "inputs": ["..."],
        },
        {
            "script_id": "tau_2",
            "expected_behavior": "在查询商品时，应该直接退货不做任何核对。| NEQ",
            "result": "失败",
            "reason": "trace 失败",
            "inputs": ["..."],
        },
        {  # 不在 task_reference 中，应被跳过
            "script_id": "tau_999",
            "expected_behavior": "孤儿条目 | EQ",
            "result": "通过",
            "reason": "",
            "inputs": [],
        },
    ]
    cand_path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in candidates) + "\n",
        encoding="utf-8",
    )

    policy = tmp / "wiki.md"
    policy.write_text("# fake policy\n不允许未认证操作。", encoding="utf-8")

    ref = Path(eva.DEFAULT_REFERENCE)
    assert ref.exists(), f"缺真实参考文件：{ref}"

    eva.main([str(cand_path), "--reference", str(ref), "--policy", str(policy)])

    # ---- 校验产物 ----
    reports = sorted(tmp.glob("eval_report_*.md"))
    csvs = sorted(tmp.glob("eval_detail_*.csv"))
    assert len(reports) == 1 and len(csvs) == 1, f"产物缺失: {list(tmp.iterdir())}"
    report = reports[0].read_text(encoding="utf-8")

    with open(csvs[0], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3, f"CSV 行数应为 3，实际 {len(rows)}"
    assert {r["script_id"] for r in rows} == {"tau_0", "tau_1", "tau_2"}
    by_sid = {r["script_id"]: r for r in rows}
    assert by_sid["tau_0"]["overall_score"] == "2" and by_sid["tau_0"]["tolerant"] == "1"
    assert by_sid["tau_1"]["overall_score"] == "1" and by_sid["tau_1"]["tolerant"] == "0"
    assert by_sid["tau_2"]["overall_score"] == "0"
    assert by_sid["tau_2"]["result"] == "失败"
    # instruction 进了明细（标准侧来自 task_reference）
    assert "Yusuf Rossi" in by_sid["tau_0"]["instruction"]

    # 报告关键段落
    for marker in (
        "# golden EB vs τ-bench 任务参考真值 一致性评测报告",
        "## 准确率",
        "## 三档分布",
        "## 三要素一致率",
        "## 分组准确率（严格）",
        "### 按 candidate result",
        "## 不一致 case 清单",
        "tau_2",
    ):
        assert marker in report, f"报告缺段落/内容: {marker}"
    # 汇总数值：严格 1/3、宽松 2/3
    assert "33.3%（1/3）" in report and "66.7%（2/3）" in report

    print()
    print(f"自测通过 ✔  产物目录: {tmp}")
    print(f"  报告: {reports[0].name}  明细: {csvs[0].name}（{len(rows)} 行）")


if __name__ == "__main__":
    main()
