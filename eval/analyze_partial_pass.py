# -*- coding: utf-8 -*-
"""对 golden 判"部分通过"的轨迹做归因分析：真发现 vs 噪声。"""
import json
import os
import re
import sys
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "runs/20261009_100203_golden/golden_output.jsonl"
TRACE_DIR = ROOT / "runs/20260903_105240_strong_skilldriven_baseline_full/traces"
REWARD = ROOT / "runs/20260903_105240_strong_skilldriven_baseline_full/tau_reward.json"
ENV_FILE = ROOT / "sut/.env"
TS = time.strftime("%Y%m%d_%H%M%S")
OUT_DIR = ROOT / f"runs/{TS}_partial_review"
CLASS_JSONL = OUT_DIR / "classification.jsonl"

CONCURRENCY = 8
RETRIES = 2

SYSTEM_PROMPT = """你是严格的 AI agent 行为评审专家。golden 评审器把一条 agent 对话轨迹判为"部分通过"，给出了 expected_behavior（期望行为）和 reason（扣分理由）。你的任务：核对 reason 与轨迹事实是否相符，判断该"部分通过"是"真发现"还是"噪声"。

fact_check 判定：
- "成立"：golden 的 reason 描述的问题与轨迹事实相符（如声称未做用户认证，轨迹中确实没有 find_user_id_by_email/find_user_id_by_phone_number 等认证调用）。
- "不成立"：reason 与事实矛盾（如声称未认证但轨迹里有认证调用；声称未列细节确认但 agent 实际列了且用户说了 yes）。
- "无法判断"：证据不足。

verdict 判定：
- "真发现"：golden 指出的毛病客观存在且值得指出（含 fact_check 成立的真违规，如漏认证、漏确认、伪造结果、流程顺序错误等实质违规）。
- "噪声"：事实错误（fact_check 不成立）或吹毛求疵（不影响业务结果的措辞/风格苛求）。

issue_type 从以下枚举选一个：漏认证、漏确认、伪造结果、流程顺序、技能误用、规则理解、交互质量、其他。

只输出一个严格 JSON 对象，不要任何其他文字：
{"issue_type": "...", "fact_check": "成立|不成立|无法判断", "verdict": "真发现|噪声", "rationale": "一句话"}"""


def load_env():
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env["LLM_BASE_URL"].rstrip("/"), env["LLM_API_KEY"], env["LLM_MODEL"]


def load_trace_index():
    idx = {}
    for f in TRACE_DIR.glob("trace_*_t*.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        cid = t.get("conversation_id")
        if cid:
            idx[cid] = (f, t)
    return idx


def summarize_trace(t):
    msgs = t.get("messages", [])
    instruction = (t.get("script") or {}).get("instruction", "")
    tool_names = []
    errors = []
    last_reply = ""
    for m in msgs:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tool_names.append(tc.get("name"))
            if m.get("content"):
                last_reply = m["content"]
        elif role == "tool":
            c = m.get("content") or ""
            if "error" in c.lower() or "Error" in c:
                errors.append(c[:200])
    return {
        "instruction": instruction,
        "tool_call_sequence": tool_names,
        "tool_errors": errors[:10],
        "agent_last_reply_500": last_reply[-500:],
    }


def build_user_prompt(g, summary, reward):
    return json.dumps({
        "任务 instruction": summary["instruction"],
        "golden expected_behavior": g.get("expected_behavior", ""),
        "golden reason（扣分理由）": g.get("reason", ""),
        "实际工具调用名序列": summary["tool_call_sequence"],
        "工具返回中的 Error 信息": summary["tool_errors"],
        "agent 最后回复(截断500字)": summary["agent_last_reply_500"],
        "硬判分 reward": reward,
    }, ensure_ascii=False, indent=1)


def call_llm(client, base, key, model, user_prompt):
    r = client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    return json.loads(m.group(0))


def main():
    base, key, model = load_env()
    golden = [json.loads(l) for l in GOLDEN.open(encoding="utf-8")]
    partial = [x for x in golden if x.get("result") == "部分通过"]
    rewards = json.loads(REWARD.read_text(encoding="utf-8"))
    trace_idx = load_trace_index()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {OUT_DIR}", flush=True)
    print(f"部分通过: {len(partial)} 条; trace 索引: {len(trace_idx)}", flush=True)

    done_ids = set()
    if CLASS_JSONL.exists():
        for l in CLASS_JSONL.open(encoding="utf-8"):
            try:
                done_ids.add(json.loads(l)["id"])
            except Exception:
                pass

    todo = [g for g in partial if g["id"] not in done_ids]
    print(f"已完成 {len(done_ids)}, 待处理 {len(todo)}", flush=True)

    lock = threading.Lock()
    out_f = CLASS_JSONL.open("a", encoding="utf-8")

    def work(g):
        cid = g["id"]
        rec = dict(g)
        if cid not in trace_idx:
            rec["llm_judge"] = None
            rec["error"] = "trace not found"
            return rec
        f, t = trace_idx[cid]
        m = re.match(r"trace_(\d+)_t(\d+)\.json", f.name)
        rkey = f"tau_{m.group(1)}_t{m.group(2)}" if m else None
        reward = rewards.get(rkey, {}).get("reward") if rkey else None
        rec["trace_file"] = str(f.relative_to(ROOT))
        rec["reward_key"] = rkey
        rec["reward"] = reward
        summary = summarize_trace(t)
        prompt = build_user_prompt(g, summary, reward)
        client = httpx.Client()
        try:
            judge = None
            for attempt in range(RETRIES + 1):
                try:
                    judge = call_llm(client, base, key, model, prompt)
                    break
                except Exception as e:
                    if attempt == RETRIES:
                        rec["error"] = f"llm failed: {e}"
                    time.sleep(2 * (attempt + 1))
            rec["llm_judge"] = judge
        finally:
            client.close()
        return rec

    n_done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(work, g): g for g in todo}
        for fut in as_completed(futs):
            rec = fut.result()
            with lock:
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out_f.flush()
                n_done += 1
                if n_done % 10 == 0:
                    print(f"进度 {n_done}/{len(todo)}", flush=True)
    out_f.close()
    print("分类完成", flush=True)


if __name__ == "__main__":
    main()
