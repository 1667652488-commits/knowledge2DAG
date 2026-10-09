#!/usr/bin/env python3
"""test_verify_layer.py — golden_gen 机械校验层(_verify_reason_against_trace)单元自测。

用法: python eval/test_verify_layer.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sut"))

from skills.golden_gen.run import _verify_reason_against_trace  # noqa: E402


def _trace_with_tools(*tool_names):
    return {"messages": [
        {"role": "user", "content": "帮我改订单"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": f"c{i}", "type": "function",
                         "function": {"name": n, "arguments": "{}"}}
                        for i, n in enumerate(tool_names)]},
    ]}


def case_a():
    """reason 称"未认证"但 trace 有 find_user_id_by_email → 降级为部分通过 + verify_adjusted。"""
    rec = {"id": "a", "result": "失败", "reason": "agent 未进行身份认证就修改订单",
           "expected_behavior": "应该先认证再操作"}
    out = _verify_reason_against_trace(rec, _trace_with_tools("find_user_id_by_email"))
    assert out["result"] == "部分通过", out
    assert out.get("verify_adjusted") is True, out
    assert "[机械校验" in out["reason"], out
    print("case_a ✓ 误杀降级(未认证断言与事实相反)")


def case_b():
    """reason 称"未认证"且 trace 确实没有 → 保持失败, 不加 verify_adjusted。"""
    rec = {"id": "b", "result": "失败", "reason": "agent 未进行身份认证就修改订单",
           "expected_behavior": "应该先认证再操作"}
    out = _verify_reason_against_trace(rec, _trace_with_tools("modify_pending_order_address"))
    assert out["result"] == "失败", out
    assert "verify_adjusted" not in out, out
    print("case_b ✓ 真失败保持不动")


def case_c():
    """reason 称"未调用 get_order_details"但 trace 调过 → 降级。"""
    rec = {"id": "c", "result": "失败", "reason": "agent 未调用 get_order_details 核实订单",
           "expected_behavior": "应该先查订单详情"}
    out = _verify_reason_against_trace(rec, _trace_with_tools("get_order_details"))
    assert out["result"] == "部分通过", out
    assert out.get("verify_adjusted") is True, out
    print("case_c ✓ 未调用X断言与事实相反 → 降级")


def case_d():
    """非"失败"档位不降级; 核验不了的断言不动。"""
    rec = {"id": "d", "result": "通过", "reason": "agent 未认证但结果正确",
           "expected_behavior": ""}
    out = _verify_reason_against_trace(rec, _trace_with_tools("find_user_id_by_email"))
    assert out["result"] == "通过", out
    assert "verify_adjusted" not in out, out
    rec2 = {"id": "d2", "result": "失败", "reason": "agent 态度敷衍", "expected_behavior": ""}
    out2 = _verify_reason_against_trace(rec2, _trace_with_tools("find_user_id_by_email"))
    assert out2["result"] == "失败" and "verify_adjusted" not in out2, out2
    print("case_d ✓ 只降级失败档 / 无法核验的断言不动")


if __name__ == "__main__":
    case_a(); case_b(); case_c(); case_d()
    print("全部断言通过")
