# disclosure-sdc

한국 기업공시의 Semantic Change와 정량모형 대비 Incremental Information
— 연구 실행 계획서의 **Phase 0 (사전 진단)** 구현체.

> 현재 구현 범위는 Phase 0 뿐이다. Phase 1~9는 아직 비어 있다.
> Gate 0을 통과하기 전에는 다음 Phase로 넘어가지 않는다.

---

## 1. 지금 상태 한 줄 요약

Phase 0 파이프라인(P0, P0-b)이 코드·테스트·리포트 생성까지 완성되어 있고,
**OpenDART 인증키만 꽂으면 곧바로 실데이터로 돌아간다.**
키가 아직 없으므로 `--mock`(합성 공시) 모드로 전 구간을 검증해 두었다.

---

## 2. API 키 — 나중에 넣을 자리

키는 코드나 `config.yaml` 어디에도 하드코딩하지 않는다. **단 한 곳만 채우면 된다.**

```bash
cp .env.example .env
# .env 를 열어 OPENDART_KEY=... 한 줄만 채운다
```

| 항목 | 위치 | 상태 |
|---|---|---|
| OpenDART 인증키 | `.env` 의 `OPENDART_KEY` | **미발급 — 발급 후 채울 것** (opendart.fss.or.kr, 일 20,000건) |
| LLM API 키 | `.env` 의 `LLM_API_KEY` | Phase 6에서만 필요. Phase 0~5는 비용 0원 |

키를 읽는 코드는 [src/collect/dart_client.py](src/collect/dart_client.py) 의
`load_api_key()` 하나뿐이다. 키가 없으면 `MissingApiKey` 를 던지고
`--mock` 으로 실행하라는 안내를 출력한다. **소스 수정은 필요 없다.**

---

## 3. 설치

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt
```

## 4. 실행

```bash
python -m src.pilot.p0_diagnostics --mock
```

```bash
python -m src.pilot.p0b_change_diagnostics --mock
```

키 발급 후에는 `--mock` 을 떼고 돌린다. 부록 B.1 원칙대로 먼저 소규모로:

```bash
python -m src.pilot.p0_diagnostics --limit 3
```

```bash
python -m src.pilot.p0_diagnostics
```

```bash
python -m src.pilot.p0b_change_diagnostics
```

테스트:

```bash
python -m pytest tests -q
```

---

## 5. 무엇을 진단하는가 (0.1 D1·D2·D3)

| # | 진단 항목 | 통과 기준 | 구현 |
|---|---|---|---|
| D1 | 4개 후보 섹션의 문자 수 및 전년 대비 변화율 | 섹션 평균 2,000자 이상, 변화율 중앙값 5% 이상 | `p0_diagnostics` (문자 수) + `p0b_change_diagnostics` (변화율) |
| D2 | 연도별 평균 텍스트 변화율의 공통 스파이크 | 스파이크가 관측되면 정상 | `p0b_change_diagnostics` 2·3절 |
| D3 | 사업보고서 파싱 성공률 | 85% 이상 | `p0_diagnostics` 2절 |

대상 4개 섹션 (`config.yaml` 의 `sections`):

| id | 섹션명 |
|---|---|
| S1 | 사업의 내용 |
| S2 | 이사의 경영진단 및 분석의견 |
| S3 | 그 밖에 투자자 보호를 위하여 필요한 사항 |
| S4 | 임원 및 직원 등에 관한 사항 |

### 섹션 매칭 원칙

**섹션 번호(로마숫자)는 연도마다 바뀌므로 번호가 아니라 이름으로 매칭한다.**
`src/parse/sections.py` 는

1. HTML을 블록 시퀀스로 평탄화하고 (표는 별도 블록),
2. 표기 변형(공백·중점·괄호·앞머리 번호)을 제거한 정규형으로 최상위 섹션 헤더를 찾고,
3. 섹션의 끝을 **다음 최상위 헤더**로 정하며,
4. 같은 이름이 목차(TOC)와 본문에 둘 다 나오면 **본문 길이가 긴 쪽**을 택한다.

이 네 가지는 전부 `tests/test_sections.py` 에 회귀 테스트로 고정되어 있다.

---

## 5-b. P0-c — 섹션 경계 · 문단 분할 수정

실데이터 89건을 돌린 뒤 P0-b 상위 유사쌍을 검토하다가 두 가지 결함이 드러났다.
S1 에 K-IFRS 재무제표 주석이 섞여 들어갔고, S4 의 문단당 평균 문자 수가 9.1자였다.
`p0c_boundary_audit` 이 이를 진단하고 수정 전/후를 비교한다.

```bash
python -m src.pilot.p0c_boundary_audit
```

### 근본 원인 3가지

| # | 증상 | 원인 | 수정 |
|---|---|---|---|
| 1 | 표 셀이 문단으로 분해 (S4 문단당 9.1자) | 블록 분해가 **화이트리스트** 방식이라 DART 커스텀 컨테이너 태그(`<TABLE-GROUP>`, `<SECTION-3>`, `<COVER>`, `<LIBRARY>`) 안의 `<table>` 이 인라인으로 취급되어 표 전체가 본문으로 새어 들어감 | 인라인 태그만 블랙리스트로 지정하고 나머지는 블록으로 재귀. `<table>` 은 어디에 있든 분리 |
| 2 | 섹션이 잘못 잘림 | `1. 회사의 개요`, `2. 재무 등에 관한 사항` 같은 하위 소제목이 퍼지 92 로 오인 매칭 | 정확 일치 요구(퍼지 97) + 로마숫자 체계 문서에서는 로마숫자만 섹션 **시작**으로 인정 |
| 3 | 종료 경계 실패가 성공으로 집계 | 시작 헤더만 찾으면 `found=True` | `parse.require_terminator` — 종료 헤더가 없으면 `found=False` 로 강등 |

**시작 헤더와 종료 헤더는 기준이 다르다.** 시작은 로마숫자만 인정하지만, 종료는
번호 체계를 가리지 않는다. 종료를 놓치면 섹션이 문서 끝까지 흘러 주석이 통째로
섞이는 큰 피해가 나지만, 조금 이르게 끊는 것은 피해가 작기 때문이다.
실제로 `1. 전문가의 확인`(아라비아 번호)이 XI 섹션의 정당한 종료 헤더인 문서가 다수 있다.

### P0-c 산출물

| 파일 | 내용 |
|---|---|
| `boundary_audit.csv` | (문서 x 섹션 x legacy/fixed) 시작·종료 헤더 원문, 첫/마지막 200자 |
| `contamination_report.md` | K-IFRS 마커 기반 오염 진단 + 문제 문서 목록 |
| `paragraph_stats.csv` | 문단당 문자 수 분포 (평균/중앙값/p10/p90) |
| `common_pairs_recount.csv` | 문단 중복 제거 전/후 공통쌍 수 |
| `corp_dominance.csv` | corp_code 별 공통쌍 등장 빈도 |
| `manual_review.html` | 무작위 20건 수동 검수 UI (O/X + 메모, JSON 다운로드) |
| `p0c_report.md` | Part 1~4 종합 + Gate 0 재판정 |
| `sections_fixed/` | 수정된 추출 결과 |

`src/parse/legacy_sections.py` 는 수정 전 동작의 **동결 사본**이다. 같은 코드베이스에서
수정 전/후를 재현 가능하게 비교하기 위한 것이며, Gate 0 판정이 끝나면 삭제한다.

---

## 5-c. P0-d — 오염 지표 재정의 · 공통문단 재계산

P0-c 에서 섹션 경계는 정상(EOF 0건, legacy/fixed 356건 전부 동일)으로 확인되어
경계 수정은 종료했다. P0-d 는 남은 지표 결함을 고친다.

```bash
python -m src.pilot.p0d_report
```

### 오염 비중은 문자 수 기준이다

문단 수 기준 `오염 문단 수 / 전체 문단 수` 는 **분모가 파서 변경에 흔들린다**.
표 유입을 막고 짧은 문단을 병합하면 전체 문단 수가 절반 이하로 줄어드는데 오염
문단 수는 그대로라, 오염이 늘어난 것처럼 보인다. P0-c 에서 S1 오염 비중이
0.45% → 0.59% 로 '증가'한 것이 이 착시였다. 그래서
`오염 문단의 문자 수 합 / 섹션 전체 문자 수` 로 바꿨다.

마커 세트도 섹션별로 나눴다. S2(경영진단)에서는 `리스부채`·`한국채택국제회계기준`·
`손상차손`·`재무제표 작성기준` 이 정상적으로 언급될 수 있어 주석 유입의 신호로
쓸 수 없다.

### 공통문단은 세 가지 입력으로 센다

| 입력 | 정의 |
|---|---|
| `raw` | 그대로 |
| `dedup` | (rcept_no, section) 내 문단 텍스트 중복 제거 |
| `clean` | dedup + 회계기준 주석 마커 포함 문단 제외 |

**`clean` 에서도 공통 문단이 남는가** — 이것이 Phase 5(Template Filter)
필요성의 진짜 근거다.

### DART 편집기 플레이스홀더

Part C 를 수행하다 발견한 것: `◆click◆『수주상황』 삽입` 같은 문구가 356건 중
**177건(49.7%)** 에 들어 있었다. 각 문구가 정확히 59회 = 2016년 29건 + 2020년 30건,
즉 전 기업에 나타나고 2024년에는 사라진다. 공시 내용이 아니라 **DART 작성 도구의
위젯 라벨**이 본문 XML 에 그대로 실려 나온 것이다.

남겨두면 '기업 간 공통 변경 문단' 신호가 통째로 이 아티팩트로 채워진다 —
수정 전 S3 `2016->2020` 의 427쌍은 상위가 전부
`◆click◆『특례상장기업 관리종목 지정유예 현황』 삽입` 이었다.
`clean_text()` 에서 각주 마커와 함께 제거한다.

### 파싱 캐시

`src/pilot/parse_cache.py` 가 legacy/fixed 파싱 결과를 `parsed_cache.parquet` 로
남긴다. 파싱 경로 소스(`sections`, `legacy_sections`, `paragraphs`, `body`,
`textnorm`)의 해시를 함께 저장해, 하나라도 바뀌면 자동으로 다시 파싱한다.
강제로 다시 만들려면 `--rebuild-cache`.

> **파싱 경로에 모듈을 추가하면 `parser_fingerprint()` 목록에도 반드시 넣을 것.**
> 실제로 `textnorm` 이 빠져 있어 `clean_text()` 를 고쳤는데도 캐시가 무효화되지
> 않아 옛 결과가 그대로 리포트에 실린 적이 있다.

---

## 6. 산출물

`--mock` 실행 시 `data/pilot_mock/`, 실데이터는 `data/pilot/` 에 쌓인다.

| 파일 | 내용 |
|---|---|
| `report.md` | P0 진단 리포트 (섹션별 문자 수, 파싱 성공률, 실패 원인, Gate 0 체크리스트) |
| `diagnostics.csv` | `corp_code, corp_name, fy, section, char_len_text, char_len_table, n_paragraphs, parse_ok` |
| `reports_index.csv` | (기업, 회계연도) → `rcept_no` 매핑, 정정보고서 플래그 |
| `sections/{corp}_{fy}_{S#}.txt` | 표를 뺀 섹션 본문 |
| `tables/{corp}_{fy}_{S#}.html` | 섹션에서 떼어낸 표 |
| `failures.csv` | 단계별 실패 로그 (부록 B.2) |
| `change_rates.csv` | 인접 연도쌍 유사도 3종 + 변화율 |
| `change_diagnostics.md` | P0-b 리포트 |
| `common_paragraphs.csv` | 기업 간 거의 동일한 변경 문단 쌍 |
| `fig1_change_by_year.png` | 연도별 평균 변화율 (섹션별 라인) |
| `fig2_common_paragraphs.png` | 기업 간 공통 변경 문단 수 (연도별 바) |

원본 ZIP은 `data/raw/{rcept_no}.zip` 에 캐시되며 재실행 시 다시 받지 않는다.
`data/` 와 `results/` 는 전부 gitignore 대상이다 (부록 A.2).

---

## 7. Gate 0 통과 조건

```
□ 파싱 성공률 85% 이상
□ S1(사업의 내용) 평균 5,000자 이상
□ S2(경영진단)가 2,000자 미만이면 → MVP 섹션에서 제외 확정
□ 기업 간 공통 변경 문단이 실제로 관측됨 → Phase 5 필요성 확인
```

미달 시: 섹션 목록을 재확정하고 Phase 2 설계를 수정한 뒤 진행.
파싱 성공률이 70% 미만이면 표본 시작연도를 2018년으로 올린다.
판정 로직과 임계값은 전부 `config.yaml` 의 `gate0` 에 있다.

---

## 8. 표본 30개 기업

`config.yaml` 의 `universe` 에 KOSPI 15 + KOSDAQ 15가 시가총액 층화로 하드코딩되어 있다.

> **주의.** 이 30개는 Phase 0 파일럿용 **임의 추출본**이다. 정식 층화 무작위
> 추출(pykrx 기반, 시드 고정)은 Phase 1에서 수행하고 이 리스트를 교체한다.

---

## 9. 알려진 한계 — 3개 연도 표본에서 D2 스파이크 검정은 검정력이 없다

계획서는 "기업별 변화율을 기업 평균으로 표준화한 뒤 연도 평균을 보라"고 한다.
그런데 표본이 2016/2020/2024 **3개 연도**이면 기업당 인접 연도쌍이 **2개**뿐이라,
기업 평균으로 demean 한 두 값은 부호만 반대인 같은 크기가 되어 서로 상쇄된다.
게다가 2020년에 삽입된 서식 문구는 `2016→2020`(추가)과 `2020→2024`(삭제)의
변화율을 **동시에** 끌어올린다.

따라서 D2 판정은 **3절(기업 간 공통 변경 문단, MinHash)** 을 1차 근거로 삼는다.
demean 기반 스파이크 검정은 Phase 1에서 연속 연도 패널을 확보한 뒤 다시 수행한다.
`change_diagnostics.md` 는 이 조건이 걸리면 리포트 안에 경고를 자동으로 찍는다.

---

## 10. 디렉토리 (부록 A.1)

```
disclosure-sdc/
  README.md
  config.yaml              전역 설정 (표본, 기간, 시드, Gate 임계값)
  requirements.txt
  .env.example             OPENDART_KEY, LLM_API_KEY
  src/
    collect/               Phase 1  — dart_client, corp_code, report_select
    parse/                 Phase 2  — body, sections
    pilot/                 Phase 0  — p0_diagnostics, p0b_change_diagnostics,
                                      similarity, mock_source
    utils/                 공통     — config, logging, failures, textnorm, plotting
    features/ model/ diff/ template/ llm/ validation/ experiments/ mechanism/
                           (Phase 3~9, 아직 비어 있음)
  data/                    raw/ pilot/ corpus/ ... (gitignore)
  results/                 (gitignore)
  tests/
```

## 11. 재현성 (부록 A.3)

- 모든 난수 시드는 `config.yaml` 의 `seed` 에서 오고, 스크립트 시작 시 로그에 찍힌다.
- 중간 산출물은 전부 파일로 남긴다.
- 각 Phase는 재실행 가능(idempotent)하다. 이미 받은 ZIP은 다시 받지 않는다.
- 개별 실패가 전체를 중단시키지 않고 `failures.csv` 에 쌓인다.

## 12-a. Phase 0 종료 (P0-e)

```bash
python -m src.pilot.p0e_close
```

| 산출물 | 내용 |
|---|---|
| `results/pilot/artifact_impact.md` | **논문 Appendix 원본** — DART 편집기 잔재가 유사도 측정에 미치는 영향 (제거 전/후 cos·Jaccard·Levenshtein 비교) |
| `results/pilot/artifact_coverage.csv` | 잔재 3종의 문서 커버리지·제거 문자 수 |
| `results/pilot/common_pair_taxonomy.md` | 남은 공통쌍 4분류 (서식소제목/K-IFRS/법령인용/기타) |
| `results/pilot/gate0_final.md` | Gate 0 최종 판정 · Phase 0 종료 |

제거 전 텍스트는 P0-c 가 남긴 `data/pilot/sections_fixed/` 에 그대로 있고, 제거 후는
`data/pilot/sections/` 다. 두 코퍼스를 그대로 비교하므로 파서를 다시 건드리지 않는다.

**S4 는 텍스트 섹션에서 탈락**했다 (본문 평균 1,081자). 내용이 사실상 전부 표라서,
Phase 1 에서 표를 구조화 데이터로 추출하는 소스로 재분류한다.

---

## 13. Phase 1 — 데이터 인프라 (진행 중)

MVP 표본(각 회계연도 말 시총 상위 800개 × 2015–2024)의 공시 원문과 메타를 전량 확보한다.

### Phase 0 에서 넘어온 요구사항 3가지

| # | 요구 | 구현 | 상태 |
|---|---|---|---|
| 1 | 공정위 대규모기업집단 → `corp_code` 매핑 (연도별 스냅샷) | [src/collect/affiliates.py](src/collect/affiliates.py) | 로더·매칭 완성. **원자료 수동 배치 필요** |
| 2 | 편집기 잔재 제거 적용 + 제거 전 텍스트 병행 보관 | `clean_text()` + `phase1.keep_raw_text` | 설정 완료 |
| 3 | S4 표를 구조화 데이터로 추출 | [src/parse/officers.py](src/parse/officers.py) | 핵심 동작. 중첩 표 한계 있음 |

**요구 1 — 공정위 자료**는 API 키가 없어 자동 수집을 붙이지 못했다.
기업집단포털(egroup.go.kr)에서 연도별 소속회사 현황을 내려받아 아래에 두면 된다.

```
data/reference/fair_trade_groups_{year}.csv
필수 컬럼: group_name, corp_name
```

연도별로 두는 이유는 지정이 매년 바뀌기 때문이다. SKC 가 2023년 ISC 를 인수한
사례가 표본 안에 실제로 있어, 단일 시점 매핑을 쓰면 인수 전 연도까지 계열사로
잘못 묶인다.

### 접수시각을 수집하지 않는 이유

Gate 1 의 원래 조건은 "접수일시(시각 포함)가 전 건에 존재" 였다.
그런데 OpenDART 공시검색 API(`list.json`)는 접수**일자**만 주고 시각은 주지 않는다.
시각을 얻으려면 문서당 공시 상세 페이지를 1회씩 더 긁어야 하고, 7,900건이면
그만큼의 추가 요청이 된다.

**수집하지 않기로 했다.** 근거는 두 가지다.

1. 주 예측 horizon 이 t+1~t+120 거래일(6개월)이라 시각 단위 차이가 무의미하다.
2. 진입 규칙이 이미 "접수일 +1 거래일 시가" 로 고정되어 있어, 시각을 알아도
   이벤트일이 바뀌지 않는다.

Gate 1 조건을 아래로 교체했다.

| | 조건 |
|---|---|
| 기존 | 접수일시(시각 포함)가 전 건에 존재 |
| 변경 | 접수**일자**가 전 건에 존재하며, 이벤트일 = 접수일 +1 거래일 규칙이 코드에 강제되어 있다 |

규칙은 [src/utils/trading_calendar.py](src/utils/trading_calendar.py) 의 `event_date()`
한 곳에만 있고, `tests/test_trading_calendar.py` 가 금요일 제출·연휴 직전 제출·
접수일 자체가 비거래일인 경우까지 고정한다. 접수 당일 진입은 look-ahead 이므로
`next_trading_day()` 는 접수일이 거래일이어도 **그 다음** 거래일을 돌려준다.

거래일 달력은 KOSPI 지수(KS11)의 거래일에서 만든다 (KRX 로그인 불필요).

### 10자 미만 문단 기준을 바꾼 이유

Gate 1 의 원래 조건은 "문단당 문자 수 10자 미만 비중이 0" 이었다.
전량 파싱 결과 **182개**가 남았고, 전부 확인해 보니 이런 것들이었다.

```
[27] '(2) 커머스'        <- 직전 문단 2,244자
[38] '(6) 시장점유율'     <- 직전 문단 2,665자
[126] '6. 수주현황'       <- 직전 문단 2,445자
```

원인은 `merge_short_paragraphs` 의 `max_merged_chars=2000` 가드다.
표가 통째로 새어 들어왔을 때 한 문단이 무한정 커지는 것을 막는 장치인데,
그 부작용으로 이미 2,000자를 넘은 문단 뒤의 짧은 소제목은 병합되지 않는다.

**가드를 조정하지 않았다.** 2,244자 문단에 소제목을 이어붙이면 문단 경계가
오히려 나빠진다. 애초에 잡으려던 것은 표 셀 파편이었고, 그것은 S4 에서
0개가 됐다 (수정 전 문단당 8.3자, 10자 미만 비중 78%).

| | 조건 |
|---|---|
| 기존 | 문단당 문자 수 10자 미만 비중이 0 |
| 변경 | **표 셀 파편에서 기인한** 10자 미만 문단 0개 (긴 문단 뒤 소제목은 정상) |

전체 약 190만 문단 중 182개, 0.01% 다.

### 수집 범위 — 유니버스 firm-year **및 그 직전 연도**

유니버스는 매년 시총 상위 800 으로 재산정한다. 그런데 문서를 유니버스
firm-year 만 받으면, '올해는 유니버스인데 작년에는 아니었던' 기업이 직전 연도
문서를 갖지 못해 **전년 대비 변화**를 계산할 수 없다.

이건 단순 결손이 아니라 **선택편향**이다. 유니버스 신규 진입은 시총이 급등한
기업, 이탈은 급락한 기업이므로, 수익률 예측 연구에서 가장 중요한 구간이 통째로
빠진다.

| | 페어링률 |
|---|---|
| 유니버스 firm-year 만 수집 | 83.1% (Gate 3 미달) |
| **직전 연도까지 수집** | **95.6%** |

추가 수집분은 `sample_role='pair_only'` 로 표시한다. **이 문서들은 t-1 기준
문서로만 쓰이고, 그 자체가 관측(t)이 되지 않는다.** 학습 표본에 중복 투입하면
안 된다.

남은 4.4% 는 직전 연도 보고서가 DART 에 존재하지 않는 경우다 (상장 전 등).
예: DS단석은 2023년 상장이라 fy2022 사업보고서가 없다. 원리적으로 페어링 불가다.

### 생존편향

"현재 상장된 기업 목록"으로 표본을 만들면 안 된다. 각 회계연도 말 시점의 상장종목
스냅샷을 따로 만들고 그 기준으로 표본을 구성한다. 상장폐지된 기업도 그 시점에
상장되어 있었다면 반드시 포함한다.

---

## 14. 다음 단계

1. OpenDART 키 발급 → `.env` 에 기입
2. `python -m src.pilot.p0_diagnostics --limit 3` 로 실데이터 소규모 시운전
3. 전체 30개 실행 → `data/pilot/report.md` 로 Gate 0 판정
4. 통과 시 Phase 1(데이터 인프라)로, 미달 시 섹션 목록 재확정
