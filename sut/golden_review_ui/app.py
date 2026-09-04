#!/usr/bin/env python3
"""golden_review_ui — golden 审阅/修改本地网页(零依赖, 标准库 http.server)

读 runs/<RUN>/golden/golden_output.jsonl(原始, 不动) + runs/<RUN>/md/trace_<id>.md,
人工审阅后写 runs/<RUN>/golden/golden_review.jsonl(同字段名 expected_behavior/result/reason,
按 script_id 对齐, status=accepted|edited)。原始与人工两份都保留。

布局: 左上=trace MD(只读) | 左下=原始 golden(只读) | 右=编辑区+按钮+导航
统计: agent 维度(按最终人工 result 计 通过/部分通过/失败) + golden 维度(LLM 原始
       expected_behavior 被接受不改的比例)。

用法:
    python app.py                         # 最新 run(含 golden_output.jsonl)
    python app.py --run-id 20260708_151648
    python app.py --port 8765
然后浏览器开 http://127.0.0.1:8765  (自动弹)
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

BUNDLE = Path(__file__).resolve().parent.parent
RESULTS_VOCAB = ["通过", "部分通过", "失败"]


# ── 数据加载 ────────────────────────────────────────────────
def latest_run() -> Path | None:
    runs = BUNDLE / "runs"
    if not runs.exists():
        return None
    cands = []
    for p in runs.iterdir():
        if p.is_dir() and (p / "golden" / "golden_output.jsonl").exists():
            cands.append(p)
    return sorted(cands, key=lambda p: p.name)[-1] if cands else None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


def save_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


def md_path(run_dir: Path, sid: str) -> Path:
    return run_dir / "md" / f"trace_{sid}.md"


def list_runs() -> list[dict]:
    """扫描 runs/ 下所有 run, 返回 name/path/记录数/是否有golden。"""
    runs = BUNDLE / "runs"
    out = []
    if runs.exists():
        for p in sorted(runs.iterdir(), key=lambda x: x.name, reverse=True):
            if not p.is_dir():
                continue
            gf = p / "golden" / "golden_output.jsonl"
            n = len(load_jsonl(gf)) if gf.exists() else 0
            out.append({"name": p.name, "path": str(p), "n": n, "has_golden": gf.exists()})
    return out


def set_ctx(run_dir: Path) -> bool:
    """加载某 run 为当前审阅目标; 返回是否成功。"""
    global CTX
    if run_dir.exists() and (run_dir / "golden" / "golden_output.jsonl").exists():
        CTX = Ctx(run_dir)
        return True
    return False


def render_landing(msg: str = "") -> str:
    runs = list_runs()
    rows = "".join(
        f'<tr><td><a href="/load?path={quote(r["path"])}"><b>{r["name"]}</b></a></td>'
        f'<td>{"✓ golden" if r["has_golden"] else "— 无"}</td>'
        f'<td>{r["n"]} 条</td>'
        f'<td><code>{html.escape(r["path"])}</code></td></tr>'
        for r in runs) or '<tr><td colspan=4 class="muted">runs/ 下暂无 run</td></tr>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>golden 审阅 - 选 run</title>
<style>body{{font-family:system-ui;padding:24px;max-width:900px;margin:auto}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:8px;text-align:left}}
code{{font-size:12px;color:#666}} .muted{{color:#999}} .msg{{padding:8px;background:#fcf8e3;border:1px solid #eee;margin:8px 0}}</style></head>
<body>
<h2>golden 审阅 — 选择 run</h2>
{f'<div class="msg">{html.escape(msg)}</div>' if msg else ''}
<h3>从 runs/ 选</h3>
<table><tr><th>run_id</th><th>golden</th><th>记录数</th><th>路径</th></tr>{rows}</table>
<h3>或粘贴 run 目录路径导入</h3>
<form method="get" action="/load">
  <input type="text" name="path" size="70" placeholder="D:\\bank_zhidaitong0708\\intranet_bundle\\runs\\20260708_151648">
  <button type="submit">加载</button>
</form>
<p class="muted">导入任意 run 目录(须含 golden/golden_output.jsonl)。也可从别的位置加载, 不限 runs/ 下。</p>
</body></html>"""


# ── 全局状态(每次请求读盘, 避免脏数据) ─────────────────────
class Ctx:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.original_path = run_dir / "golden" / "golden_output.jsonl"
        self.review_path = run_dir / "golden" / "golden_review.jsonl"
        self.reload()

    def reload(self):
        self.original = load_jsonl(self.original_path)
        reviews = load_jsonl(self.review_path)
        self.review_by_sid = {r.get("script_id"): r for r in reviews}
        self.order = [r.get("script_id", f"#{i}") for i, r in enumerate(self.original)]
        self.by_sid = {r.get("script_id"): r for r in self.original}

    def write_review(self, rec: dict):
        reviews = load_jsonl(self.review_path)
        reviews = [r for r in reviews if r.get("script_id") != rec["script_id"]]
        reviews.append(rec)
        save_jsonl(self.review_path, reviews)
        self.reload()


CTX: Ctx | None = None


# ── HTML ────────────────────────────────────────────────────
def render_page(sid: str) -> str:
    CTX.reload()
    if sid not in CTX.by_sid:
        return f"<h3>未知 script_id: {html.escape(sid)}</h3>"
    idx = CTX.order.index(sid)
    orig = CTX.by_sid[sid]
    rev = CTX.review_by_sid.get(sid)
    status = rev.get("status") if rev else "pending"
    # 预填: 有 review 用 review, 否则用原始(方便从原始改起)
    cur = rev if rev else orig

    prev_sid = CTX.order[idx - 1] if idx > 0 else None
    next_sid = CTX.order[idx + 1] if idx < len(CTX.order) - 1 else None

    md_file = md_path(CTX.run_dir, sid)
    md_text = md_file.read_text(encoding="utf-8") if md_file.exists() else "(无 trace MD)"

    reviewed = len(CTX.review_by_sid)
    total = len(CTX.order)

    options = "".join(
        f'<option value="{s}"{" selected" if s == sid else ""}>{s}</option>'
        for s in CTX.order)
    result_options = "".join(
        f'<option value="{v}"{" selected" if cur.get("result") == v else ""}>{v}</option>'
        for v in RESULTS_VOCAB)

    # 评估器打分(参考), 嵌套在 golden 记录的 score 字段; 动态展示所有字段(自适应维度改名)
    sc = orig.get("score") or (cur.get("score") if isinstance(cur.get("score"), dict) else None)
    if isinstance(sc, dict) and sc:
        sc_lines = "\n".join(f"{k}={esc(v)}" for k, v in sc.items())
        score_block = ('<h4>评估器打分(参考)</h4>'
                       f'<pre>{sc_lines}</pre>')
    else:
        score_block = '<h4>评估器打分(参考)</h4><pre class="muted">(无, 未跑 score 阶段)</pre>'

    def esc(x): return html.escape(str(x or ""))

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>golden 审阅 - {esc(sid)}</title>
<style>
  html,body{{margin:0;height:100%;font-family:system-ui,sans-serif;font-size:14px}}
  .grid{{display:grid;height:100vh;gap:6px;
    grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;
    grid-template-areas:"lt rt" "lb rt"}}
  .lt{{grid-area:lt;overflow:auto;background:#fafafa;padding:8px}}
  .lb{{grid-area:lb;overflow:auto;background:#f0f0f0;padding:8px}}
  .rt{{grid-area:rt;overflow:auto;padding:8px}}
  pre{{white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0}}
  h4{margin:4px 0;color:#333}.muted{color:#888}
  textarea{{width:100%;box-sizing:border-box;font-family:inherit;font-size:13px}}
  select{{font-size:14px;padding:2px}}
  .btn{{font-size:15px;padding:6px 14px;margin-right:6px;cursor:pointer}}
  .accept{{background:#4caf50;color:#fff;border:none}}
  .save{{background:#2196f3;color:#fff;border:none}}
  .nav{{background:#eee;border:1px solid #ccc}}
  .status{{display:inline-block;padding:2px 8px;border-radius:3px;font-size:12px}}
  .st-accepted{{background:#dff0d8;color:#3c763d}}
  .st-edited{{background:#fcf8e3;color:#8a6d3b}}
  .st-pending{{background:#ddd;color:#555}}
  .hdr{{position:sticky;top:0;background:#fff;padding:4px 0;border-bottom:1px solid #eee}}
</style></head><body>
<div class="grid">
  <div class="lt">
    <div class="hdr"><b>trace MD</b> — {esc(sid)}
      <span class="muted">({idx+1}/{total}, 已审 {reviewed})</span></div>
    <pre>{esc(md_text)}</pre>
  </div>
  <div class="lb">
    <div class="hdr"><b>原始 golden (LLM, 只读)</b></div>
    <h4>scenario</h4><pre>{esc(orig.get('scenario'))}</pre>
    <h4>expected_behavior</h4><pre>{esc(orig.get('expected_behavior'))}</pre>
    <h4>result / reason</h4>
    <pre>{esc(orig.get('result'))}  —  {esc(orig.get('reason'))}</pre>
    {score_block}
  </div>
  <div class="rt">
    <div class="hdr">
      <b>人工编辑</b>
      <span class="status st-{status}">{status}</span>
    </div>
    <form method="post" action="/save">
      <input type="hidden" name="sid" value="{esc(sid)}">
      <p><label><b>expected_behavior</b></label>
        <textarea name="expected_behavior" rows="10">{esc(cur.get('expected_behavior'))}</textarea></p>
      <p><label><b>result</b></label>
        <select name="result">
        {result_options}
        </select></p>
      <p><label><b>reason</b></label>
        <textarea name="reason" rows="4">{esc(cur.get('reason'))}</textarea></p>
      <p>
        <button class="btn accept" name="action" value="accept">✓ 接受(用原始, 下一条)</button>
        <button class="btn save" name="action" value="save">💾 保存修改(下一条)</button>
      </p>
    </form>
    <hr>
    <p>
      <a class="btn nav" href="/review?sid={esc(prev_sid) if prev_sid else esc(sid)}">{'← 上一条' if prev_sid else '(已是第一条)'}</a>
      <a class="btn nav" href="/review?sid={esc(next_sid) if next_sid else esc(sid)}">{'下一条 →' if next_sid else '(已是最后一条)'}</a>
    </p>
    <p>按 id 跳转:
      <select onchange="location.href='/review?sid='+this.value">{options}</select>
    </p>
    <p><a href="/stats">📊 查看统计</a>  |  <a href="/">回到首条待审</a>  |  <a href="/landing">切换 run</a></p>
  </div>
</div>
</body></html>"""


def render_stats() -> str:
    CTX.reload()
    total = len(CTX.order)
    reviewed = 0
    agent_cnt = {v: 0 for v in RESULTS_VOCAB}
    eb_unchanged = 0  # golden 维度: expected_behavior 被接受不改
    result_unchanged = 0
    for sid in CTX.order:
        orig = CTX.by_sid.get(sid, {})
        rev = CTX.review_by_sid.get(sid)
        if not rev:
            continue
        reviewed += 1
        # 最终 result(人工)
        fr = rev.get("result") or orig.get("result")
        if fr in agent_cnt:
            agent_cnt[fr] += 1
        # expected_behavior 是否被改
        if (rev.get("expected_behavior") or "") == (orig.get("expected_behavior") or ""):
            eb_unchanged += 1
        if (rev.get("result") or "") == (orig.get("result") or ""):
            result_unchanged += 1
    pending = total - reviewed
    eb_rate = (eb_unchanged / reviewed * 100) if reviewed else 0
    res_rate = (result_unchanged / reviewed * 100) if reviewed else 0

    rows = "".join(
        f"<tr><td>{v}</td><td>{agent_cnt[v]}</td>"
        f"<td>{agent_cnt[v]/reviewed*100:.1f}%</td></tr>" if reviewed else
        f"<tr><td>{v}</td><td>0</td><td>-</td></tr>"
        for v in RESULTS_VOCAB)

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>golden 审阅统计</title>
<style>body{{font-family:system-ui;padding:20px}} table{{border-collapse:collapse}} td,th{{border:1px solid #ccc;padding:6px 12px}}</style></head>
<body><h2>审阅统计</h2>
<p>总 {total} 条 | 已审 {reviewed} | 待审 {pending}</p>
<h3>① agent 维度(按最终人工 result)</h3>
<table><tr><th>结果</th><th>条数</th><th>占比</th></tr>{rows}</table>
<h3>② golden 维度(LLM 原始被接受不改的比例)</h3>
<p>expected_behavior 未改: <b>{eb_unchanged}/{reviewed} = {eb_rate:.1f}%</b></p>
<p>result 未改: <b>{result_unchanged}/{reviewed} = {res_rate:.1f}%</b></p>
<p><a href="/">回到审阅</a></p>
</body></html>"""


# ── HTTP ────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, body: str, code: int = 200):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, loc: str):
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(p.query).items()}
        if p.path in ("/", ""):
            if CTX is None:
                self._send(render_landing()); return
            target = next((s for s in CTX.order if s not in CTX.review_by_sid), CTX.order[0] if CTX.order else None)
            if target:
                self._redirect(f"/review?sid={target}")
            else:
                self._send("<h3>无 golden 数据</h3>")
            return
        if p.path == "/landing":
            self._send(render_landing()); return
        if p.path == "/load":
            path = q.get("path")
            if path:
                rd = Path(path)
                if set_ctx(rd):
                    print(f"已加载 run: {rd}")
                    self._redirect("/")
                    return
                else:
                    self._send(render_landing(f"路径无效或不含 golden/golden_output.jsonl: {path}"))
                    return
            self._send(render_landing("请提供 path 参数")); return
        if p.path == "/stats":
            if CTX is None:
                self._redirect("/landing"); return
            self._send(render_stats()); return
        if p.path == "/review":
            if CTX is None:
                self._redirect("/landing"); return
            sid = q.get("sid")
            if sid and sid in CTX.by_sid:
                self._send(render_page(sid))
            else:
                self._send("<h3>缺少或未知 sid</h3>")
            return
        self._send(f"<h3>404: {html.escape(p.path)}</h3>", 404)

    def do_POST(self):
        p = urlparse(self.path)
        if CTX is None:
            self._redirect("/landing"); return
        if p.path != "/save":
            self._send("<h3>404</h3>", 404); return
        length = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(
            self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}
        sid = form.get("sid")
        action = form.get("action")
        if sid not in CTX.by_sid:
            self._send(f"<h3>未知 sid: {html.escape(sid or '')}</h3>"); return
        orig = CTX.by_sid[sid]
        if action == "accept":
            rec = {
                "script_id": sid, "status": "accepted",
                "expected_behavior": orig.get("expected_behavior", ""),
                "result": orig.get("result", ""),
                "reason": orig.get("reason", ""),
            }
        else:  # save
            rec = {
                "script_id": sid, "status": "edited",
                "expected_behavior": form.get("expected_behavior", ""),
                "result": form.get("result", ""),
                "reason": form.get("reason", ""),
            }
        CTX.write_review(rec)
        # 下一条
        idx = CTX.order.index(sid)
        nxt = CTX.order[idx + 1] if idx + 1 < len(CTX.order) else sid
        if nxt == sid:
            self._redirect("/stats")
        else:
            self._redirect(f"/review?sid={nxt}")


def main() -> int:
    global CTX
    ap = argparse.ArgumentParser(description="golden 审阅/修改本地网页")
    ap.add_argument("--run-id", default=None, help="预加载 runs/<run-id>(可选, 不传则开界面选)")
    ap.add_argument("--run-dir", default=None, help="预加载任意 run 目录路径(可选)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # 预加载(可选): --run-dir 优先, 再 --run-id; 没有就开界面让用户选
    run_dir = None
    if args.run_dir:
        run_dir = Path(args.run_dir)
    elif args.run_id:
        run_dir = BUNDLE / "runs" / args.run_id
    if run_dir and set_ctx(run_dir):
        print(f"已预加载 run: {CTX.run_dir}  ({len(CTX.original)} 条)")
    else:
        if run_dir:
            print(f"⚠ 指定的 run 无 golden_output.jsonl: {run_dir}, 改为开界面选")
        print("未预加载 run, 开界面选择/导入")

    url = f"http://127.0.0.1:{args.port}"
    print(f"开: {url}  (Ctrl+C 退出)")
    if not args.no_browser:
        webbrowser.open(url)
    HTTPServer(("127.0.0.1", args.port), H).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
