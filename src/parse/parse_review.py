"""Phase 2 무작위 20건 수동 검수 HTML.

성공률 지표만으로 통과 판정하지 않기 위한 장치다. Phase 0 에서
found_rate=1.0 이면서 실제로는 결함이 있었던 전례가 있다.
P0-c 의 검수 UI 를 그대로 재사용하되, 파싱 산출물(JSON)에서 읽는다.
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path

import pandas as pd

from src.parse.run_parse import _paths
from src.pilot.manual_review import _CSS, _JS, _esc, _snip
from src.parse.sections import SectionContent
from src.utils.config import Config

log = logging.getLogger(__name__)

DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def build_review(cfg: Config, m: pd.DataFrame, out_dir: Path,
                 n_docs: int = 20, snip: int = 300) -> Path:
    dirs = _paths(cfg)
    rng = random.Random(int(cfg["seed"]))
    pool = m.dropna(subset=["corp_name"]).to_dict("records")
    rng.shuffle(pool)
    sample = pool[:n_docs]

    parts = []
    for rec in sample:
        rcept = str(rec["rcept_no"])
        sec_file = dirs["sections"] / f"{rcept}.json"
        tbl_file = dirs["tables"] / f"{rcept}.json"
        if not sec_file.exists():
            continue
        secs = json.loads(sec_file.read_text(encoding="utf-8"))
        tabs = json.loads(tbl_file.read_text(encoding="utf-8")) if tbl_file.exists() else {}
        doc_key = f"{rec.get('corp_code','')}_{int(rec['fy'])}"
        blocks = []
        for sid, sc in secs.items():
            cid = f"{doc_key}|{sid}"
            t = tabs.get(sid, [])
            n_layout = sum(1 for x in t if x.get("is_layout"))
            bad = "" if sc.get("found") else " bad"
            status = "추출됨" if sc.get("found") else "실패/강등"
            text = sc.get("text", "")
            blocks.append(f"""
      <div class="sec" data-cell="{_esc(cid)}" data-corp="{_esc(str(rec['corp_name']))}"
           data-rcept="{_esc(rcept)}" data-fy="{_esc(str(int(rec['fy'])))}"
           data-section="{_esc(sid)}">
        <div class="title">
          <b>{_esc(sid)} · {_esc(sc.get('name',''))}</b>
          <span class="badge{bad}">{status}</span>
          <span class="badge">{len(text):,}자 / {len(sc.get('paragraphs',[])):,}문단</span>
          <span class="badge">표 {len(t)}개 (레이아웃 {n_layout})</span>
          <span class="badge">종료: {_esc(sc.get('end_header') or sc.get('end_reason',''))}</span>
        </div>
        <div class="snip"><span class="lbl">첫 {snip}자</span>{_esc(_snip(text, snip))}</div>
        <div class="snip"><span class="lbl">마지막 {snip}자</span>{
            _esc(_snip(text, snip, tail=True))}</div>
        <div class="controls">
          <label><input type="radio" name="v_{_esc(cid)}" value="O">
            <span class="ok">O 정상</span></label>
          <label><input type="radio" name="v_{_esc(cid)}" value="X">
            <span class="ng">X 오류</span></label>
          <input type="text" placeholder="메모 (예: 표 유입, 캡션 고아, 경계 오류)">
        </div>
      </div>""")

        parts.append(f"""
  <section class="doc">
    <div class="head">
      <strong>{_esc(str(rec['corp_name']))} · {int(rec['fy'])}년</strong>
      <span class="meta">{_esc(rcept)}</span>
    </div>
    <div class="grid">
      <div class="left">
        <p><a href="{DART_VIEWER.format(rcept_no=rcept)}" target="_blank"
              rel="noopener">DART 원문 열기 ↗</a></p>
        <p class="meta">시장 {_esc(str(rec.get('market','')))}</p>
        <p class="meta">좌측 원문과 우측 추출 결과를 대조해 섹션마다 O/X 를 남기세요.</p>
      </div>
      <div class="right">{''.join(blocks)}
      </div>
    </div>
  </section>""")

    doc = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phase 2 수동 검수 — 전량 파싱 결과</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>Phase 2 수동 검수 — 전량 파싱 결과</h1>
  <span class="meta">무작위 {len(parts)}개 문서 · seed {cfg['seed']}</span>
  <span id="status"></span><span id="warn" class="warn"></span>
  <span style="flex:1"></span>
  <button class="primary" onclick="download()">JSON 다운로드</button>
  <button onclick="copyJson()">JSON 복사</button>
  <button onclick="resetAll()">초기화</button>
</header>
<main>{''.join(parts)}</main>
<script>{_JS}</script>
</body></html>"""

    path = out_dir / "parse_manual_review.html"
    path.write_text(doc, encoding="utf-8")
    log.info("수동 검수 HTML -> %s (%d개 문서)", path, len(parts))
    return path
