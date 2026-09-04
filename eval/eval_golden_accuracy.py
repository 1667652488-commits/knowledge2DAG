"""评测：golden 软标工具产出的 expected_behavior（EB）vs τ-bench 任务参考真值。

本项目对拍关系（已改造，移植自 openjiuwen 项目 test/eval_golden_accuracy.py）：
- candidate = 待评测，golden_output.jsonl（golden 软标工具自动产出，字段含
  script_id/expected_behavior/result/reason/inputs）。
- standard  = 任务参考真值 eval/task_reference.jsonl（由 eval/build_task_reference.py
  生成，字段：script_id(形如 tau_0)/instruction/expected_actions_text/expected_action_names）。
- 配对：直接按 script_id 取交集（删掉了原脚本的 trace 桥接逻辑）。

思路：
- 判定：LLM 裁判（读 eval/eval_judge_config.json，不碰 .env）按三要素
  （场景前提/应该做/不应该做）逐项判 等价2/部分1/不等0，整条综合。
  对位关系（与原脚本不同，核心改动）：
  - scenario   ←→ reference.instruction（任务前提）；
  - should     ←→ reference.expected_actions_text（期望工具调用序列的行为含义；
    EB 是原则级业务语言，expected actions 是具体调用，判语义覆盖而非字面一致）；
  - should_not ←→ 政策手册相关条款（policy 文件全文给裁判自行定位；
    expected actions 中没有"不应该"的真值）。
- 伴随指标：embedding 余弦相似度。口径说明：整条 EB 与 expected_actions_text
  一个是业务语言、一个是具体调用序列，直接对 sim 解释力弱，故整条 sim 采用
  「candidate EB vs instruction + expected_actions_text 拼接」；三要素 sim 用
  裁判拆出的对应参考段（scenario→instruction 摘要 / should→actions 摘要 /
  should_not→裁判定位的政策条款）。阈值口径保留 0.75。
- 准确率（头版4指标）：
  [LLM判定]严格(含容忍·overall==2或tolerant, 超集/粒度差异不降档) / [LLM判定]宽松(三要素均部分等价以上)
  [相似度判定]整条 sim>=0.75 / [相似度判定]平均整条相似度(0-100)。
- 分组：case_type 缺失容忍；script_id 恒为 tau_N 前缀，不再按 trace 类别分组，
  简化为按 candidate result（通过/失败）分组统计。

输出：
  终端打印摘要（准确率 + 三档分布带平均sim + 三要素带平均sim + 分组）
  <candidate 同目录>/eval_report_<ts>.md   完整报告（含不一致 case 清单）
  <candidate 同目录>/eval_detail_<ts>.csv   每条明细（含各要素 sim + reason）待查

用法（Windows，路径用正斜杠）::

    python eval/eval_golden_accuracy.py <candidate_golden_output.jsonl> \
        [--reference eval/task_reference.jsonl] \
        [--policy agent/tau_bench/tau_bench/envs/retail/wiki.md]

依赖：httpx + 标准库。本项目 agent/.venv 已装 httpx（0.28.1），推荐用
agent/.venv/Scripts/python.exe 运行；若用其它环境请先 pip install httpx。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:  # 允许无 httpx 环境导入本模块（如自测脚本 monkeypatch 掉网络调用）
    httpx = None

# ---- 路径常量（相对项目根解析，脚本位置固定在 <root>/eval/ 下） ----
EVAL_DIR = Path(__file__).resolve().parent
REPO = EVAL_DIR.parent
JUDGE_CONFIG = EVAL_DIR / "eval_judge_config.json"
DEFAULT_REFERENCE = EVAL_DIR / "task_reference.jsonl"
DEFAULT_POLICY = REPO / "agent" / "tau_bench" / "tau_bench" / "envs" / "retail" / "wiki.md"

# embedding 默认（硅基流动 SiliconFlow，OpenAI 兼容）。可在 eval_judge_config.json 覆盖。
_DEFAULT_EMBED_BASE = "https://api.siliconflow.cn/v1"
_DEFAULT_EMBED_MODEL = "BAAI/bge-large-zh-v1.5"

# 裁判
JUDGE_TIMEOUT = 120
EMBED_TIMEOUT = 60
_JUDGE_MAX_RETRIES = 3

# 相似度口径等价阈值：整条余弦 sim >= 该值算"等价"（相似度判定）。
SIM_EQ_THRESHOLD = 0.75

_ELEMENTS = ["scenario", "should", "should_not"]
_SCORE_LABEL = {2: "等价", 1: "部分等价", 0: "不等价"}


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def _load_judge_config() -> dict[str, str]:
    """读 eval/eval_judge_config.json（裁判 + embedding 配置，gitignore 不提交）。

    含两组配置（全程不碰 .env）：
    - judge_*：裁判模型（judge_api_key/judge_base_url/judge_model 必填）。
    - embed_*：embedding（embed_api_key 必填；embed_base_url/embed_model 可缺省走硅基流动默认）。
    """
    if not JUDGE_CONFIG.exists():
        print(f"✗ 找不到配置文件：{JUDGE_CONFIG}")
        print("  请创建 eval/eval_judge_config.json，字段："
              "judge_api_key/judge_base_url/judge_model/embed_api_key"
              "/embed_base_url(可缺省)/embed_model(可缺省)。")
        sys.exit(1)
    cfg = json.loads(JUDGE_CONFIG.read_text(encoding="utf-8"))
    for k in ("judge_api_key", "judge_base_url", "judge_model"):
        if not cfg.get(k):
            print(f"✗ 配置缺 {k}，请填 {JUDGE_CONFIG}")
            sys.exit(1)
    cfg.setdefault("embed_base_url", _DEFAULT_EMBED_BASE)
    cfg.setdefault("embed_model", _DEFAULT_EMBED_MODEL)
    if not cfg.get("embed_api_key"):
        print(f"✗ 配置缺 embed_api_key（embedding key），请填 {JUDGE_CONFIG}")
        sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# embedding（OpenAI 兼容 /v1/embeddings）
# ---------------------------------------------------------------------------


def embed_texts(
    texts: list[str], key: str, base: str, model: str
) -> list[list[float]]:
    """批量 embedding（OpenAI 兼容 /v1/embeddings），返回与 texts 等长的向量列表。

    硅基流动标准格式：body {model, input:[texts]}，返回 data[].embedding 按 index 对齐。
    空文本传占位避免 400。
    """
    cleaned = [(t if t else " ") for t in texts]
    url = base.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model, "input": cleaned}
    r = httpx.post(url, headers=headers, json=body, timeout=EMBED_TIMEOUT)
    r.raise_for_status()
    data = r.json()["data"]
    vecs: list[list[float] | None] = [None] * len(texts)
    for item in data:
        vecs[item["index"]] = item["embedding"]
    return [v if v is not None else [] for v in vecs]


def cosine(a: list[float], b: list[float]) -> float:
    """纯 python 余弦相似度（零依赖）。任一空返回 0.0。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# 裁判（拆三要素 + 逐要素判档 + reason；标准侧 = 任务参考真值 + 政策手册）
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """你是 expected_behavior（EB）一致性裁判，判断「待评测 EB」是否正确覆盖了 τ-bench 任务的参考真值。

待评测 EB 是 golden 软标工具产出的三段式原则级业务语言：「在 X 情况下，应该 Y，不应该 Z」。
- scenario（场景前提）：触发该行为的上下文/前提条件。
- should（应该做）：agent 的正确做法（含工具调用方向）。
- should_not（不应该做）：agent 应避免的错误做法。

参考真值不是另一份 EB，而是以下三类材料：
- scenario 段 ←→ 任务指令（instruction）：任务前提/用户身份/诉求。
- should 段 ←→ 期望工具调用序列（expected actions）：注意 EB 是原则级业务语言，
  expected actions 是具体工具调用（含工具名/参数），判"语义覆盖"而非字面一致——
  EB 的"应该"若正确表达了该调用序列的行为含义（先认证用户、再查订单/商品、最后执行
  换货/退货/修改等写操作，含关键选项约束如颜色/尺寸/支付方式）即算覆盖，不要求出现工具名。
- should_not 段 ←→ 政策手册（policy）相关条款：期望调用序列中没有"不应该"的真值，
  请从随附政策手册全文中自行定位与本任务相关的条款（如换退货条件、支付方式限制、
  不得泄露隐私等），判断 EB 的"不应该"是否与这些条款一致、无冲突。若 EB 的 should_not
  为空但政策确有本任务易踩的禁止条款，酌情降档；若政策无相关禁止条款，EB 该段为空不扣分。

判定口径（逐要素，2/1/0）：
- 等价(2)：行为方向 + 关键约束一致（措辞/粒度/抽象层次可不同）。EB 比参考多覆盖合理并列要素（合理超集）也算等价，不因"多要素"降档。
- 部分(1)：方向对，但缺一个要素；或 EB 陷入实现层代码细节（直接透传工具名/字段名/步骤序列化）偏离业务语言；或关键约束只覆盖了一部分（如只写了换货没写"没有合适键盘就只换恒温器"的分支）。注意：EB"多识别了并列的合理业务问题/约束"不算粒度过细，不据此降档。
- 不等(0)：行为方向相反 / 遗漏关键工具调用对应的行为（如漏了认证、漏了写操作）/ 关键约束与 expected actions 或政策冲突。

整条综合 overall：
- 三要素全 2 -> 2（等价）
- 有 1 但无 0 -> 1（部分）
- 任一 0 -> 0（不等）

【超集容忍（tolerant 字段，重要——容许归纳波动与抽象层次差异）】
expected actions 是具体调用，EB 是原则归纳，两者抽象层次天然不同。只要方向一致、无关键冲突，应予容忍。tolerant=true 当且仅当满足全部：
  (1) 三要素均无 0（无方向冲突 / 无遗漏关键行为 / 无关键约束冲突）；
  (2) EB 与参考的差异仅属以下"可容忍"类型之一：
      (a) EB 是参考的合理超集——多覆盖了并列的合理业务要素（如多识别了一个并列的合理问题/约束）；
      (b) 措辞、粒度、抽象层次不同但行为方向与关键约束一致（业务语言 vs 具体调用属此类）；
      (c) EB 对某要素做了合理细化但未改变方向。
tolerant=false 当：EB 缺参考的关键行为（如漏掉认证步骤、漏掉分支条件），或有任一要素 0。
（overall==2 蕴含 tolerant=true；tolerant=true 是"在容忍范围内视同等价"的口径，供容忍准确率统计。）

请先把待评测 EB 拆成三要素文本（scenario/should/should_not，无则空串），
并为每个要素给出对应的参考侧文本（scenario→instruction 中相关前提摘要；
should→expected actions 对应的行为含义摘要；should_not→政策手册中定位到的相关条款摘要，无则空串），
再逐要素按口径判档。只输出严格 JSON（不要 markdown 围栏，不要解释）：
{
  "scenario": {"standard": "...", "candidate": "...", "score": 0-2, "reason": "..."},
  "should": {"standard": "...", "candidate": "...", "score": 0-2, "reason": "..."},
  "should_not": {"standard": "...", "candidate": "...", "score": 0-2, "reason": "..."},
  "overall": 0-2,
  "tolerant": true或false,
  "reason": "整条判定理由（简述关键差异 + 是否超集容忍）"
}"""


def _judge_one(
    instruction: str,
    expected_actions_text: str,
    policy_text: str,
    candidate_eb: str,
    judge_cfg: dict[str, str],
) -> dict:
    """调裁判一条，返回结构化判定（失败重试3次，全失败 overall=-1）。"""
    user_prompt = (
        f"任务指令（instruction，scenario 段真值）：\n{instruction}\n\n"
        f"期望工具调用序列（expected actions，should 段真值）：\n{expected_actions_text}\n\n"
        f"政策手册全文（policy，should_not 段真值来源，请自行定位相关条款）：\n{policy_text}\n\n"
        f"待评测 EB：\n{candidate_eb}\n\n"
        f"请按口径判定，只输出 JSON。"
    )
    url = judge_cfg["judge_base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {judge_cfg['judge_api_key']}",
        "Content-Type": "application/json",
    }
    body = {
        "model": judge_cfg["judge_model"],
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    last_err = "未知错误"
    for attempt in range(1, _JUDGE_MAX_RETRIES + 1):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=JUDGE_TIMEOUT)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            parsed = _parse_judge_json(content)
            if parsed is not None:
                return parsed
            last_err = f"JSON 解析失败: {content[:120]}"
        except Exception as e:  # noqa: BLE001 — 重试
            last_err = f"{type(e).__name__}: {e}"
        print(f"    裁判第 {attempt}/{_JUDGE_MAX_RETRIES} 次失败: {last_err[:80]}")
        time.sleep(1)
    return {
        "scenario": {"standard": "", "candidate": "", "score": -1, "reason": last_err},
        "should": {"standard": "", "candidate": "", "score": -1, "reason": ""},
        "should_not": {"standard": "", "candidate": "", "score": -1, "reason": ""},
        "overall": -1,
        "tolerant": False,
        "reason": f"裁判失败: {last_err}",
    }


def _parse_judge_json(content: str) -> dict | None:
    """容错解析裁判 JSON（去 markdown 围栏 / 提取首个 {...}）。"""
    if not content:
        return None
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


# ---------------------------------------------------------------------------
# 配对（candidate golden_output.jsonl × task_reference.jsonl，按 script_id 取交集）
# ---------------------------------------------------------------------------


def _load_reference(path: Path) -> dict[str, dict]:
    """读 task_reference.jsonl -> {script_id: {instruction, expected_actions_text, expected_action_names}}。"""
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        sid = r.get("script_id")
        if not sid:
            continue
        out[sid] = {
            "instruction": r.get("instruction", ""),
            "expected_actions_text": r.get("expected_actions_text", ""),
            "expected_action_names": r.get("expected_action_names", []),
        }
    return out


def _load_candidate(path: Path) -> list[dict]:
    """读 candidate golden_output.jsonl：提取 {script_id, eb, result, reason, inputs, case_type}。

    跳过 error/空 expected_behavior。case_type 可缺（容忍缺失）。
    """
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        eb = r.get("expected_behavior", "")
        if r.get("error") or not eb:
            continue
        sid = r.get("script_id") or ""
        if not sid:
            continue
        inputs = r.get("inputs")
        if isinstance(inputs, list):
            inputs = " ".join(str(x) for x in inputs)
        out.append(
            {
                "script_id": sid,
                "eb": eb,
                "result": r.get("result", "") or "",
                "golden_reason": r.get("reason", "") or "",
                "inputs": inputs or "",
                "case_type": r.get("case_type", "") or "",
            }
        )
    return out


def build_pairs(reference_path: Path, candidate_path: Path) -> list[dict]:
    """配对：candidate 与 reference 直接按 script_id 取交集。"""
    ref_map = _load_reference(reference_path)
    cand = _load_candidate(candidate_path)

    pairs: list[dict] = []
    seen: set[str] = set()
    for cr in cand:
        sid = cr["script_id"]
        rr = ref_map.get(sid)
        if not rr:
            continue
        seen.add(sid)
        pairs.append(
            {
                "script_id": sid,
                "case_type": cr["case_type"],
                "result": cr["result"],
                "golden_reason": cr["golden_reason"],
                "inputs": cr["inputs"],
                "instruction": rr["instruction"],
                "expected_actions_text": rr["expected_actions_text"],
                "candidate_eb": cr["eb"],
            }
        )
    print(
        f"配对：任务参考 {len(ref_map)} 条 / 待评测 {len(cand)} 条，"
        f"成功配对 {len(pairs)} 条"
    )
    only_ref = len(ref_map) - len(seen)
    only_cand = len(cand) - len(pairs)
    if only_ref:
        print(f"  任务参考有但待评测无（跳过）：{only_ref} 条")
    if only_cand:
        print(f"  待评测有但任务参考无（跳过）：{only_cand} 条")
    return pairs


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _safe_mean(vals: list[float]) -> float:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _rate(scores: list[int], target: int) -> float:
    return (scores.count(target) / len(scores)) if scores else 0.0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="golden EB（待评测）vs τ-bench 任务参考真值 一致性评测"
    )
    ap.add_argument("candidate", help="待评测 golden_output.jsonl（含 script_id/expected_behavior）")
    ap.add_argument(
        "--reference",
        default=str(DEFAULT_REFERENCE),
        help=f"任务参考真值 jsonl（默认 {DEFAULT_REFERENCE}）",
    )
    ap.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY),
        help=f"政策手册 wiki.md 路径（默认 {DEFAULT_POLICY}）",
    )
    args = ap.parse_args(argv)

    candidate_path = Path(args.candidate)
    reference_path = Path(args.reference)
    policy_path = Path(args.policy)
    for p in (candidate_path, reference_path, policy_path):
        if not p.exists():
            print(f"✗ 文件不存在：{p}")
            sys.exit(1)

    print(f"任务参考：{reference_path}")
    print(f"待评测：  {candidate_path}")
    print(f"政策手册：{policy_path}")
    print()

    judge_cfg = _load_judge_config()
    print(f"裁判模型：{judge_cfg['judge_model']} @ {judge_cfg['judge_base_url']}")
    print(f"embedding：{judge_cfg['embed_model']} @ {judge_cfg['embed_base_url']}")
    print()

    policy_text = policy_path.read_text(encoding="utf-8")

    pairs = build_pairs(reference_path, candidate_path)
    if not pairs:
        print("✗ 无可评配对，退出。检查两份文件 script_id 是否有交集。")
        return

    run_dir = candidate_path.parent
    # 落原路径（candidate 同目录），文件名加时间戳防重跑覆盖
    eval_ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = run_dir / f"eval_detail_{eval_ts}.csv"
    report_path = run_dir / f"eval_report_{eval_ts}.md"
    print(f"本次评测产物（原路径，时间戳防覆盖）：")
    print(f"  {report_path}")
    print(f"  {csv_path}")
    print()

    rows: list[dict] = []
    n = len(pairs)
    for i, p in enumerate(pairs, 1):
        sid = p["script_id"]
        ct = p["case_type"] or "-"
        print(f"[{i:>2}/{n}] {sid} ({ct}) 裁判中...", end=" ")
        verdict = _judge_one(
            p["instruction"],
            p["expected_actions_text"],
            policy_text,
            p["candidate_eb"],
            judge_cfg,
        )
        overall = int(verdict.get("overall", -1))

        # tolerant：裁判判的"超集容忍"标志（容许归纳波动与抽象层次差异）。
        # overall==2 蕴含 tolerant；裁判未返回该字段时回退到 overall==2。
        raw_tol = verdict.get("tolerant", False)
        if isinstance(raw_tol, str):
            is_tolerant = raw_tol.strip().lower() in ("true", "1", "yes", "是")
        else:
            is_tolerant = bool(raw_tol)
        is_tolerant = bool(is_tolerant or overall == 2)

        # embedding 口径：整条 = candidate EB vs (instruction + expected_actions_text) 拼接
        # （EB 是业务语言、actions 是具体调用，直接对 sim 解释力弱，拼接任务前提更接近 EB 语义）。
        # 三要素段 = 裁判拆出的 candidate 段 vs 参考侧段（instruction 摘要 / actions 摘要 / 政策条款）。
        ref_full = (p["instruction"] + "\n" + p["expected_actions_text"]).strip()
        texts = [ref_full, p["candidate_eb"]]
        for e in _ELEMENTS:
            seg = verdict.get(e, {})
            texts.append(seg.get("standard", ""))
            texts.append(seg.get("candidate", ""))
        try:
            vecs = embed_texts(
                texts,
                judge_cfg["embed_api_key"],
                judge_cfg["embed_base_url"],
                judge_cfg["embed_model"],
            )
        except Exception as ex:  # noqa: BLE001
            print(f"embedding 失败: {ex}")
            vecs = [[] for _ in texts]

        overall_sim = cosine(vecs[0], vecs[1])
        elem_sim: dict[str, float] = {}
        for j, e in enumerate(_ELEMENTS):
            elem_sim[e] = cosine(vecs[2 + j * 2], vecs[3 + j * 2])

        print(f"{_SCORE_LABEL.get(overall, '错误')}")

        row = {
            "script_id": sid,
            "case_type": p["case_type"],
            "result": p["result"],
            "overall_score": overall,
            "tolerant": int(is_tolerant),
            "overall_sim": f"{overall_sim:.4f}",
            "scenario_score": int(verdict.get("scenario", {}).get("score", -1)),
            "scenario_sim": f"{elem_sim['scenario']:.4f}",
            "should_score": int(verdict.get("should", {}).get("score", -1)),
            "should_sim": f"{elem_sim['should']:.4f}",
            "should_not_score": int(verdict.get("should_not", {}).get("score", -1)),
            "should_not_sim": f"{elem_sim['should_not']:.4f}",
            "reason": verdict.get("reason", ""),
            "instruction": p["instruction"],
            "expected_actions_text": p["expected_actions_text"],
            "candidate_eb": p["candidate_eb"],
            "inputs": p["inputs"],
        }
        rows.append(row)

    # ---- 汇总 ----
    valid = [r for r in rows if r["overall_score"] >= 0]
    nv = len(valid)
    # 来源1：LLM 判定（严格 = overall==2 或 裁判判 tolerant——超集/粒度差异不降档）
    n_tolerant = sum(1 for r in valid if r["tolerant"])
    accuracy = n_tolerant / nv if nv else 0.0  # 严格(含容忍)
    # 宽松：三要素均部分等价以上（每要素 >=1，即无不等价要素）
    loose_rows = [
        r
        for r in valid
        if r["scenario_score"] >= 1
        and r["should_score"] >= 1
        and r["should_not_score"] >= 1
    ]
    loose = len(loose_rows) / nv if nv else 0.0
    # 来源2：相似度判定（整条 sim >= SIM_EQ_THRESHOLD 算等价）
    overall_sims = [float(r["overall_sim"]) for r in valid]
    n_sim = sum(1 for s in overall_sims if s >= SIM_EQ_THRESHOLD)
    acc_sim = n_sim / nv if nv else 0.0
    avg_overall_sim = _safe_mean(overall_sims)

    by_overall: dict[int, list[dict]] = {2: [], 1: [], 0: []}
    for r in valid:
        by_overall.setdefault(r["overall_score"], []).append(r)

    elem_stats = {}
    for e in _ELEMENTS:
        col = f"{e}_score"
        es = [r[col] for r in valid]
        elem_stats[e] = {
            "eq_rate": _rate(es, 2),
            "avg_sim": _safe_mean([float(r[f"{e}_sim"]) for r in valid]),
        }

    def _group_rate(subset: list[dict]) -> float:
        if not subset:
            return 0.0
        return sum(1 for r in subset if r["tolerant"]) / len(subset)

    by_case: dict[str, list[dict]] = {}
    by_result: dict[str, list[dict]] = {}
    for r in valid:
        if r["case_type"]:
            by_case.setdefault(r["case_type"], []).append(r)
        by_result.setdefault(r["result"] or "未知", []).append(r)

    # ---- 终端摘要 ----
    print()
    print("=" * 64)
    print("准确率：")
    print(
        f"  [LLM 判定] 严格(含容忍·超集/粒度差异不降档): {accuracy*100:5.1f}% ({n_tolerant}/{nv})"
    )
    print(
        f"  [LLM 判定] 宽松(三要素均部分等价以上):  {loose*100:5.1f}% ({len(loose_rows)}/{nv})"
    )
    print(
        f"  [相似度判定] 整条 sim>={SIM_EQ_THRESHOLD}:           {acc_sim*100:5.1f}% ({n_sim}/{nv})"
    )
    print(
        f"  [相似度判定] 平均整条相似度:           {avg_overall_sim*100:5.1f}/100"
    )
    print(f"样本数：{nv}（配对 {len(rows)}，裁判失败 {len(rows)-nv}）")
    print("-" * 64)
    print("三档分布（条数 / 占比 / 平均整条sim）：")
    for s in (2, 1, 0):
        grp = by_overall.get(s, [])
        cnt = len(grp)
        pct = cnt / nv if nv else 0.0
        avg_sim = _safe_mean([float(r["overall_sim"]) for r in grp])
        print(f"  {_SCORE_LABEL[s]:>4}：{cnt:>3} 条 / {pct*100:5.1f}% / sim={avg_sim:.4f}")
    print("-" * 64)
    print("三要素一致率（等价率 / 平均sim）：")
    for e in _ELEMENTS:
        st = elem_stats[e]
        print(f"  {e:>12}：等价率 {st['eq_rate']*100:5.1f}% / sim={st['avg_sim']:.4f}")
    print("-" * 64)
    print("分组准确率（严格）：")
    if by_case:
        print("  按 case_type：")
        for ct, grp in sorted(by_case.items()):
            print(f"    {ct:>10}：{_group_rate(grp)*100:5.1f}% （{len(grp)} 条）")
    else:
        print("  按 case_type：（candidate 无 case_type 字段，跳过）")
    print("  按 candidate result：")
    for res, grp in sorted(by_result.items()):
        print(f"    {res:>10}：{_group_rate(grp)*100:5.1f}% （{len(grp)} 条）")
    print("=" * 64)

    # ---- CSV 明细 ----
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # ---- 报告 md ----
    lines: list[str] = []
    lines.append("# golden EB vs τ-bench 任务参考真值 一致性评测报告\n")
    lines.append(f"- 任务参考：`{reference_path}`")
    lines.append(f"- 待评测：  `{candidate_path}`")
    lines.append(f"- 政策手册：`{policy_path}`")
    lines.append(f"- 裁判模型：{judge_cfg['judge_model']} @ {judge_cfg['judge_base_url']}")
    lines.append(f"- embedding：{judge_cfg['embed_model']} @ {judge_cfg['embed_base_url']}")
    lines.append(f"- 样本数：{nv}（配对 {len(rows)}，裁判失败 {len(rows)-nv}）\n")

    lines.append("## 准确率\n")
    lines.append("| 口径 | 值 |")
    lines.append("|---|---|")
    lines.append(
        f"| [LLM 判定] 严格（含容忍·超集/粒度差异不降档） | {accuracy*100:.1f}%（{n_tolerant}/{nv}） |"
    )
    lines.append(
        f"| [LLM 判定] 宽松（三要素均部分等价以上） | {loose*100:.1f}%（{len(loose_rows)}/{nv}） |"
    )
    lines.append(
        f"| [相似度判定] 整条 sim>={SIM_EQ_THRESHOLD} | {acc_sim*100:.1f}%（{n_sim}/{nv}） |"
    )
    lines.append(f"| [相似度判定] 平均整条相似度 | {avg_overall_sim*100:.1f}/100 |")
    lines.append("")
    lines.append("**对位关系与档位定义（等价/部分等价/不等价）：**\n")
    lines.append("- 对位：scenario ←→ 任务指令（instruction）；should ←→ 期望工具调用序列（expected actions，判语义覆盖而非字面一致）；should_not ←→ 政策手册相关条款（裁判自行定位）。")
    lines.append("- **等价(2)**：行为方向 + 关键约束一致（措辞/粒度/抽象层次可不同）。EB 比参考多覆盖合理并列要素（合理超集）也算等价。")
    lines.append("- **部分等价(1)**：方向对，但缺一个要素；或 EB 陷入实现层代码细节偏离业务语言；或关键约束只覆盖一部分。EB\"多识别并列合理业务问题\"不算粒度过细、不据此降档。")
    lines.append("- **不等价(0)**：行为方向相反 / 遗漏关键行为（如漏认证、漏写操作）/ 关键约束与 expected actions 或政策冲突。")
    lines.append("- **整条综合**：三要素全 2 → 等价；有 1 无 0 → 部分等价；任一 0 → 不等价。")
    lines.append("- **严格(含容忍)** = overall==2 或裁判判 tolerant（差异仅属「EB 是参考的合理超集 / 措辞粒度抽象层次不同 / 合理细化未改方向」之一且无任何要素 0）。EB 缺参考关键行为或有要素 0 时不计入严格。\n")

    lines.append("## 三档分布\n")
    lines.append("| 档 | 条数 | 占比 | 平均整条sim |")
    lines.append("|---|---|---|---|")
    for s in (2, 1, 0):
        grp = by_overall.get(s, [])
        cnt = len(grp)
        pct = cnt / nv if nv else 0.0
        avg_sim = _safe_mean([float(r["overall_sim"]) for r in grp])
        lines.append(f"| {_SCORE_LABEL[s]} | {cnt} | {pct*100:.1f}% | {avg_sim:.4f} |")
    lines.append("")

    lines.append("## 三要素一致率\n")
    lines.append("| 要素 | 等价率 | 平均sim |")
    lines.append("|---|---|---|")
    for e in _ELEMENTS:
        st = elem_stats[e]
        lines.append(f"| {e} | {st['eq_rate']*100:.1f}% | {st['avg_sim']:.4f} |")
    lines.append("")

    lines.append("## 分组准确率（严格）\n")
    if by_case:
        lines.append("### 按 case_type\n")
        lines.append("| case_type | 准确率 | 条数 |")
        lines.append("|---|---|---|")
        for ct, grp in sorted(by_case.items()):
            lines.append(f"| {ct} | {_group_rate(grp)*100:.1f}% | {len(grp)} |")
        lines.append("")
    lines.append("### 按 candidate result\n")
    lines.append("| result | 准确率 | 条数 |")
    lines.append("|---|---|---|")
    for res, grp in sorted(by_result.items()):
        lines.append(f"| {res} | {_group_rate(grp)*100:.1f}% | {len(grp)} |")
    lines.append("")

    lines.append("## 不一致 case 清单（overall=0 不等）\n")
    diff = [r for r in valid if r["overall_score"] == 0]
    if not diff:
        lines.append("无不一致 case。\n")
    else:
        for r in diff:
            lines.append(f"### {r['script_id']}（{r['case_type'] or '-'}/{r['result'] or '未知'}）")
            lines.append(f"- 任务指令（instruction）：{r['instruction']}")
            lines.append(f"- 场景：{r['scenario_score']}（sim {r['scenario_sim']}）")
            lines.append(f"- 应该：{r['should_score']}（sim {r['should_sim']}）")
            lines.append(f"- 不应该：{r['should_not_score']}（sim {r['should_not_sim']}）")
            lines.append(f"- 任务参考摘要（expected actions）：{r['expected_actions_text']}")
            lines.append(f"- 待评测 EB：{r['candidate_eb']}")
            lines.append(f"- 裁判理由：{r['reason']}\n")

    lines.append("## 口径说明\n")
    lines.append("- 对位关系与档位定义（等价/部分等价/不等价 + 整条综合规则 + 严格含容忍定义）见上文「准确率」小节。")
    lines.append("- 严格=overall==2 或裁判判 tolerant（超集/粒度差异不降档）；宽松=三要素均部分等价以上。")
    lines.append("- 相似度判定：整条 sim 用「candidate EB vs instruction+expected_actions_text 拼接」的余弦 sim（EB 为业务语言、actions 为具体调用，直接对 sim 解释力弱，故拼接任务前提）；三要素 sim 用裁判拆出的参考侧段。阈值 0.75。")
    lines.append("- 完整明细见 eval_detail CSV（含 tolerant 列）。")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print("完成。")


if __name__ == "__main__":
    main()
