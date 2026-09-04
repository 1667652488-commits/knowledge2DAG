#!/usr/bin/env python3
"""
run_pipeline.py — 单次 run 一个目录的薄驱动 (不改任何 skill 代码)。

把各阶段产出都收敛到 runs/<RUN_ID>/ 下, 解决"轨迹在 runs/agent、
golden 在 data/golden、规则在 runs/coldstart 各自散落"的问题。
每个阶段仍调 run.py <命令>, 只是把 --output/--output-dir/--trace-dir 等
都指到同一个 run 目录。

布局:
    runs/<RUN_ID>/
    ├── run.meta                     # 本次 run 元信息
    ├── scripts.json                 # 本次剧本(自包含拷贝)
    ├── agent/<ts>/traces/trace_*.json + batch_result.csv
    ├── golden/{global_understanding.txt, golden_output.jsonl, intermediate/}
    ├── md/trace_<id>.md             # trace 转可读 markdown (trace_md, 有 golden 则注入判定)
    ├── coldstart/{skill}/... + merged/final_rules.json + 双版本NL规则
    ├── rules/rules.py               # (可选, 当前不用)
    └── (validate/curation/localize 按需扩展)

用法:
    # ① 默认全链路到 trace_md (agent→refetch→golden→trace_md; refetch 自动回捞超时失败 trace)
    python run_pipeline.py
    python run_pipeline.py --ids BC13             # 冒烟某几条
    # ② 接着挖规则 (复用已有 run, 继续往同一目录写)
    python run_pipeline.py --run-id 20260708_153012 --stages coldstart
    # ③ 全量 (当前=agent,refetch,golden,trace_md,coldstart; rules/validate 不含)
    python run_pipeline.py --stages all
    # 单阶段也可任意组合
    python run_pipeline.py --stages agent
    python run_pipeline.py --run-id <RUN> --stages golden,trace_md,coldstart
    # 调 refetch 等待时长(默认 poll=15s, 每条 max=180s)
    python run_pipeline.py --refetch-poll 30 --refetch-max-wait 600

RUN_ID 不传则用时间戳。golden 默认 --regenerate-global (每次重生成 GU,
per-run 独立, 不跨 run 污染); 加 --no-regenerate-global 复用本 run 已有 GU。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent

# 当前在用的阶段; rules/validate 暂不用, 但保留可选
ALL_STAGES = ["agent", "refetch", "score", "golden", "trace_md", "coldstart"]
DEFAULT_STAGES = ["agent", "refetch", "score", "golden", "trace_md"]
SELECTABLE = ["agent", "refetch", "score", "golden", "trace_md", "coldstart", "rules", "validate"]


def _run(cmd: list[str]) -> bool:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=BUNDLE)
    return r.returncode == 0


def find_trace_dir(run_dir: Path) -> Path | None:
    """agent 产出在 runs/<RUN>/agent/<ts>/traces, 探出含 trace_*.json 的那个。"""
    cand = [c for c in (run_dir / "agent").glob("*/traces")
            if c.is_dir() and any(c.glob("trace_*.json"))]
    if not cand:
        return None
    return cand[-1]  # 多个 <ts> 取最新


def stage_agent(ctx) -> bool:
    out = ctx.run_dir / "agent"
    cmd = [sys.executable, "run.py", "agent", "--output-dir", str(out)]
    if ctx.scripts:
        cmd += ["--scripts", str(ctx.scripts)]
    # else: agent_runner 用自身默认 --scripts (data/scripts/badcase_scripts.json)
    if ctx.ids:
        cmd += ["--ids", ctx.ids]
    if not _run(cmd):
        return False
    td = find_trace_dir(ctx.run_dir)
    if not td:
        print("✗ agent 跑完但没找到 trace 目录")
        return False
    ctx.trace_dir = td
    print(f"  → trace_dir = {td}")
    # 自包含: 拷贝剧本进 run 目录(显式传了 --scripts 才拷)
    if ctx.scripts:
        try:
            shutil.copyfile(ctx.scripts, ctx.run_dir / "scripts.json")
        except Exception as e:
            print(f"  (剧本拷贝失败, 忽略: {e})")
    return True


def stage_refetch(ctx) -> bool:
    """agent 跑完后回捞失败/空 trace: 按 conversation_id 轮询 cleaned-traces,
    服务器跑完的会落 trace, 回填到 trace 文件。放 agent 之后、golden 之前,
    这样 golden 能看到回捞后的完整 trace。回捞 0 条不算阶段失败。"""
    td = ctx.trace_dir or find_trace_dir(ctx.run_dir)
    if not td:
        print("ℹ refetch: 无 trace 目录(agent 未跑或无产出), 跳过")
        return True
    from common import agent_client
    tfs = sorted(td.glob("trace_*.json"))
    failed = []
    for tf in tfs:
        d = json.loads(tf.read_text(encoding="utf-8"))
        errs = d.get("errors") or []
        msgs = d.get("messages") or []
        if errs or not msgs:
            failed.append((tf, d))
    print(f"  trace {len(tfs)} | 失败/空 {len(failed)} | 回捞中 (poll={ctx.refetch_poll}s max={ctx.refetch_max_wait}s/条)")
    if not failed:
        return True
    recovered = 0
    for tf, d in failed:
        sid = d.get("script_id", "?")
        cid = d.get("conversation_id")
        if not cid:
            print(f"  {sid}: 无 conversation_id, 跳过"); continue
        msgs: list = []
        t0 = time.time()
        while True:
            cl = agent_client.get_cleaned_traces(cid)
            msgs = (cl or {}).get("messages", []) or []
            if msgs or not ctx.refetch_poll or not ctx.refetch_max_wait or time.time() - t0 > ctx.refetch_max_wait:
                break
            time.sleep(ctx.refetch_poll)
        if msgs:
            d["messages"] = msgs
            d["errors"] = []
            tf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  ✓ {sid} ({cid[:8]}): 回捞 {len(msgs)} 条")
            recovered += 1
        else:
            print(f"  ✗ {sid} ({cid[:8]}): 仍无 trace")
    print(f"  refetch 汇总: 回捞 {recovered}/{len(failed)}")
    ctx.trace_dir = td
    return True


def stage_score(ctx) -> bool:
    """用评估器基础提示词对每条 trace 打分(独立于 golden), 产出 scores.jsonl。
    放 refetch 之后、golden 之前; golden 会自动读 scores 作参考注入。"""
    td = ctx.trace_dir or find_trace_dir(ctx.run_dir)
    if not td:
        print("✗ score 需要 trace 目录(先跑 agent 或用已有 run)")
        return False
    scores_path = ctx.run_dir / "scores" / "scores.jsonl"
    cmd = [sys.executable, "run.py", "score",
           "--trace-dir", str(td),
           "--output", str(scores_path),
           "--skill-dir", "data/skills_flat"]
    ctx.trace_dir = td
    return _run(cmd)


def stage_golden(ctx) -> bool:
    td = ctx.trace_dir or find_trace_dir(ctx.run_dir)
    if not td:
        print("✗ golden 需要 trace 目录(先跑 agent 或用已有 run)")
        return False
    gdir = ctx.run_dir / "golden"
    gdir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "run.py", "golden",
           "--trace-dir", str(td),
           "--output", str(gdir / "golden_output.jsonl"),
           "--global-understanding", str(gdir / "global_understanding.txt"),
           "--intermediate-dir", str(gdir / "intermediate"),
           "--batch-size", str(ctx.batch_size)]
    if ctx.regenerate_global:
        cmd.append("--regenerate-global")
    # 若 score 阶段产出了 scores.jsonl, 自动注入 golden 作参考
    scores_path = ctx.run_dir / "scores" / "scores.jsonl"
    if scores_path.exists():
        cmd += ["--scores", str(scores_path)]
    ok = _run(cmd)
    if ok:
        ctx.trace_dir = td
    return ok


def stage_trace_md(ctx) -> bool:
    """trace → markdown (调 skills/trace_md)。有 golden 就注入判定, 没有就纯转 md。"""
    td = ctx.trace_dir or find_trace_dir(ctx.run_dir)
    if not td:
        print("✗ trace_md 需要 trace 目录(先跑 agent 或用已有 run)")
        return False
    md_dir = ctx.run_dir / "md"
    golden = ctx.run_dir / "golden" / "golden_output.jsonl"
    # trace_md 对 golden 不存在宽容(load_golden_map 返回 {}), 故始终传
    cmd = [sys.executable, "run.py", "trace_md",
           "--trace-dir", str(td),
           "--output-dir", str(md_dir),
           "--golden", str(golden)]
    ctx.trace_dir = td
    return _run(cmd)


def stage_coldstart(ctx) -> bool:
    td = ctx.trace_dir or find_trace_dir(ctx.run_dir)
    if not td:
        print("✗ coldstart 需要 trace 目录(先跑 agent 或用已有 run)")
        return False
    golden = ctx.run_dir / "golden" / "golden_output.jsonl"
    if not golden.exists():
        print("✗ coldstart 需要 golden_output.jsonl(先跑 golden)")
        return False
    ctx.trace_dir = td
    return _run([sys.executable, "run.py", "coldstart",
                 "--trace-dir", str(td),
                 "--golden", str(golden),
                 "--skills", "data/skills_flat",
                 "--output", str(ctx.run_dir / "coldstart")])


def stage_rules(ctx) -> bool:
    td = ctx.trace_dir or find_trace_dir(ctx.run_dir)
    fr = ctx.run_dir / "coldstart" / "merged" / "final_rules.json"
    if not fr.exists():
        print("✗ rules 需要 final_rules.json(先跑 coldstart)")
        return False
    return _run([sys.executable, "run.py", "rules",
                 "--rules", str(fr),
                 "--output", str(ctx.run_dir / "rules"),
                 "--traces", str(td)])


def stage_validate(ctx) -> bool:
    td = ctx.trace_dir or find_trace_dir(ctx.run_dir)
    return _run([sys.executable, "run.py", "validate",
                 "--rules-dir", str(ctx.run_dir / "rules"),
                 "--trace-dir", str(td),
                 "--golden", str(ctx.run_dir / "golden" / "golden_output.jsonl")])


STAGE_FNS = {
    "agent": stage_agent,
    "refetch": stage_refetch,
    "score": stage_score,
    "golden": stage_golden,
    "trace_md": stage_trace_md,
    "coldstart": stage_coldstart,
    "rules": stage_rules,
    "validate": stage_validate,
}


class Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def main() -> int:
    ap = argparse.ArgumentParser(description="单次 run 一个目录的薄驱动")
    ap.add_argument("--run-id", default=None, help="不传则用时间戳")
    ap.add_argument("--scripts", default=None, help="剧本路径(agent 阶段必填)")
    ap.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                    help=f"逗号分隔; all={ALL_STAGES}; 默认={DEFAULT_STAGES}")
    ap.add_argument("--ids", default=None, help="只跑指定剧本 id(逗号分隔, agent 用)")
    ap.add_argument("--no-regenerate-global", action="store_true",
                    help="复用本 run 已有 global_understanding, 不重生成")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--refetch-poll", type=int, default=15, help="refetch 轮询间隔秒")
    ap.add_argument("--refetch-max-wait", type=int, default=180,
                    help="refetch 每条失败 trace 最长等待秒(等服务器跑完)")
    ap.add_argument("--no-score", action="store_true",
                    help="跳过 score 阶段(评估器打分), golden 不带 scores 参考")
    args = ap.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = BUNDLE / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    stages = ALL_STAGES if args.stages == "all" else args.stages.split(",")
    stages = [s.strip() for s in stages if s.strip()]
    if args.no_score:
        stages = [s for s in stages if s != "score"]
    unknown = [s for s in stages if s not in STAGE_FNS]
    if unknown:
        print(f"✗ 未知阶段: {unknown}; 可选: {SELECTABLE}")
        return 1

    ctx = Ctx(run_id=run_id, run_dir=run_dir, scripts=args.scripts,
              ids=args.ids, batch_size=args.batch_size,
              regenerate_global=not args.no_regenerate_global,
              refetch_poll=args.refetch_poll, refetch_max_wait=args.refetch_max_wait,
              trace_dir=None)

    # 元信息 (记录本次 run 用的端点/模型, 便于审计)
    meta = {
        "run_id": run_id, "scripts": args.scripts, "ids": args.ids,
        "stages": stages,
        "regenerate_global": ctx.regenerate_global,
        "start": datetime.now().isoformat(timespec="seconds"),
        "results": {},
    }
    try:
        sys.path.insert(0, str(BUNDLE))
        from common.config import get_config  # noqa: E402
        c = get_config()
        meta["llm"] = {"mode": c.llm.mode, "model": c.llm.model,
                       "base_url": c.llm.base_url}
        meta["agent"] = {"base_url": c.agent.base_url,
                         "agent_name": c.agent.agent_name}
    except Exception as e:
        meta["config_err"] = str(e)

    print("=" * 70)
    print(f"  RUN_ID = {run_id}")
    print(f"  目录   = {run_dir}")
    print(f"  阶段   = {stages}")
    if args.scripts:
        print(f"  剧本   = {args.scripts}" + (f"  ids={args.ids}" if args.ids else ""))
    print("=" * 70)

    for s in stages:
        print(f"\n{'─' * 70}\n  阶段: {s}\n{'─' * 70}")
        ok = STAGE_FNS[s](ctx)
        meta["results"][s] = "PASS" if ok else "FAIL"
        if not ok:
            print(f"\n✗ 阶段 {s} 失败, 中断后续阶段")
            break

    meta["end"] = datetime.now().isoformat(timespec="seconds")
    if ctx.trace_dir:
        meta["trace_dir"] = str(ctx.trace_dir)
    (run_dir / "run.meta").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'=' * 70}")
    print(f"  完成。run.meta → {run_dir / 'run.meta'}")
    for s, r in meta["results"].items():
        print(f"    {s:<10} {r}")
    print(f"  目录: {run_dir}")
    print("=" * 70)
    return 0 if all(v == "PASS" for v in meta["results"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
