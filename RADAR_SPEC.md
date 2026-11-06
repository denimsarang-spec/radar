# 진입 레이더 — 로컬 스펙 (RADAR_SPEC.md)

## 현재 시스템 (2026-11 기준, 아래 초기 스펙보다 우선)
| 구성 | 시각(KST) | 하는 일 | 결과 |
|---|---|---|---|
| Actions `radar.yml` | 평일 19:30 (+ LLM·코드 변경 푸시 시) | 01~07 수집·판정 | `data/<거래일>.json`, `out/`, GitHub Pages |
| 루틴 `진입 레이더 일일 갱신` | 평일 08:35 | 06·07·08 보조, 월가 해석, 후보군, 일정 | 아티팩트, `claude/llm-*` 브랜치의 `data/llm/latest.json` |
| Actions `llm-merge` | 루틴 푸시 직후 | latest.json 형식 검사 후 main 반영, 페이지 재생성 | main |
| `live.py` | PC 로그온부터 | 시세 5~70초, 램프 30초~5분 | http://127.0.0.1:8765 (기록 안 남김) |

규칙
- `date`는 기준 거래일. 당일 16:30 전 값은 `provisional`이며 `data/`·`kis_fut.json`에 저장하지 않는다.
- 지난 거래일 기록은 덮어쓰지 않는다. 고칠 때만 `--force`.
- 06은 FRED(국내 회선은 `data/hy_oas.json`), 비면 LLM. 07은 수동 입력, LLM asof가 더 새로우면 LLM. 08은 LLM.
- LLM 결과는 5일 지나면 판정에 쓰지 않는다. 형식은 `radar.validate_llm`.
- 소스 실패는 `sig.errors`와 화면 "수집 실패"에 남긴다. 조용히 삼키지 않는다.

독립 프로젝트. 기존 스캐너·DB·봇과 연결하지 않는다. 폴더 `D:\radar` (예시) 하나로 완결.
목적: 매일 아침 8개 진입 조건의 숫자 램프를 로컬에서 계산해 `signals.json`을 만들고, 같은 신호판 HTML을 뽑는다. 매수·매도 판단은 하지 않는다.

## 초기 스펙

## 0. 역할 분담
- 로컬 스크립트 (이 스펙): 01·02·03·05·07 숫자 램프 계산, 04 SOX 20일선 계산, `signals.json` + `index.html` 생성
- LLM (Opus 예약 작업 / 필요 시 Fable): 04 월가 해석 문구, 06 하이일드, 08 이익 컨센서스, 후보군 공시 갱신, 결론 한 줄
- 사람: 링 색 보고 결정

## 1. 폴더 구조
```
D:\radar\
  radar.py            # 메인: 수집 → 판정 → signals.json → index.html
  sources\            # 소스별 수집 함수 (krx.py, dram.py, us.py, kis.py 선택)
  template\index.html # 신호판 템플릿 (현재 아티팩트 HTML, 값 자리에 {{ }} 치환)
  data\YYYY-MM-DD.json# 일별 signals 보관 (백테스트용)
  out\index.html      # 오늘 결과
  RADAR_SPEC.md
```

## 2. 데이터 소스 (무료 우선, KIS는 선택)
| 항목 | 1차 소스 | 2차(교차검증) |
|---|---|---|
| 코스피 종가·등락·장중 고저 | pykrx `get_index_ohlcv` | yfinance `^KS11` |
| 외국인·개인·기관 코스피 순매수(일별) | pykrx `get_market_trading_value_by_investor` | KRX 정보데이터시스템 CSV |
| 외인 코스피200 선물 순매수·미결제약정 | KRX 정보데이터시스템(파생 투자자별) 다운로드 | KIS API(선택) |
| 코스피200 현물·선물 종가 → 베이시스 | pykrx 코스피200 + KRX 선물 종가 | 네이버 금융 선물 페이지 |
| SOX 종가·20일선 | yfinance `^SOX` | stooq |
| D램 현물 DDR5 16Gb / DDR4 16Gb / DDR4 8Gb | DRAMeXchange 무료 보드 스크랩 (하루 1회) | memoryindex.io / stockhub 반도체 페이지 |
| 외인 코스피 시총 비중 | KRX 정보데이터시스템(외국인 보유 현황) | 금투협 통계 |
| 원달러 | yfinance `KRW=X` | 서울외국환중개 |
- 스크랩은 실패해도 전체가 죽지 않게 try/except, 실패 항목은 `null` + `na`
- 두 소스 값이 1% 넘게 다르면 `verify: "mismatch"`로 기록하고 1차 소스 채택

## 3. 8개 조건 판정 규칙 (출력 상태: ok / warn / no / na)
- **01 급락일 외인 매도 급감**: 코스피 등락 ≤ -2.0%인 날, 외인 순매도 절대값 < 직전 20거래일 외인 순매도 절대값 평균 → ok. 급락일인데 평균 이상 → no. 급락일 아니면 직전 급락일 판정 유지, 단 그 뒤 5거래일 지나면 warn.
- **02 외인 선물 롱 유지**: 외인 코스피200 선물 5일 누적 순매수 > 0 이고 당일 ≥ 0 → ok. 5일 누적 > 0 인데 당일 < 0 → warn. 5일 누적 ≤ 0 → no.
- **03 베이시스 정상화**: 시장 베이시스 = 선물 종가 − 현물 종가. ≥ 0 → ok. −1.0 ~ 0 → warn. < −1.0 → no. (이론 베이시스는 참고 표시만)
- **04 SOX 20일선 복귀**: SOX 종가 > 20일 단순이동평균 → ok, 아니면 no. (해석 문구는 LLM)
- **05 D램 현물 유지**: DDR5 16Gb 현물가가 30일 전 대비 > 0 → ok. ≤ 0 → no (무효 램프). 데이터 없으면 na.
- **06 하이일드 스프레드**: 로컬에선 na 고정. LLM이 채우면 그 값 사용.
- **07 외인 시총 비중**: ≤ 33.0% → ok, 33~35 → warn, > 35 → no.
- **08 이익 컨센서스**: 로컬에선 warn 고정. LLM이 채우면 그 값 사용.

### 단계
- `stage = "s2"` if 01·02·03·04·05 모두 ok
- `stage = "s1"` if 01·02·03 모두 ok
- 그 외 `"wait"`
- `veto = true` if 05 == no or 08 == no  (결론에 경고, 단계 무관)
- 01이 no면 어떤 단계든 `"wait"`로 리셋

### 색
wait → `--no`(빨강), s1 → `--warn`(주황), s2 → `--ok`(초록). 링 호·스윕·결론 점 세 곳 모두 같은 색.

## 4. signals.json 형식
```json
{
  "date": "2026-11-05",
  "stage": "wait",
  "veto": false,
  "ok_count": 1,
  "lamps": {
    "01": {"s":"no",  "val":"11/4 -2.50조 · 전일 1.7배", "raw":{"kospi_chg":-0.0402,"frgn":-2502000000000,"avg20":-1480000000000}, "verify":"2src"},
    "02": {"s":"warn","val":"5일 누적 +1.6조 · 당일 -2,390억", "raw":{}, "verify":"1src"},
    "03": {"s":"no",  "val":"시장 -1.98pt · 이론 +2.60", "raw":{}, "verify":"1src"},
    "04": {"s":"no",  "val":"10,268.65 · 20MA 11,0xx", "raw":{}, "verify":"2src"},
    "05": {"s":"ok",  "val":"DDR5 16Gb $61.50 · 30일 +6.1%", "raw":{}, "verify":"2src"},
    "06": {"s":"na",  "val":"미수집"},
    "07": {"s":"no",  "val":"37.38% · 바닥 32~33%", "raw":{}, "verify":"2src"},
    "08": {"s":"warn","val":"LLM 미채움"}
  },
  "tape": {"dow":"+3.03%","sox":"-3.13%","vix":"-19%","us10y":"4.884%","dxy":"101.50","wti":"$88.12 -8.3%","kospi":"5,683.80 +0.62%","usdkrw":"1,338.80"},
  "next": [{"d":"11/12 목","n":"옵션 만기 + 샘씨엔에스 실적 → 03"}],
  "llm": {"wallst_note":"", "candidates_updates":[], "verdict":""}
}
```
- `raw`는 판정에 쓴 원수치 전부. 백테스트 때 재판정 가능해야 함.
- `llm` 블록은 비워두고, LLM이 채우면 머지.

## 5. HTML 생성
- `template/index.html` = 현재 아티팩트 HTML 그대로. 데이터가 들어가는 자리를 `{{lamp01_s}}`, `{{lamp01_val}}`, `{{stage}}`, `{{ok_count}}`, `{{tape_sox}}` 식으로 치환.
- 후보군 섹션(id="cands")과 검증 로그는 템플릿에 고정, `llm.candidates_updates`가 있으면 해당 행만 치환.
- 출력 `out/index.html`. 폰에서 보려면 깃허브 페이지(공개 저장소에 out/ 만 푸시) 또는 집 PC의 간단한 http 서버.

## 6. 실행
- 윈도 작업 스케줄러: 평일 08:10 `python D:\radar\radar.py`
- 성공 시 `data/YYYY-MM-DD.json` 저장, `out/index.html` 갱신, (선택) `git push`
- 실패 항목은 na로 두고 반드시 HTML은 생성 (빈 화면 금지)
- 예약 작업(Opus)은 08:20에 깃허브의 signals.json을 읽어 `llm` 블록을 채우고 아티팩트 갱신. 못 읽으면 기존처럼 검색으로 대체.

## 7. 백테스트 (나중)
- `data/*.json` 90일 쌓이면: stage가 s1으로 바뀐 날 기준 5·10·20일 코스피 수익률, 그리고 veto가 켜진 날 이후 수익률을 표로. 레이더를 믿을 근거를 숫자로.

## 8. 하지 않는 것
- 기존 스캐너·봇·DB와 import/연결 없음
- 매수·매도 신호 출력 없음. 상태와 단계만
- 종목 추천 문구 없음. 후보군은 "검토 목록"
