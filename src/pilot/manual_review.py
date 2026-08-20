"""P0-c Part 5 — 수동 검수용 단일 HTML 생성.

무작위 N개 문서에 대해 좌측에 DART 원문 링크, 우측에 추출된 4개 섹션의
첫/마지막 300자를 나란히 놓고, 섹션마다 O/X 체크와 메모를 남길 수 있게 한다.
결과는 JSON 으로 내려받는다 (브라우저 localStorage 에 자동 저장).
"""
from __future__ import annotations

import html as html_lib
import logging
import random
from pathlib import Path
from typing import Any

from src.utils.config import Config

log = logging.getLogger(__name__)

DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e2e2e2;--card:#fafafa;
      --ok:#0a7f3f;--ng:#c0392b;--accent:#1f4e9c;}
@media (prefers-color-scheme:dark){
  :root{--bg:#16181c;--fg:#e8e8e8;--muted:#9aa0a6;--line:#33363d;--card:#1e2126;
        --ok:#39c07a;--ng:#e8705f;--accent:#7aa8ee;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.6 -apple-system,"Segoe UI","Malgun Gothic",sans-serif}
header{position:sticky;top:0;z-index:10;background:var(--bg);
       border-bottom:1px solid var(--line);padding:14px 20px;
       display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:650}
.meta{color:var(--muted);font-size:13px}
button{font:inherit;padding:7px 14px;border:1px solid var(--line);
       border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:transparent}
main{padding:20px;max-width:1400px;margin:0 auto}
.doc{border:1px solid var(--line);border-radius:10px;margin-bottom:22px;
     overflow:hidden;background:var(--card)}
.doc>.head{display:flex;justify-content:space-between;align-items:center;
     gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.doc>.head strong{font-size:15px}
.grid{display:grid;grid-template-columns:220px 1fr;gap:0}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
.left{padding:16px;border-right:1px solid var(--line);font-size:13px}
@media (max-width:860px){.left{border-right:none;border-bottom:1px solid var(--line)}}
.left a{color:var(--accent);word-break:break-all}
.right{padding:8px 16px 16px}
.sec{border-top:1px solid var(--line);padding:12px 0}
.sec:first-child{border-top:none}
.sec .title{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.sec .title b{font-size:14px}
.badge{font-size:11px;padding:2px 7px;border-radius:999px;
       border:1px solid var(--line);color:var(--muted)}
.badge.bad{color:var(--ng);border-color:var(--ng)}
.snip{white-space:pre-wrap;word-break:break-word;font-size:13px;
      background:var(--bg);border:1px solid var(--line);border-radius:6px;
      padding:9px 11px;margin:5px 0;max-height:180px;overflow:auto}
.snip .lbl{color:var(--muted);font-size:11px;display:block;margin-bottom:3px}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
.controls label{display:flex;gap:4px;align-items:center;font-size:13px;cursor:pointer}
.controls input[type=text]{flex:1;min-width:200px;padding:6px 9px;
      border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.ok{color:var(--ok);font-weight:600}
.ng{color:var(--ng);font-weight:600}
#status{font-size:13px;color:var(--muted)}
.warn{font-size:13px;color:var(--ng)}
"""

_JS = """
const KEY = 'p0c_manual_review_v1';
// localStorage 는 환경에 따라 막혀 있을 수 있다(file:// 정책, 시크릿 모드 등).
// 저장 실패가 UI 전체를 멈추면 안 되므로 반드시 감싼다. 저장이 안 되면
// 경고만 띄우고 검수는 계속 가능하게 두되, JSON 다운로드를 반드시 하도록 안내한다.
let persistOk = true;
function load(){ try{ return JSON.parse(localStorage.getItem(KEY)||'{}'); }catch(e){ persistOk=false; return {}; } }
function save(st){
  try{ localStorage.setItem(KEY, JSON.stringify(st)); }
  catch(e){
    if(persistOk){
      persistOk = false;
      const w = document.getElementById('warn');
      if(w) w.textContent = '⚠ 자동 저장 불가 — 끝나면 반드시 JSON 다운로드를 누르세요.';
    }
  }
}
let state = load();

function cellId(doc, sec){ return doc + '|' + sec; }

function restore(){
  document.querySelectorAll('[data-cell]').forEach(el=>{
    const id = el.dataset.cell;
    const rec = state[id];
    if(!rec) return;
    if(rec.verdict){
      const r = el.querySelector('input[value="'+rec.verdict+'"]');
      if(r) r.checked = true;
    }
    if(rec.note){ el.querySelector('input[type=text]').value = rec.note; }
  });
  updateStatus();
}

function updateStatus(){
  const total = document.querySelectorAll('[data-cell]').length;
  const done = Object.values(state).filter(r=>r && r.verdict).length;
  const ng = Object.values(state).filter(r=>r && r.verdict==='X').length;
  document.getElementById('status').textContent =
    done + ' / ' + total + ' 검수 완료 · X 판정 ' + ng + '건';
}

document.addEventListener('change', e=>{
  const cell = e.target.closest('[data-cell]');
  if(!cell) return;
  const id = cell.dataset.cell;
  const rec = state[id] || {};
  rec.corp_name = cell.dataset.corp;
  rec.rcept_no  = cell.dataset.rcept;
  rec.fy        = cell.dataset.fy;
  rec.section   = cell.dataset.section;
  const v = cell.querySelector('input[type=radio]:checked');
  rec.verdict = v ? v.value : null;
  rec.note = cell.querySelector('input[type=text]').value;
  rec.reviewed_at = new Date().toISOString();
  state[id] = rec;
  updateStatus();     // 저장보다 먼저: 저장이 실패해도 화면은 갱신된다
  save(state);
});
document.addEventListener('input', e=>{
  if(e.target.type !== 'text') return;
  const cell = e.target.closest('[data-cell]');
  if(!cell) return;
  const id = cell.dataset.cell;
  const rec = state[id] || {section: cell.dataset.section};
  rec.note = e.target.value;
  rec.corp_name = cell.dataset.corp; rec.rcept_no = cell.dataset.rcept;
  rec.fy = cell.dataset.fy; rec.section = cell.dataset.section;
  state[id] = rec; updateStatus(); save(state);
});

function payload(){
  return JSON.stringify({
    exported_at: new Date().toISOString(),
    n_reviewed: Object.values(state).filter(r=>r && r.verdict).length,
    results: state
  }, null, 2);
}

function download(){
  const blob = new Blob([payload()], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'manual_review_result.json';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
}
function copyJson(){
  navigator.clipboard.writeText(payload()).then(
    ()=>alert('JSON 을 클립보드에 복사했습니다.'),
    ()=>alert('복사 실패. 다운로드 버튼을 사용하세요.'));
}
function resetAll(){
  if(!confirm('검수 내용을 모두 지웁니다. 계속할까요?')) return;
  state = {}; save(state);
  document.querySelectorAll('input[type=radio]').forEach(r=>r.checked=false);
  document.querySelectorAll('input[type=text]').forEach(t=>t.value='');
  updateStatus();
  if(!persistOk){
    const w = document.getElementById('warn');
    if(w) w.textContent = '⚠ 자동 저장 불가 — 끝나면 반드시 JSON 다운로드를 누르세요.';
  }
}
restore();
"""


def _esc(s: str) -> str:
    return html_lib.escape(s or "")


def _snip(text: str, n: int, *, tail: bool = False) -> str:
    if not text:
        return "(비어 있음)"
    if len(text) <= n * 2:
        return text
    return text[-n:] if tail else text[:n]


def build_manual_review(cfg: Config, records: list[dict[str, Any]],
                        out_dir: Path) -> Path:
    """records: p0c 가 만든 [{meta, legacy, fixed}, ...]"""
    mr = cfg.get("manual_review", {}) or {}
    n_docs = int(mr.get("n_docs", 20))
    snip_chars = int(mr.get("snippet_chars", 300))

    rng = random.Random(int(cfg["seed"]))
    pool = list(records)
    rng.shuffle(pool)
    sample = pool[:n_docs]

    parts: list[str] = []
    for rec in sample:
        meta = rec["meta"]
        rcept = str(meta["rcept_no"])
        url = DART_VIEWER.format(rcept_no=rcept)
        doc_key = f"{meta['corp_code']}_{meta['fy']}"

        secs_html: list[str] = []
        for sid, sc in rec["fixed"].items():
            cid = f"{doc_key}|{sid}"
            bad = "" if sc.found else " bad"
            status = "추출됨" if sc.found else "실패/강등"
            secs_html.append(f"""
      <div class="sec" data-cell="{_esc(cid)}" data-corp="{_esc(str(meta['corp_name']))}"
           data-rcept="{_esc(rcept)}" data-fy="{_esc(str(meta['fy']))}"
           data-section="{_esc(sid)}">
        <div class="title">
          <b>{_esc(sid)} · {_esc(sc.name)}</b>
          <span class="badge{bad}">{status}</span>
          <span class="badge">{sc.char_len_text:,}자 / {sc.n_paragraphs}문단</span>
          <span class="badge">종료: {_esc(sc.end_header or sc.end_reason)}</span>
        </div>
        <div class="snip"><span class="lbl">첫 {snip_chars}자</span>{
            _esc(_snip(sc.text, snip_chars))}</div>
        <div class="snip"><span class="lbl">마지막 {snip_chars}자</span>{
            _esc(_snip(sc.text, snip_chars, tail=True))}</div>
        <div class="controls">
          <label><input type="radio" name="v_{_esc(cid)}" value="O">
            <span class="ok">O 정상</span></label>
          <label><input type="radio" name="v_{_esc(cid)}" value="X">
            <span class="ng">X 오류</span></label>
          <input type="text" placeholder="메모 (예: 재무제표 주석 유입, 표 셀 혼입)">
        </div>
      </div>""")

        parts.append(f"""
  <section class="doc">
    <div class="head">
      <strong>{_esc(str(meta['corp_name']))} · {_esc(str(meta['fy']))}년</strong>
      <span class="meta">{_esc(str(meta['stock_code']))} · {_esc(rcept)}</span>
    </div>
    <div class="grid">
      <div class="left">
        <p><a href="{_esc(url)}" target="_blank" rel="noopener">DART 원문 열기 ↗</a></p>
        <p class="meta">고유번호 {_esc(str(meta['corp_code']))}<br>
           접수번호 {_esc(rcept)}<br>
           시장 {_esc(str(meta.get('market', '')))}</p>
        <p class="meta">좌측 원문과 우측 추출 결과를 대조해 섹션마다 O/X 를 남기세요.</p>
      </div>
      <div class="right">{''.join(secs_html)}
      </div>
    </div>
  </section>""")

    doc = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>P0-c 수동 검수 — 섹션 추출 결과</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>P0-c 수동 검수 — 섹션 추출 결과</h1>
  <span class="meta">무작위 {len(sample)}개 문서 · seed {cfg['seed']}</span>
  <span id="status"></span>
  <span id="warn" class="warn"></span>
  <span style="flex:1"></span>
  <button class="primary" onclick="download()">JSON 다운로드</button>
  <button onclick="copyJson()">JSON 복사</button>
  <button onclick="resetAll()">초기화</button>
</header>
<main>{''.join(parts)}</main>
<script>{_JS}</script>
</body></html>"""

    path = out_dir / "manual_review.html"
    path.write_text(doc, encoding="utf-8")
    log.info("수동 검수 HTML -> %s (%d개 문서)", path, len(sample))
    return path
