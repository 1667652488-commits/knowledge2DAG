#!/usr/bin/env python3
"""
rules_to_python_llm.py — 用 LLM 从 final_rules.json 的 judgment_criteria 生成可执行 Python checker

与旧 rules_to_python.py 的区别:
- 旧: 关键词出现就判失败(方向错, 误报率 89%)
- 新: LLM 阅读完整判据(触发条件+行为要求+失败样例), 生成"触发命中 AND agent 行为错误→失败, 否则通过"的 checker
- 每条规则编译+试跑校验, 失败带错误重试(最多2次)

输出 rules_python/rules.py, 保留 evaluate / evaluate_summary API(供 validate_rules.py 调用)。
"""
import json, re, os, sys, argparse, textwrap
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.llm_client import call_llm  # noqa
from common.config import get_config  # noqa

HEADER = '''#!/usr/bin/env python3
"""
rules.py — 由 rules_to_python_llm.py 从 cold-start 规则自动生成的可执行 checker

纪律: 每条规则只在"触发条件命中 AND agent 行为错误"时判失败; 触发不命中一律通过。
"""
import json, re
from dataclasses import dataclass


@dataclass
class RuleResult:
    result: str = "通过"
    reason: str = ""
    skill: str = ""
    confidence: float = 0.0


_ALL_RULES = []


def rule(id, skill="", confidence=0.5):
    def decorator(fn):
        fn._rule_id = id
        fn._rule_skill = skill
        fn._rule_confidence = confidence
        _ALL_RULES.append(fn)
        return fn
    return decorator


def _user_text(msgs):
    return "\\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "user")

def _assistant_text(msgs):
    return "\\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "assistant")

def _last_assistant(msgs):
    asst = [str(m.get("content", "")) for m in msgs if m.get("role") == "assistant"]
    return asst[-1] if asst else ""

def _tool_text(msgs):
    return "\\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "tool")

def _all_text(msgs):
    return "\\n".join(str(m.get("content", "")) for m in msgs)

'''

FOOTER = '''

def evaluate(trace: dict) -> list:
    results = []
    for fn in _ALL_RULES:
        try:
            results.append(fn(trace))
        except Exception as e:
            results.append(RuleResult("NA", f"规则 {fn._rule_id} 异常: {e}", fn._rule_skill, 0.0))
    return results


def evaluate_summary(trace: dict) -> dict:
    results = evaluate(trace)
    fails = [r for r in results if r.result == "失败"]
    if not fails:
        return {"result": "通过", "rules_hit": 0, "details": []}
    top = max(fails, key=lambda r: r.confidence)
    return {
        "result": "失败",
        "rules_hit": len(fails),
        "top_skill": top.skill,
        "top_confidence": top.confidence,
        "top_reason": top.reason,
        "details": [{"rule_id": fn._rule_id, "result": r.result, "skill": r.skill,
                     "confidence": r.confidence, "reason": r.reason}
                    for fn, r in zip(_ALL_RULES, results) if r.result == "失败"],
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        trace = json.load(open(sys.argv[1], encoding="utf-8"))
        print(json.dumps(evaluate_summary(trace), ensure_ascii=False, indent=2))
    else:
        print(f"共 {len(_ALL_RULES)} 条规则")
        for fn in _ALL_RULES:
            print(f"  {fn._rule_id}: skill={fn._rule_skill} conf={fn._rule_confidence}")
'''

EXAMPLE_BODY = '''msgs = trace.get("messages", [])
user = _user_text(msgs)
agent = _assistant_text(msgs)
if "30年" in user and "流动资金" in user:
    if not any(k in agent for k in ["异常", "不合理", "超过", "建议", "无法", "不支持", "提示", "违反", "确认"]):
        return RuleResult(result="失败", reason="用户请求30年流动资金贷款,agent未识别异常参数直接执行", skill="compliance_review", confidence=0.85)
return RuleResult(result="通过")'''


def _trace_turns(trace: dict, n: int = 6) -> str:
    """把 trace 的对话轮次压成文本(用户/agent)."""
    msgs = trace.get("messages", [])
    turns = []
    for m in msgs:
        role = m.get("role", "")
        c = str(m.get("content", ""))[:200]
        if role == "user":
            turns.append(f"  user: {c}")
        elif role == "assistant":
            turns.append(f"  agent: {c}")
        if len(turns) >= n:
            break
    return "\n".join(turns)


def build_prompt(rule: dict, idx: int, pos_examples: list = None) -> str:
    rule_id = rule.get("id", f"R{idx:03d}")
    skill_attr = rule.get("skill_attribution", {})
    top3 = skill_attr.get("top3", [])
    top1_skill = (top3[0].get("skill_name", "") if top3 else "") or rule.get("_skill_group", "")
    top1_conf = top3[0].get("confidence", 0.5) if top3 else 0.5

    if_conds = rule.get("if_conditions", [])
    crit_parts = []
    for c in if_conds:
        fd = c.get("feature_description", "")
        jc = c.get("judgment_criteria", "")
        if fd or jc:
            crit_parts.append(f"- 特征: {fd}\n  判据: {jc}")
    criteria_txt = "\n".join(crit_parts) if crit_parts else "（无）"

    neg = rule.get("few_shots", {}).get("negative_examples", [])
    neg_txt = ""
    if neg:
        e = neg[0]
        turns = "\n".join(f"  {t}" for t in e.get("turns", [])[:6])
        neg_txt = f"\n失败样例(必须判失败):\n{turns}\n为何失败: {e.get('why','')[:200]}"

    pos_txt = ""
    if pos_examples:
        blocks = []
        for t in pos_examples[:2]:
            blocks.append(_trace_turns(t))
        pos_txt = "\n通过样例(必须判 通过 —— 你的触发条件绝不能在这些 trace 上命中, 据此收紧触发):\n" + "\n---\n".join(blocks)

    return f'''你在为一个银行 agent 的轨迹评估器生成 Python 检查函数。

## trace 数据结构(重要, 避免误报)
trace 是 dict, trace["messages"] 是消息列表, 每条 {{role, content, name, metadata}}:
- role="user": 用户输入, content 为 str
- role="assistant": agent 回复, content 为 str —— 这是 agent 对用户说的话
- role="tool": 技能/工具返回, content 为 str —— 注意: tool 返回天然是 dict/JSON 字符串(如 {{"status":"success","data":...}} 或 {{'status':'awaiting_user_response',...}}), 这是正常格式, 绝不能因为 tool 文本是 dict 串就判失败。
- 只有当 agent 的回复(role=assistant)中出现原始 Python 对象引用(如 `<...object at 0x...>`)或明显未格式化的内存地址, 才算"未格式化暴露"。
- tool 文本中出现"失败""error""status":"fail"等词, 不等于本次调用失败 —— 必须结合 agent 是否据此编造成功状态来综合判断; 若 agent 据实回复, 不判失败。

## 可用辅助函数(直接调用, 无需定义/导入)
- _user_text(msgs) -> str: 拼接所有用户输入
- _assistant_text(msgs) -> str: 拼接所有 agent 回复
- _last_assistant(msgs) -> str: agent 最后一条回复
- _tool_text(msgs) -> str: 拼接所有 tool 返回
- _all_text(msgs) -> str: 拼接全部文本
- RuleResult(result=, reason=, skill=, confidence=): 结果对象, result 取 "失败" 或 "通过"
- 可用 re 模块

## 本条规则
id: {rule_id}
skill: {top1_skill}
confidence: {top1_conf}
error_category: {rule.get('error_category','')}
error_reason: {rule.get('error_reason','')}
expected_behavior: {rule.get('expected_behavior','')}
判定判据:
{criteria_txt}{neg_txt}{pos_txt}

## 要求(写一个函数体, 只写 def 下面的缩进语句)
- 函数签名 def check(trace): (不要写 def 行本身, 不要 @ 装饰器, 不要 import)
- 开头 msgs = trace.get("messages", [])
- 当【触发条件命中】且【agent 确实表现出坏行为 / 未做应做行为】时, return RuleResult(result="失败", reason="<简短中文坏行为描述>", skill="{top1_skill}", confidence={top1_conf})
- 否则 return RuleResult(result="通过")
- 关键纪律(务必遵守, 否则会产生大量误报):
  1. 触发条件必须窄: 只在本规则明确描述的具体场景命中时才继续检查; 若本轨迹不属于该场景, 立即 return RuleResult(result="通过")。
  2. 宁漏勿错: 只有当 agent 的坏行为有明确证据时才判失败; 证据不足或模棱两可时, return RuleResult(result="通过")。
  3. 区分 agent 回复与 tool 返回: 坏行为判定主要看 agent 的回复(role=assistant); tool 返回的 dict 串/含"失败"字样不构成坏行为。
  4. 不要用过于宽泛的关键词(如"完成""成功""状态""数据")作为坏行为判据 —— 正常回复也会出现。
- reason 简短中文, 不要换行。
- 只输出函数体代码本身, 不要解释, 不要 markdown 围栏, 不要 def 行。

## 格式示例(仅参考格式, 不要照搬逻辑)
{EXAMPLE_BODY}
'''


def extract_body(llm_out: str) -> str:
    """从 LLM 输出提取函数体(去掉围栏/def行/装饰器)."""
    s = llm_out.strip()
    # 去 markdown 围栏
    s = re.sub(r"^```(?:python)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    # 若含 def 行, 去掉 def ... : 及之前内容
    m = re.search(r"def\s+\w+\s*\(.*?\)\s*(?:->.*?)?:", s)
    if m:
        s = s[m.end():]
    # 去掉残留的 @ 装饰器行
    lines = s.splitlines()
    lines = [ln for ln in lines if not ln.strip().startswith("@")]
    s = "\n".join(lines)
    # 整体 dedent 到最小缩进
    s = textwrap.dedent(s)
    return s.strip()


def compile_check(body: str, rule: dict, idx: int, sample_traces: list, pos_examples: list = None) -> tuple:
    """编译+试跑校验. 返回 (ok, error_or_None).
    正例(pos_examples)必须判 通过, 否则视为失败(强制对比 spec)."""
    rule_id = rule.get("id", f"R{idx:03d}")
    skill_attr = rule.get("skill_attribution", {})
    top3 = skill_attr.get("top3", [])
    top1_skill = (top3[0].get("skill_name", "") if top3 else "") or rule.get("_skill_group", "")
    top1_conf = top3[0].get("confidence", 0.5) if top3 else 0.5
    func = (
        f'@rule(id="{rule_id}", skill="{top1_skill}", confidence={top1_conf})\n'
        f'def check(trace):\n'
        f'{textwrap.indent(body, "    ")}\n'
    )
    src = HEADER + func + "\n_check_result = check(_sample)\n"
    ns = {"_sample": sample_traces[0]}
    try:
        exec(compile(src, "<gen>", "exec"), ns)
        for t in sample_traces:
            r = ns["check"](t)
            if not hasattr(r, "result"):
                return False, f"返回值非 RuleResult: {type(r)}"
        # 正例必须判 通过
        if pos_examples:
            for i, t in enumerate(pos_examples[:2]):
                r = ns["check"](t)
                if r.result == "失败":
                    return False, f"正例{i}被误判失败(reason={r.reason[:40]}), 触发条件过宽"
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _is_llm_error(out: str) -> bool:
    """call_llm 失败时返回 [LLM ...] 错误串, 识别它避免当成代码喂给编译器。"""
    return out.startswith("[LLM") or "调用超时" in out or "连接失败" in out or "HTTP 错误" in out


def gen_one(rule: dict, idx: int, sample_traces: list, log, pos_examples: list = None) -> str:
    import time
    rule_id = rule.get("id", f"R{idx:03d}")
    prompt = build_prompt(rule, idx, pos_examples)
    for attempt in range(5):
        out = call_llm([
            {"role": "system", "content": "你是严谨的 Python 代码生成器, 只输出代码函数体, 不解释。"},
            {"role": "user", "content": prompt},
        ])
        if _is_llm_error(out) or not out.strip():
            log(f"  {rule_id}: attempt{attempt} LLM传输失败({out[:40]}), 退避重试")
            time.sleep(3)
            continue
        body = extract_body(out)
        if not body:
            log(f"  {rule_id}: attempt{attempt} 空输出, 重试")
            continue
        ok, err = compile_check(body, rule, idx, sample_traces, pos_examples)
        if ok:
            log(f"  {rule_id}: ✅ attempt{attempt}")
            return body
        log(f"  {rule_id}: attempt{attempt} 校验失败: {err}")
        prompt = build_prompt(rule, idx, pos_examples) + f"\n\n## 上次代码校验失败: {err}\n请收紧触发条件后重新输出函数体(正例必须判通过)。"
    log(f"  {rule_id}: ❌ 5次失败, 用兜底(恒通过)")
    return 'return RuleResult(result="通过")'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", required=True, help="final_rules.json")
    ap.add_argument("--output", default="rules_python", help="输出目录")
    ap.add_argument("--traces", default=None, help="sample trace 目录(用于试跑校验)")
    args = ap.parse_args()

    data = json.load(open(args.rules, encoding="utf-8"))
    # 兼容三种结构: {rules:[...]} / 顶层list / {scoped_rules:[{rules:{meta,rules:[...]}}]}(bundle merged)
    if isinstance(data, list):
        rules = data
    elif isinstance(data, dict):
        rules = data.get("rules")
        if not rules and data.get("scoped_rules"):
            rules = []
            for sr in data.get("scoped_rules", []) or []:
                sr_rules = (sr or {}).get("rules", [])
                if isinstance(sr_rules, dict):   # phase6 输出 {meta, rules:[...], rejected_rules}
                    sr_rules = sr_rules.get("rules", []) or sr_rules.get("rejected_rules", [])
                if isinstance(sr_rules, list):
                    rules.extend(sr_rules)
    else:
        rules = []
    rules = rules or []


    # 加载若干 sample trace 用于试跑
    from common import trace_io
    sample_traces = []
    if args.traces:
        sample_traces = trace_io.load_traces(args.traces)[:5]
    if not sample_traces:
        sample_traces = [{"messages": [{"role": "user", "content": "测试"}, {"role": "assistant", "content": "好的"}]}]

    # 建 skill_group -> 通过 trace 列表, 作为每条规则的正例(强制判通过, 收紧触发)
    pos_map = {}
    if args.traces:
        import glob, os as _os
        # golden 通过集
        golden_path = _os.path.join(_os.path.dirname(args.rules), "..", "golden", "golden_output.jsonl")
        passing_ids = set()
        if _os.path.exists(golden_path):
            for line in open(golden_path, encoding="utf-8"):
                d = json.loads(line)
                if d.get("result") == "通过":
                    passing_ids.add(d["script_id"])
        all_traces = {}
        for tf in sorted(glob.glob(os.path.join(args.traces, "*.json"))):
            sid = _os.path.basename(tf).replace("trace_", "").replace(".json", "")
            t = json.load(open(tf, encoding="utf-8"))
            all_traces[sid] = t
        for sid, t in all_traces.items():
            if sid not in passing_ids:
                continue
            sk = t.get("script", {}).get("skill", "") if isinstance(t.get("script"), dict) else ""
            pos_map.setdefault(sk, []).append(t)
        # 备用: 任意通过 trace
        any_passing = [t for sid, t in all_traces.items() if sid in passing_ids]

    def pos_for(rule):
        sk = rule.get("_skill_group", "")
        lst = pos_map.get(sk, [])
        if len(lst) >= 2:
            return lst[:2]
        # 同 skill 不足则补任意通过 trace
        need = 2 - len(lst)
        return lst + [t for t in any_passing if t not in lst][:need]

    logs = []
    def log(m):
        print(m, flush=True); logs.append(m)

    log(f"开始用 LLM 生成 {len(rules)} 条 checker(带正例对比)... (并行)")
    def work(item):
        i, r = item
        return (r, i, gen_one(r, i, sample_traces, log, pos_for(r)))
    with ThreadPoolExecutor(max_workers=3) as ex:
        bodies = list(ex.map(work, list(enumerate(rules))))

    # 组装
    funcs = []
    for r, i, body in bodies:
        rule_id = r.get("id", f"R{i:03d}")
        sa = r.get("skill_attribution", {})
        top3 = sa.get("top3", [])
        top1_skill = (top3[0].get("skill_name", "") if top3 else "") or r.get("_skill_group", "")
        top1_conf = top3[0].get("confidence", 0.5) if top3 else 0.5
        doc = (r.get("error_category", "") + ": " + r.get("error_reason", ""))[:160].replace('"', '\\"').replace("\n", " ")
        funcs.append(
            f'@rule(id="{rule_id}", skill="{top1_skill}", confidence={top1_conf})\n'
            f'def rule_{i}_{rule_id.lower()}(trace: dict) -> "RuleResult":\n'
            f'    """{doc}"""\n'
            f'{textwrap.indent(body, "    ")}\n'
        )
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rules.py").write_text(HEADER + "\n".join(funcs) + FOOTER, encoding="utf-8")
    log(f"\n生成完毕 → {out_dir/'rules.py'}  共 {len(funcs)} 条规则")
    (out_dir / "generation_log.txt").write_text("\n".join(logs), encoding="utf-8")


if __name__ == "__main__":
    main()
