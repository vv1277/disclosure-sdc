"""API 키가 없을 때 파이프라인을 점검하기 위한 합성 공시 생성기.

TODO(API): OPENDART_KEY 가 발급되면 --mock 없이 실행하면 된다.
이 모듈은 그때도 회귀 테스트용으로 남겨 둔다.

경고: --mock 산출물은 data/pilot_mock/ 에만 쓴다. 실데이터와 절대 섞지 않는다.
      여기서 나온 숫자는 Gate 0 판정 근거가 될 수 없다.
"""
from __future__ import annotations

import logging
import random
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.config import Config

log = logging.getLogger(__name__)

# 2020년에 모든 기업 공시에 동일하게 삽입되는 '서식 개정' 문단.
# P0-b 의 스파이크/공통문단 탐지가 실제로 작동하는지 확인하는 장치다.
_TEMPLATE_REVISION_YEAR = 2020
_TEMPLATE_PARAGRAPHS = [
    "본 보고서는 기업공시서식 작성기준 개정에 따라 해당 서식에 맞추어 작성되었습니다. "
    "개정된 서식은 투자자의 이해가능성 제고를 위하여 항목별 기재 순서를 조정하였습니다.",
    "당사는 기업공시서식 작성기준에 따라 주요 제품 및 서비스의 매출 비중을 사업부문별로 "
    "구분하여 기재하였으며, 전기 대비 비교가능성을 확보하기 위하여 재작성하였습니다.",
    "본 항목은 기업공시서식 작성기준 개정으로 신설된 항목이며, 관련 세부내역은 "
    "상세표를 참조하시기 바랍니다.",
]

_BIZ_SENTENCES = [
    "당사는 {seg} 부문을 중심으로 사업을 영위하고 있으며, 당해 사업연도 매출액은 {rev}억원입니다.",
    "{seg} 시장은 전방산업의 수요 변동에 민감하며, 당사는 원가 경쟁력 확보를 위해 노력하고 있습니다.",
    "당사의 주요 원재료는 {mat}이며, 당기 평균 매입단가는 전기 대비 {pct}% 변동하였습니다.",
    "연구개발 활동은 {seg} 관련 신제품 개발에 집중되어 있으며, 당기 연구개발비는 {rd}억원입니다.",
    "생산설비는 국내 {n_plant}개 사업장에 분산되어 있고, 당기 평균 가동률은 {util}%입니다.",
    "당사는 주요 고객사와의 장기 공급계약을 통해 안정적인 매출 기반을 확보하고 있습니다.",
    "경쟁 환경은 국내외 {n_comp}개 업체가 경합하는 구조이며, 당사의 시장점유율은 {share}%입니다.",
]
_MDNA_SENTENCES = [
    "당기 영업이익은 {op}억원으로 전기 대비 {pct}% 변동하였습니다.",
    "유동비율은 {cur}%, 부채비율은 {debt}% 수준으로 재무안정성은 양호합니다.",
    "향후 {seg} 수요 회복 여부가 실적의 주요 변수로 판단됩니다.",
    "환율 변동은 당사 손익에 유의적인 영향을 미치며, 당기 환관련 손익은 {fx}억원입니다.",
]
_INVEST_SENTENCES = [
    "당사는 보고기간 종료일 현재 계류 중인 소송사건 {n_suit}건이 있으며, 소송가액은 {amt}억원입니다.",
    "당기 중 감독기관으로부터의 제재 사항은 {n_pen}건입니다.",
    "당사는 중요한 우발부채 및 약정사항을 주석에 기재하였습니다.",
    "보고기간 종료일 이후 발생한 중요한 사항은 없습니다.",
]
_EMP_SENTENCES = [
    "보고기간 종료일 현재 직원 수는 {emp}명이며, 평균 근속연수는 {yrs}년입니다.",
    "당기 미등기임원을 포함한 임원 수는 {exec_n}명입니다.",
    "직원 1인 평균 급여액은 {pay}백만원입니다.",
]
_SEGMENTS = ["반도체", "이차전지", "화학", "자동차부품", "바이오", "엔터테인먼트",
             "유통", "금융", "전력", "철강", "정유", "디스플레이"]
_MATERIALS = ["웨이퍼", "리튬", "나프타", "알루미늄", "니켈", "원유", "구리"]

# 섹션명 -> (문장 템플릿, 기준 문단 수)
# 실제 사업보고서의 대략적인 분량(S1 수천~수만 자)에 맞춰 잡는다.
_SECTION_BODY = {
    "사업의 내용": (_BIZ_SENTENCES, 120),
    "이사의 경영진단 및 분석의견": (_MDNA_SENTENCES, 40),
    "그 밖에 투자자 보호를 위하여 필요한 사항": (_INVEST_SENTENCES, 45),
    "임원 및 직원 등에 관한 사항": (_EMP_SENTENCES, 28),
    "임원 및 직원에 관한 사항": (_EMP_SENTENCES, 28),   # 구서식 표기
}

# 연도별 목차. 번호 체계와 섹션명 표기가 연도마다 바뀌는 상황을 재현한다.
_TOC_OLD = (      # ~2019 서식
    "회사의 개요",
    "사업의 내용",
    "재무에 관한 사항",
    "감사인의 감사의견 등",
    "이사의 경영진단 및 분석의견",
    "이사회 등 회사의 기관에 관한 사항",
    "주주에 관한 사항",
    "임원 및 직원에 관한 사항",
    "이해관계자와의 거래내용",
    "그 밖에 투자자 보호를 위하여 필요한 사항",
    "재무제표 등",
)
_TOC_NEW = (      # 2020~ 서식
    "회사의 개요",
    "사업의 내용",
    "재무에 관한 사항",
    "이사의 경영진단 및 분석의견",
    "회계감사인의 감사의견 등",
    "이사회 등 회사의 기관에 관한 사항",
    "주주에 관한 사항",
    "임원 및 직원 등에 관한 사항",
    "계열회사 등에 관한 사항",
    "대주주 등과의 거래내용",
    "그 밖에 투자자 보호를 위하여 필요한 사항",
    "상세표",
)

_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
          "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII"]


def _fmt(template: str, rng: random.Random, seg: str) -> str:
    return template.format(
        seg=seg,
        mat=rng.choice(_MATERIALS),
        rev=rng.randint(500, 90000),
        rd=rng.randint(10, 5000),
        op=rng.randint(-500, 20000),
        pct=round(rng.uniform(-40, 60), 1),
        util=rng.randint(55, 99),
        n_plant=rng.randint(1, 9),
        n_comp=rng.randint(3, 40),
        share=round(rng.uniform(1, 45), 1),
        cur=rng.randint(60, 400),
        debt=rng.randint(20, 320),
        fx=rng.randint(-900, 900),
        n_suit=rng.randint(0, 25),
        amt=rng.randint(1, 3000),
        n_pen=rng.randint(0, 5),
        emp=rng.randint(50, 120000),
        yrs=round(rng.uniform(2, 18), 1),
        exec_n=rng.randint(3, 40),
        pay=rng.randint(30, 180),
    )


def _paragraphs(name: str, rng: random.Random, seg: str, fy: int,
                seed_base: int) -> list[str]:
    templates, base_n = _SECTION_BODY[name]
    # 기업마다 분량 편차를 준다 (실제 공시의 분산을 흉내).
    scale = random.Random(seed_base + 977).uniform(0.4, 1.7)
    n = max(3, int(round(base_n * scale)))
    # 기업 고유의 '안정적' 문단: 연도가 바뀌어도 유지된다 (변화율이 1이 되지 않게).
    stable_rng = random.Random(seed_base)
    out = [
        _fmt(stable_rng.choice(templates), stable_rng, seg)
        for _ in range(max(1, n // 2))
    ]
    # 연도별로 바뀌는 문단
    out += [_fmt(rng.choice(templates), rng, seg) for _ in range(n - len(out))]
    if fy == _TEMPLATE_REVISION_YEAR:
        out = _TEMPLATE_PARAGRAPHS + out  # 전 기업 공통 변경
    rng.shuffle(out)
    return out


def _table_html(rng: random.Random) -> str:
    rows = "".join(
        "<tr><td>구분{i}</td><td>{a}</td><td>{b}</td></tr>".format(
            i=i, a=rng.randint(100, 99999), b=rng.randint(100, 99999)
        )
        for i in range(1, rng.randint(4, 12))
    )
    head = "<tr><th>구분</th><th>당기</th><th>전기</th></tr>"
    return "<table>" + head + rows + "</table>"


def build_mock_html(corp_name: str, stock_code: str, fy: int, seed: int) -> str:
    """DART 원본과 유사한 구조(목차 + 본문)의 합성 사업보고서 HTML."""
    seed_base = seed + int(stock_code)
    rng = random.Random(seed_base + fy)
    seg = random.Random(seed_base).choice(_SEGMENTS)

    # 연도마다 목차와 번호 체계가 달라진다 (번호 의존 파서를 잡아내기 위함).
    names = list(_TOC_NEW if fy >= 2020 else _TOC_OLD)
    numbered = [(_ROMAN[i] + ". " + nm, nm) for i, nm in enumerate(names)]

    toc = "".join("<tr><td>" + label + "</td></tr>" for label, _ in numbered)
    parts = [
        "<html><head><title>사업보고서</title></head><body>",
        "<p>" + corp_name + " 사업보고서</p>",
        "<p>(제{n}기) 사업연도 {fy}.01.01 부터 {fy}.12.31 까지</p>".format(
            n=fy - 1980, fy=fy
        ),
        "<table>" + toc + "</table>",  # 목차: 헤더로 오인되면 안 된다
    ]
    for label, nm in numbered:
        parts.append("<p><b>" + label + "</b></p>")
        if nm in _SECTION_BODY:
            for p in _paragraphs(nm, rng, seg, fy, seed_base):
                parts.append("<p>" + p + "</p>")
            parts.append(_table_html(rng))
        else:
            parts.append("<p>해당 사항은 관련 규정에 따라 기재하였습니다.</p>")
            parts.append(_table_html(rng))
    parts.append("</body></html>")
    return "\n".join(parts)


def mock_rcept_no(stock_code: str, fy: int) -> str:
    return "{y}0331{sc}".format(y=fy + 1, sc=stock_code)


class MockDartClient:
    """DartClient 와 같은 인터페이스를 흉내내는 오프라인 소스."""

    def __init__(self, cfg: Config, seed: int):
        self.cfg = cfg
        self.seed = seed
        self.raw_dir = cfg.dir("raw") / "mock"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.n_calls = 0
        self._by_corp = {mock_corp_code(u["stock_code"]): u for u in cfg["universe"]}

    def search_reports(self, corp_code: str, bgn_de: str, end_de: str,
                       **_: Any) -> list[dict[str, Any]]:
        u = self._by_corp[corp_code]
        sc = u["stock_code"]
        rows: list[dict[str, Any]] = []
        for fy in self.cfg["sample"]["fiscal_years"]:
            filed = "{y}0331".format(y=fy + 1)
            if not (bgn_de <= filed <= end_de):
                continue
            base = {
                "corp_code": corp_code, "corp_name": u["name"],
                "stock_code": sc, "flr_nm": u["name"],
            }
            rows.append(dict(
                base,
                report_nm="사업보고서 ({fy}.12)".format(fy=fy),
                rcept_no=mock_rcept_no(sc, fy),
                rcept_dt=filed,
            ))
            # 일부 기업에는 정정보고서도 섞는다 (원본 우선 로직 점검용)
            if int(sc) % 7 == 0:
                rows.append(dict(
                    base,
                    report_nm="[기재정정]사업보고서 ({fy}.12)".format(fy=fy),
                    rcept_no=mock_rcept_no(sc, fy)[:-1] + "9",
                    rcept_dt="{y}0520".format(y=fy + 1),
                ))
            # 분기/반기보고서 노이즈: 필터가 걸러내야 한다
            rows.append(dict(
                base,
                report_nm="반기보고서 ({fy}.06)".format(fy=fy),
                rcept_no="{y}0814{sc}".format(y=fy, sc=sc),
                rcept_dt="{y}0814".format(y=fy),
            ))
        self.n_calls += 1
        return rows

    def download_document(self, rcept_no: str) -> Path:
        dest = self.raw_dir / (rcept_no + ".zip")
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        stock_code = rcept_no[8:14]
        fy = int(rcept_no[:4]) - 1
        u = next((x for x in self.cfg["universe"] if x["stock_code"] == stock_code), None)
        if u is None:  # 정정본은 끝자리를 바꿔 두었다
            u = next(x for x in self.cfg["universe"]
                     if x["stock_code"][:5] == stock_code[:5])
        html = build_mock_html(u["name"], u["stock_code"], fy, self.seed)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            # 실제 DART 원본처럼 CP949 로 인코딩해 디코더 경로까지 태운다
            zf.writestr(rcept_no + ".html", html.encode("cp949", errors="replace"))
            zf.writestr("attach_감사보고서.html",
                        "<html><body><p>첨부</p></body></html>".encode("cp949"))
        self.n_calls += 1
        return dest


def mock_corp_code(stock_code: str) -> str:
    return "MOCK" + stock_code


def build_mock_mapping(cfg: Config) -> pd.DataFrame:
    rows = [
        {
            "corp_code": mock_corp_code(u["stock_code"]),
            "corp_name": u["name"],
            "stock_code": u["stock_code"],
            "modify_date": "20260101",
            "is_listed": True,
        }
        for u in cfg["universe"]
    ]
    return pd.DataFrame(rows)
