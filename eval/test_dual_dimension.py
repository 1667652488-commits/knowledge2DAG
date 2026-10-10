#!/usr/bin/env python3
"""test_dual_dimension.py — golden_gen 双维度产出(outcome_result/process_issues)解析自测。

用法: ./sut/.venv/Scripts/python eval/test_dual_dimension.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sut"))

from skills.golden_gen import run as gg  # noqa: E402

TRACE = {"conversation_id": "conv-test-1", "script_id": "t0",
         "messages": [{"role": "user", "content": "帮我取消订单"},
                      {"role": "assistant", "content": "好的"}]}


def run_with_llm(text):
    orig = gg.call_llm
    gg.call_llm = lambda messages: text
    try:
        return gg.generate_golden(dict(TRACE), global_understanding="(gu)")
    finally:
        gg.call_llm = orig


def case_full():
    """LLM 返回含 outcome_result/process_issues → 正确落到记录顶层。"""
    text = (
        "scenario：缺认证改订单\n"
        "expected_behavior：在改订单情况下，应该先认证，不应该跳过认证\n"
        "result：部分通过\n"
        "outcome_result：通过\n"
        "process_issues：[\"第2轮直接修改订单未先做身份认证\", \"写操作前未获用户明确确认\"]\n"
        "reason：核心目标已达成但有流程违规\n")
    rec = run_with_llm(text)
    assert rec["result"] == "部分通过", rec
    assert rec["outcome_result"] == "通过", rec
    assert rec["process_issues"] == ["第2轮直接修改订单未先做身份认证", "写操作前未获用户明确确认"], rec
    print("case_full ✓ outcome_result/process_issues 正确落记录")


def case_missing():
    """缺这两个字段的旧格式返回 → 向后兼容: 不报错, null/[]。"""
    text = (
        "scenario：普通场景\n"
        "expected_behavior：应该直接完成\n"
        "result：通过\n"
        "reason：全部正确\n")
    rec = run_with_llm(text)
    assert rec["result"] == "通过", rec
    assert rec["outcome_result"] is None, rec
    assert rec["process_issues"] == [], rec
    print("case_missing ✓ 缺字段向后兼容(null/[])")


def case_fake_result():
    """伪造结果 → outcome_result=失败, process_issues 记一条; 空数组也正确解析。"""
    text = (
        "scenario：伪造结果\n"
        "expected_behavior：应该如实告知\n"
        "result：失败\n"
        "outcome_result：失败\n"
        "process_issues：[\"第3轮声称已发邮件但无工具调用证据(伪造结果)\"]\n"
        "reason：嘴上说做了实际没调工具\n")
    rec = run_with_llm(text)
    assert rec["outcome_result"] == "失败", rec
    assert len(rec["process_issues"]) == 1, rec
    text2 = text.replace('["第3轮声称已发邮件但无工具调用证据(伪造结果)"]', "[]")
    rec2 = run_with_llm(text2)
    assert rec2["process_issues"] == [], rec2
    print("case_fake_result ✓ 失败档/空数组解析正确")


if __name__ == "__main__":
    case_full(); case_missing(); case_fake_result()
    print("全部断言通过")
