# -*- coding: utf-8 -*-
"""失败轨迹 LLM 失败类型初判。

对 trace_stats.jsonl 中 reward=0 的轨迹调用 OpenAI 兼容 /chat/completions 分类。
配置从 sut/.env 读取（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL），key 不落日志。
并发 5，失败重试 2 次。结果增量写入 eval/failure_classification.jsonl（可断点续跑）。
"""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TAG = os.environ.get("TAG", "")
_sfx = f"_{TAG}" if TAG else ""
STATS_PATH = ROOT / "eval" / f"trace_stats{_sfx}.jsonl"
REF_PATH = ROOT / "eval" / "task_reference.jsonl"
WIKI_PATH = ROOT / "agent" / "tau_bench" / "tau_bench" / "envs" / "retail" / "wiki.md"
OUT_PATH = ROOT / "eval" / f"failure_classification{_sfx}.jsonl"
ENV_PATH = ROOT / "sut" / ".env"

CONCURRENCY = int(os.environ.get("CONCURRENCY", "5"))
MAX_RETRIES = 2
MAX_TOOL_CHARS = 350
MAX_DETAIL_CALLS = 14
MAX_LAST_MSG_CHARS = 800

FAILURE_TYPES = [
    "幻觉参数", "漏身份认证", "漏用户确认", "状态前置未查",
    "参数错误", "任务理解错误", "工具误用", "用户模拟器噪声", "其他",
]

PROMPT_TEMPLATE = """你是一个零售客服 agent 评测失败分析专家。下面是某任务的一次失败轨迹摘要（reward=0）。请判断 agent 的主要失败原因。

【任务指令 instruction】
{instruction}

【期望工具调用序列 expected_actions】
{expected}

【政策手册摘要 wiki（节选相关规则）】
{wiki}

【agent 实际工具调用序列】
{actual_seq}

【工具调用详情（参数与关键返回，已截断；含 Error 返回）】
{details}

【agent 最后回复】
{last_msg}

请严格输出 JSON（不要输出其他内容）：
{{"failure_type": "...", "evidence": "...", "user_simulator_fault": true/false}}

failure_type 必须从以下枚举中选一个：
- 幻觉参数：编造了工具未返回过的 id/信息（如凭空捏造 product_id、user_id、item_id）
- 漏身份认证：未按政策先验证用户身份就执行写操作
- 漏用户确认：政策要求执行前列出详情并征得用户明确确认（user 回复 yes），agent 漏了
- 状态前置未查：未先查询必要状态（订单/产品/用户详情）就执行操作
- 参数错误：用了真实存在但错误的 id/参数（张冠李戴）
- 任务理解错误：没理解用户要什么，做了错的事或漏做
- 工具误用：用错工具、调用方式错误、循环调用等
- 用户模拟器噪声：agent 行为基本符合政策与期望，但 reward=0（模拟器判错/未配合）
- 其他：以上都不适用

evidence 用一两句中文给出依据。user_simulator_fault=true 仅当你认为失败主要责任在用户模拟器而非 agent。
"""


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def compress_trace(row):
    seq = row["tool_call_sequence"]
    details = []
    for cd in row["tool_calls_detail"][:MAX_DETAIL_CALLS]:
        entry = f"- {cd['name']}({str(cd.get('arguments'))[:200]})"
        if cd.get("output"):
            entry += f" -> {cd['output'][:MAX_TOOL_CHARS]}"
        details.append(entry)
    for err in row.get("tool_errors", []):
        e = f"! ERROR in {err['tool']}: {err['output'][:MAX_TOOL_CHARS]}"
        if e not in details:
            details.append(e)
    return seq, "\n".join(details) if details else "(无)"


def select_wiki_excerpt(wiki_text):
    # 截取政策要点部分，控制长度
    if len(wiki_text) <= 2500:
        return wiki_text
    return wiki_text[:2500] + "\n...(截断)"


def build_prompt(row, ref, wiki_excerpt):
    seq, details = compress_trace(row)
    expected = ref.get("expected_actions_text") or "\n".join(ref.get("expected_action_names") or [])
    prompt = PROMPT_TEMPLATE.format(
        instruction=(ref.get("instruction") or "")[:1200],
        expected=str(expected)[:1200],
        wiki=wiki_excerpt,
        actual_seq=", ".join(seq) if seq else "(无工具调用)",
        details=details[:3000],
        last_msg=(row.get("last_assistant_message") or "")[:MAX_LAST_MSG_CHARS],
    )
    # 粗略控制：~2000 token ≈ 6000~7000 字符（中英混合），再硬截断
    return prompt[:8000]


def extract_json(text):
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no json object found")


async def classify_one(client, sem, env, row, ref, wiki_excerpt):
    prompt = build_prompt(row, ref, wiki_excerpt)
    url = env["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {env['LLM_API_KEY']}", "Content-Type": "application/json"}
    payload = {
        "model": env["LLM_MODEL"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    async with sem:
        last_err = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=120)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                result = extract_json(content)
                ft = result.get("failure_type", "其他")
                if ft not in FAILURE_TYPES:
                    ft = "其他"
                return {
                    "trace": row["trace"],
                    "task_id": row["task_id"],
                    "trial": row["trial"],
                    "failure_type": ft,
                    "evidence": str(result.get("evidence", ""))[:500],
                    "user_simulator_fault": bool(result.get("user_simulator_fault", False)),
                }
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(2 * (attempt + 1))
        # 重试耗尽：记为错误条目，主流程不中断
        return {
            "trace": row["trace"],
            "task_id": row["task_id"],
            "trial": row["trial"],
            "failure_type": "其他",
            "evidence": f"LLM 分类失败（重试{MAX_RETRIES}次后放弃），需人工复核",
            "user_simulator_fault": False,
            "classify_error": True,
            "error": last_err,
        }


async def amain():
    env = load_env()
    rows = [json.loads(l) for l in STATS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    fails = [r for r in rows if r["reward"] == 0.0]
    refs = {}
    for line in REF_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            refs[d["task_id"]] = d
    wiki_excerpt = select_wiki_excerpt(WIKI_PATH.read_text(encoding="utf-8"))

    done = set()
    if OUT_PATH.exists():
        for line in OUT_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    d = json.loads(line)
                    done.add((d["task_id"], d["trial"]))
                except Exception:
                    pass
    todo = [r for r in fails if (r["task_id"], r["trial"]) not in done]
    print(f"待分类: {len(todo)} / 总失败: {len(fails)}", flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    lock = threading.Lock()
    out_f = open(OUT_PATH, "a", encoding="utf-8")
    finished = [0]

    async def worker(row):
        async with httpx.AsyncClient() as client:
            res = await classify_one(client, sem, env, row, refs.get(row["task_id"], {}), wiki_excerpt)
        with lock:
            out_f.write(json.dumps(res, ensure_ascii=False) + "\n")
            out_f.flush()
            finished[0] += 1
            if finished[0] % 20 == 0 or finished[0] == len(todo):
                print(f"进度: {finished[0]}/{len(todo)}", flush=True)

    await asyncio.gather(*(worker(r) for r in todo))
    out_f.close()
    print("done", flush=True)


if __name__ == "__main__":
    asyncio.run(amain())
