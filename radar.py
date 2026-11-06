"""진입 레이더: 수집 → 판정 → signals.json → index.html  (RADAR_SPEC.md)

python radar.py                 # 전체 실행 (data/YYYY-MM-DD.json, out/index.html)
python radar.py --lamp 01       # 램프 하나만 계산해 출력 (저장 안 함)
python radar.py --render        # 마지막 결과(LLM 머지 후)로 HTML만 다시 생성
python radar.py --date 2026-11-04 --force   # 과거 기록 다시 쓰기
python radar.py --check-llm data/llm/latest.json   # LLM 결과 형식 검사

date는 실행일이 아니라 기준 거래일이다. 16:30 전 당일 값은 잠정(provisional)이라 data/에 저장하지 않는다.
"""
import argparse
import datetime as dt
import html
import json
import logging
import re
import subprocess
import sys
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

from sources import dram, fred, kis, manual, naver, us  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA, OUT, TEMPLATE = ROOT / "data", ROOT / "out", ROOT / "template" / "index.html"
KIS_FUT_CACHE = DATA / "kis_fut.json"  # 커밋 대상: Actions 러너에서도 이력 유지
KIS_SAVE_ALWAYS = False                # live.py: 장중 값도 자기 캐시에 쌓는다
HY_CACHE = DATA / "hy_oas.json"        # 커밋 대상: FRED가 막힌 곳(국내 PC)의 대체 이력
LLM_PATH = DATA / "llm" / "latest.json"  # 예약 루틴(Opus)이 매일 갱신

DATA.mkdir(exist_ok=True)
logging.basicConfig(filename=DATA / "radar.log", encoding="utf-8", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radar")

CRASH = -0.02          # 01 급락일 기준
CRASH_LOOKBACK = 60    # 직전 급락일 탐색 범위(거래일)
STALE_DAYS = 5         # 01 판정 유지 기간(거래일)
FINAL_AT = dt.time(16, 30)  # 이 시각 전 당일 값은 잠정 (네이버 closePriceSendTime)
HY_MAX_AGE = 10        # 06 하이일드 허용 나이(일)
LLM_MAX_AGE = 5        # LLM 결과 허용 나이(일), 지나면 06·07·08에 쓰지 않음


# ---------- 공통 ----------
def verify(a, b):
    if a is None and b is None:
        return None
    if a is None or b is None:
        return "1src"
    return "mismatch" if abs(a - b) > abs(a) * 0.01 else "2src"


def na(val="미수집", raw=None):
    return {"s": "na", "val": val, "raw": raw or {}, "verify": None}


def md(d):
    return f"{d.month}/{d.day}"


def jo(won):
    return f"{won / 1e12:+.2f}조" if abs(won) >= 1e12 else f"{won / 1e8:+,.0f}억"


def is_provisional(asof):
    now = dt.datetime.now()
    return asof == now.date() and now.time() < FINAL_AT


def check(item, a_name, a, b_name=None, b=None, fmt=str):
    """검증 로그 한 줄. 같은 기준일의 두 값을 실제로 비교한 결과만 남긴다."""
    show = lambda n, x: n if n is None else (f"{n} {fmt(x)}" if x is not None else f"{n} 값 없음")  # noqa: E731
    gap = abs(a - b) / abs(a) if a and b is not None else None
    return {"item": item, "a": show(a_name, a), "b": show(b_name, b), "r": verify(a, b), "gap": gap}


def worst(checks):
    rs = [c["r"] for c in checks]
    for r in ("mismatch", "1src", "2src"):
        if r in rs:
            return r
    return None


class Ctx:
    """수집 결과 캐시. 램프마다 필요한 것만 수집한다."""

    def __init__(self, day):
        self.day = day
        self.m = manual.load()
        self._c = {}
        self.chains = {}
        self.errors = []

    def get(self, key, fn):
        """소스 호출 1회 + 캐시. 실패·빈 응답은 None + 로그 + 화면 표시 (다음 소스/램프로 진행)."""
        if key not in self._c:
            try:
                self._c[key] = fn()
                if self._c[key] is None:
                    raise ValueError("빈 응답")
            except Exception as ex:
                log.warning("source %s 실패: %s: %s", key, type(ex).__name__, ex)
                self.errors.append(f"{key}: {type(ex).__name__}: {str(ex)[:120]}")
                self._c[key] = None
        return self._c[key]

    def upto(self, s):
        return s[[d <= self.day for d in s.index]] if s is not None else None

    def kospi_sources(self):
        return (self.upto(self.get("nv_kospi", lambda: naver.daily_close("KOSPI"))),
                self.upto(self.get("yf_kospi", us.kospi_yf)))

    def kospi(self):
        nv, yf = self.kospi_sources()
        return nv if nv is not None else yf

    def trading_days(self):
        s = self.kospi()
        return list(s.index) if s is not None else []

    def last_trading_day(self):
        d = self.trading_days()
        return d[-1] if d else self.day

    def _chain(self, key, nv, k, m):
        """1차 네이버, 2차 KIS(교차검증 겸 대체), 3차 manual."""
        self.chains[key] = [(n, x) for n, x in (("네이버", nv), ("KIS", k), ("수동", m)) if x is not None]
        return self.chains[key][0][1] if self.chains[key] else None

    def cross(self, key, item, d):
        """체인의 앞 두 소스를 기준일 d 값으로 비교."""
        c = self.chains.get(key) or []
        return check(item, c[0][0] if c else "소스", _on(c[0][1], d) if c else None,
                     c[1][0] if len(c) > 1 else "2차", _on(c[1][1], d) if len(c) > 1 else None, jo)

    def frgn(self, dates):
        """코스피 외인 순매수(원)."""
        nv = self.get(f"nv_frgn{dates[0]}", lambda: naver.trend_series("KOSPI", dates, self.day))
        k = self.get("kis_frgn", lambda: kis.foreign_net_kospi(self.day)) if kis.configured() else None
        return self._chain("frgn", nv, self.upto(k), manual.series(self.m, "frgn_net"))

    def fut(self, dates):
        """외인 코스피200 선물 순매수(원). KIS는 당일분만 주므로 매일 호출해 쌓는다."""
        nv = self.get(f"nv_fut{dates[0]}", lambda: naver.trend_series("FUT", dates, self.day))
        k = self.get("kis_fut", lambda: kis_fut_series(self.last_trading_day())) if kis.configured() else None
        return self._chain("fut", nv, k, manual.series(self.m, "fut_frgn_net"))


def kis_fut_series(day):
    """KIS는 당일분만 주므로 날짜별로 쌓는다. 잠정값은 커밋 대상에 저장하지 않는다."""
    try:
        hist = json.loads(KIS_FUT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    t = kis.fut_foreign_today()
    if t["net"] or t["qty"]:  # 장 시작 전 등 전부 0이면 미집계로 보고 저장하지 않음
        hist[str(day)] = t["net"]
    else:
        log.warning("KIS 선물 %s: 전부 0 → 저장 생략", day)
    if KIS_SAVE_ALWAYS or not is_provisional(day):
        KIS_FUT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        KIS_FUT_CACHE.write_text(json.dumps(hist), encoding="utf-8")
    return pd.Series({dt.date.fromisoformat(k): v for k, v in hist.items()}).sort_index()


# ---------- 램프 ----------
def lamp01(c):
    k = c.kospi()
    if k is None or len(k) < 2:
        return na()
    chg = k.pct_change().dropna()
    crash = [d for d in chg.index[-CRASH_LOOKBACK:] if chg[d] <= CRASH]
    nv, yf = c.kospi_sources()
    pt = lambda x: f"{x:,.2f}"  # noqa: E731
    if not crash:
        d = chg.index[-1]
        ck = check(f"코스피 {md(d)}", "네이버", _on(nv, d), "야후", _on(yf, d), pt)
        return {"s": "warn", "val": f"최근 {CRASH_LOOKBACK}거래일 급락일 없음",
                "raw": {"kospi_chg": float(chg.iloc[-1])}, "verify": ck["r"], "checks": [ck]}
    d0 = crash[-1]
    since = len(chg.index) - 1 - list(chg.index).index(d0)
    raw = {"crash_day": str(d0), "kospi_chg": float(chg[d0]), "days_since": since}

    days = c.trading_days()
    i0 = days.index(d0)
    f = c.frgn(days[max(0, i0 - 20):i0 + 1])
    prior = f[[d < d0 for d in f.index]].tail(20) if f is not None else []
    if f is None or d0 not in f.index or len(prior) < 20:
        n = 0 if f is None else len(prior)
        return na(f"급락일 {md(d0)} {chg[d0] * 100:+.2f}% · 외인 수급 미수집({n}/20일)", raw)

    net = float(f[d0])
    avg20 = float(prior.abs().mean())
    sell = max(0.0, -net)
    ratio = sell / avg20 if avg20 else 0.0
    raw.update({"frgn": net, "avg20_abs": avg20, "ratio": ratio})
    s = "ok" if sell < avg20 else "no"
    if since > STALE_DAYS:
        s = "warn"
    tail = f" · {since}일 경과" if since else ""
    ck_k = check(f"코스피 {md(d0)}", "네이버", _on(nv, d0), "야후", _on(yf, d0), pt)
    ck_f = c.cross("frgn", f"외인 현물 {md(d0)}", d0)
    return {"s": s, "val": f"{md(d0)} {jo(net)} · 20일평균 {ratio:.1f}배{tail}", "raw": raw,
            "verify": "mismatch" if ck_k["r"] == "mismatch" else ck_f["r"], "checks": [ck_k, ck_f]}


def lamp02(c):
    days = c.trading_days()
    if not days:
        return na()
    d = days[-1]
    f = c.fut(days[-5:])
    if f is None or d not in f.index:
        return na("외인 선물 미수집")
    w = f[[x <= d for x in f.index]].tail(5)
    if len(w) < 5:
        return na(f"외인 선물 이력 부족 ({len(w)}/5일)")
    cum5, today = float(w.sum()), float(w.iloc[-1])
    s = "no" if cum5 <= 0 else ("ok" if today >= 0 else "warn")
    ck = c.cross("fut", f"외인 선물 {md(d)}", d)
    return {"s": s, "val": f"5일 누적 {jo(cum5)} · 당일 {jo(today)}",
            "raw": {"date": str(d), "cum5": cum5, "today": today,
                    "window": {str(k): float(v) for k, v in w.items()}, "oi": None},
            "verify": ck["r"], "checks": [ck]}


def _on(s, d):
    return float(s[d]) if s is not None and d in s.index else None


def lamp03(c):
    d = c.last_trading_day()
    k = (c.m.get("k200") or {}).get(str(d)) or {}
    spot_n = _on(c.get("nv_k200", lambda: naver.daily_close("KPI200", 10)), d)
    fut_n = _on(c.get("nv_fut", lambda: naver.daily_close("FUT", 10)), d)
    spot_k = fut_k = None
    if kis.configured():
        ks = c.get("kis_k200", lambda: kis.k200_daily(c.day))
        spot_k = _on(ks, d)
        # KIS 선물 현재가에는 날짜가 없다 → d가 KIS 최신 거래일일 때만 같은 날 값으로 인정
        if c.day == dt.date.today() and ks is not None and ks.index[-1] == d:
            fut_k = c.get("kis_fut_px", kis.fut_price)
    spot_2 = (spot_k, "KIS") if spot_k is not None else (k.get("spot"), "수동")
    fut_2 = (fut_k, "KIS") if fut_k is not None else (k.get("fut"), "수동")
    spot = spot_n if spot_n is not None else spot_2[0]
    fut = fut_n if fut_n is not None else fut_2[0]
    if spot is None or fut is None:
        return na("코스피200 현물·선물 종가 미수집")
    basis = fut - spot
    s = "ok" if basis >= 0 else ("warn" if basis >= -1.0 else "no")
    theory = k.get("theory")
    tail = f" · 이론 {theory:+.2f}" if theory is not None else ""
    month = c.get("nv_month", naver.futures_month)
    pt = lambda x: f"{x:,.2f}"  # noqa: E731
    checks = [check(f"코스피200 현물 {md(d)}", "네이버", spot_n, spot_2[1], spot_2[0], pt),
              check(f"코스피200 선물 {md(d)}", "네이버", fut_n, fut_2[1], fut_2[0], pt)]
    return {"s": s, "val": f"시장 {basis:+.2f}pt{tail}",
            "raw": {"date": str(d), "spot": spot, "fut": fut, "basis": basis, "theory": theory, "month": month},
            "verify": worst(checks), "checks": checks}


def lamp04(c):
    x = c.get("sox", us.sox)
    if x is None:
        return na()
    d = dt.date.fromisoformat(x["date"])
    second = _on(c.get("nv_sox", lambda: naver.world_close(".SOX")), d)
    s = "ok" if x["close"] > x["ma20"] else "no"
    ck = check(f"SOX {md(d)}", "야후", x["close"], "네이버", second, lambda v: f"{v:,.2f}")
    return {"s": s, "val": f"{x['close']:,.2f} ({md(d)}) · 20MA {x['ma20']:,.2f}",
            "raw": {**x, "close_2": second}, "verify": ck["r"], "checks": [ck]}


def _local_dram(day, name):
    """data/ 이력에서 30~37일 전 DDR5 가격."""
    for back in range(30, 38):
        p = DATA / f"{day - dt.timedelta(days=back)}.json"
        try:
            v = json.loads(p.read_text(encoding="utf-8"))["lamps"]["05"]["raw"]["dx"][name]
            if v:
                return p.stem, float(v)
        except Exception:
            continue
    return None, None


def lamp05(c):
    name = "DDR5 16Gb (2Gx8) 4800/5600"
    dx = c.get("dx", dram.dramexchange) or {}
    mi = c.get("mi", dram.memoryindex) or {}
    p1 = dx.get(name)
    p2 = (mi.get(name) or {}).get("spot")
    price = p1 if p1 is not None else p2
    if price is None:
        return na()
    old_d, old_p = _local_dram(c.day, name)
    if old_p:
        chg, src = price / old_p - 1, f"local {old_d}"
    else:
        chg, src = (mi.get(name) or {}).get("chg30"), "memoryindex 30d"
    raw = {"price": price, "dx": dx, "mi": mi, "chg30": chg, "chg30_src": src}
    if chg is None:
        return na(f"DDR5 16Gb ${price:.2f} · 30일 변화 미수집", raw)
    asof = dx.get("asof")
    when = f" ({md(dt.datetime.fromisoformat(asof))})" if asof else ""
    ck = check(f"DDR5 16Gb{when}", "DRAMeXchange", p1, "memoryindex", p2, lambda v: f"${v:.2f}")
    return {"s": "ok" if chg > 0 else "no", "val": f"DDR5 16Gb ${price:.2f}{when} · 30일 {chg * 100:+.1f}%",
            "raw": raw, "verify": ck["r"], "checks": [ck]}


def hy_series(c):
    """FRED 최신값. 실패하면(국내 PC에서 막힘) 커밋된 이력 사용."""
    s = c.get("hy_live", fred.hy_oas)
    if s is not None:
        HY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HY_CACHE.write_text(json.dumps({str(k): float(v) for k, v in s.tail(60).items()}), encoding="utf-8")
        return s, "FRED"
    try:
        d = json.loads(HY_CACHE.read_text(encoding="utf-8"))
        return pd.Series({dt.date.fromisoformat(k): v for k, v in d.items()}).sort_index(), "FRED 이력"
    except Exception:
        return None, None


def lamp06(c):
    """ICE BofA HY OAS 1주(5관측) 변화: 축소 ok, 확대 no. 비면 LLM 값을 쓴다."""
    s, src = hy_series(c)
    s = c.upto(s)
    if s is None or len(s) < 6 or (c.day - s.index[-1]).days > HY_MAX_AGE:
        return na()
    d, v, bp = s.index[-1], float(s.iloc[-1]), (float(s.iloc[-1]) - float(s.iloc[-6])) * 100
    ck = check(f"HY OAS {md(d)}", src, v, fmt=lambda x: f"{x:.2f}%")
    st = "ok" if bp < -0.5 else ("no" if bp > 0.5 else "warn")
    return {"s": st, "val": f"OAS {v:.2f}% ({md(d)}) · 1주 {bp:+.0f}bp",
            "raw": {"date": str(d), "oas": v, "chg_1w_bp": bp, "src": src}, "verify": ck["r"], "checks": [ck]}


def lamp07(c):
    """공개 자동 소스 없음(네이버 페이지 종료, KRX 로그인 필요) → manual.json, 오래되면 LLM 값."""
    m = manual.series(c.m, "frgn_ratio")
    if m is None:
        return na("외인 시총 비중 미입력 (data/manual.json frgn_ratio)")
    d, r = m.index[-1], float(m.iloc[-1])
    age = (c.day - d).days
    s = "ok" if r <= 33.0 else ("warn" if r <= 35.0 else "no")
    stale = " · 갱신 필요" if age > 7 else ""
    ck = check(f"외인 시총 비중 {md(d)}", "수동 입력", r, fmt=lambda x: f"{x:.2f}%")
    return {"s": s, "val": f"{r:.2f}% ({md(d)} 기준){stale} · 바닥 32~33%",
            "raw": {"ratio": r, "asof": str(d), "age_days": age, "src": "manual"},
            "verify": ck["r"], "checks": [ck]}


def lamp08(c):
    return {"s": "warn", "val": "LLM 결과 없음", "raw": {}, "verify": None}


LAMPS = {f"{i:02d}": fn for i, fn in enumerate(
    [lamp01, lamp02, lamp03, lamp04, lamp05, lamp06, lamp07, lamp08], 1)}


def safe(fn, c):
    try:
        return fn(c)
    except Exception as e:
        log.exception("lamp %s 오류", fn.__name__)
        return na(f"오류: {type(e).__name__}", {"error": str(e)})


# ---------- 단계 ----------
def judge(lamps):
    st = {k: v["s"] for k, v in lamps.items()}
    if all(st[k] == "ok" for k in ("01", "02", "03", "04", "05")):
        stage = "s2"
    elif all(st[k] == "ok" for k in ("01", "02", "03")):
        stage = "s1"
    else:
        stage = "wait"
    if st["01"] == "no":
        stage = "wait"
    veto = st["05"] == "no" or st["08"] == "no"
    return stage, veto, sum(v == "ok" for v in st.values())


def validate_llm(d):
    """루틴이 쓴 latest.json 형식 검사. 틀리면 ValueError (화면에 깨진 값을 올리지 않는다)."""
    def text(x, name, n=4000):
        if not isinstance(x, str) or len(x) > n:
            raise ValueError(f"{name}: 문자열({n}자 이내)이 아님")
    if not isinstance(d, dict):
        raise ValueError("최상위가 객체가 아님")
    dt.date.fromisoformat(d.get("date", ""))
    text(d.get("wallst_note", ""), "wallst_note")
    for k, l in (d.get("lamps") or {}).items():
        if k not in ("06", "07", "08") or l.get("s") not in STATE_KO:
            raise ValueError(f"lamps.{k}: 램프 번호(06·07·08) 또는 상태 오류")
        for f in ("val", "src", "asof"):
            text(l.get(f, ""), f"lamps.{k}.{f}", 300)
    for n in d.get("next") or []:
        text(n.get("d"), "next.d", 40)
        text(n.get("n"), "next.n", 200)
    for r in d.get("candidates") or []:
        for f in ("slot", "name", "why", "trigger"):
            text(r.get(f), f"candidates.{f}", 1000)
    return d


def load_llm():
    """(LLM 결과, 사용 가능 여부, 오류)."""
    try:
        d = validate_llm(json.loads(LLM_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}, False, None
    except Exception as ex:
        log.warning("LLM 결과 오류: %s", ex)
        return {}, False, f"LLM 결과 형식 오류: {str(ex)[:120]}"
    age = (dt.date.today() - dt.date.fromisoformat(d["date"])).days
    return d, age <= LLM_MAX_AGE, None


def apply_llm(sig):
    """06은 로컬이 비었을 때, 07은 수동값보다 새로울 때, 08은 항상 LLM 값 사용 후 재판정."""
    llm = sig.get("llm") or {}
    for k, v in (llm.get("lamps") or {}).items() if llm.get("fresh") else ():
        cur = sig["lamps"].get(k) or {}
        if (k == "06" and cur.get("s") != "na" and (cur.get("raw") or {}).get("by") != "llm") or \
                (k == "07" and (cur.get("raw") or {}).get("by") != "llm"
                 and v.get("asof", "") <= (cur.get("raw") or {}).get("asof", "")):
            continue
        sig["lamps"][k] = {"s": v["s"], "val": v.get("val", ""), "verify": v.get("verify", "llm"),
                           "raw": {"src": v.get("src"), "asof": v.get("asof"), "by": "llm"},
                           "checks": [{"item": f"{v.get('asof', '')} LLM".strip(), "a": v.get("src") or "출처 미기재",
                                       "b": None, "r": v.get("verify", "llm"), "gap": None}]}
    sig["stage"], sig["veto"], sig["ok_count"] = judge(sig["lamps"])
    return sig


def build(day, c=None, tape=None):
    c = c or Ctx(day)
    lamps = {k: safe(fn, c) for k, fn in LAMPS.items()}
    if tape is None:
        try:
            tape, _ = us.tape()
        except Exception as ex:
            log.warning("테이프 실패: %s", ex)
            tape = {}
    asof = c.last_trading_day()
    k = c.kospi()
    tape = dict(tape)
    if k is not None and len(k) >= 2:  # 야후는 코스피 반영이 하루 늦을 때가 있어 판정과 같은 소스로 표시
        tape["kospi"] = f"{k.iloc[-1]:,.2f} {(k.iloc[-1] / k.iloc[-2] - 1) * 100:+.2f}%"
    llm, fresh, llm_err = load_llm()
    errors = c.errors + [f"테이프 {n}: 수집 실패" for n, x in tape.items() if x is None]
    errors += [f"{n}: 직전 수집 실패" for n in ("sox", "nv_sox", "dx", "mi", "yf_kospi")
               if n in c._c and c._c[n] is None and not any(e.startswith(f"{n}:") for e in errors)]
    if llm_err:
        errors.append(llm_err)
    sig = {"date": str(asof), "provisional": is_provisional(asof), "stage": None, "veto": None, "ok_count": None,
           "lamps": lamps, "tape": tape, "next": (llm.get("next") if fresh else None) or c.m.get("next", []),
           "llm": {**llm, "fresh": fresh}, "errors": errors}
    return apply_llm(sig)


# ---------- HTML ----------
STAGE = {"wait": ("대기", "--no"), "s1": ("1단계", "--warn"), "s2": ("2단계", "--ok")}
STATE_KO = {"ok": "충족", "warn": "주의", "no": "미충족", "na": "미수집"}
VERIFY_KO = {"2src": "2소스 일치", "1src": "1소스", "mismatch": "소스 불일치", "llm": "LLM", None: "—"}
RING_LEN = 2 * 3.14159265 * 52  # template 링 반지름 52


def verify_rows(sig, e):
    rows = []
    for k, l in sig["lamps"].items():
        for ck in l.get("checks") or []:
            r = VERIFY_KO.get(ck.get("r"), ck.get("r"))
            if ck.get("gap") is not None and ck.get("r") == "mismatch":
                r = f"{r} (차 {ck['gap'] * 100:.1f}%)"
            rows.append(f"<tr><td>{e(k)} {e(ck.get('item'))}</td><td>{e(ck.get('a'))}</td><td>{e(ck.get('b'))}</td>"
                        f"<td>{e(r)}</td></tr>")
    return "".join(rows) or '<tr><td colspan="4" class="muted">기록 없음</td></tr>'


def render(sig):
    e = lambda x: html.escape(str(x if x is not None else "—"))  # noqa: E731
    label, color = STAGE[sig["stage"]]
    llm = sig.get("llm") or {}
    when = f" ({llm['date']} 기준)" if llm.get("date") else ""
    note = llm.get("wallst_note") or "LLM 결과 없음"
    v = {
        "date": sig["date"], "stage": sig["stage"], "stage_label": label,
        "stage_color": f"var({color})", "ok_count": sig["ok_count"],
        "ring_dash": f"{RING_LEN * sig['ok_count'] / 8:.1f} {RING_LEN:.1f}",
        "veto": "on" if sig["veto"] else "",
        "veto_text": "무효 램프 점등 — D램 또는 이익 컨센서스 꺾임" if sig["veto"] else "",
        "wallst_note": note + when + ("" if llm.get("fresh", True) else " · 오래된 내용"),
        "verdict": f"{label} · 충족 {sig['ok_count']}/8",
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "prov": "on" if sig.get("provisional") else "",
    }
    for k, l in sig["lamps"].items():
        v[f"lamp{k}_s"] = l["s"]
        v[f"lamp{k}_state"] = STATE_KO[l["s"]]
        v[f"lamp{k}_val"] = l["val"]
        v[f"lamp{k}_verify"] = VERIFY_KO.get(l.get("verify"), l.get("verify"))
    for k in ("dow", "sox", "vix", "us10y", "dxy", "wti", "kospi", "usdkrw"):
        v[f"tape_{k}"] = sig["tape"].get(k)
    v["next_rows"] = "".join(f"<li><b>{e(n.get('d'))}</b> {e(n.get('n'))}</li>" for n in sig["next"]) \
        or "<li class=\"muted\">등록된 일정 없음</li>"
    v["cand_rows"] = "".join(f"<tr><td>{e(r.get('slot'))}</td><td>{e(r.get('name'))}</td><td>{e(r.get('why'))}</td>"
                             f"<td>{e(r.get('trigger'))}</td></tr>" for r in llm.get("candidates") or []) \
        or '<tr><td colspan="4" class="muted">LLM 결과 없음</td></tr>'
    v["cand_when"] = f"LLM {llm['date']} 기준 · 종목 추천 아님" if llm.get("date") else "종목 추천 아님"
    v["verify_rows"] = verify_rows(sig, e)
    errs = sig.get("errors") or []
    v["error_rows"] = "".join(f"<li>{e(x)}</li>" for x in errs) or '<li class="muted">이번 실행 실패 없음</li>'

    t = TEMPLATE.read_text(encoding="utf-8")
    raw_keys = {"next_rows", "cand_rows", "verify_rows", "error_rows"}
    return re.sub(r"\{\{(\w+)\}\}", lambda m: v.get(m[1], "") if m[1] in raw_keys else e(v.get(m[1])), t)


def fallback_html(sig, err):
    rows = "".join(f"<li>{k} {l['s']} — {html.escape(str(l['val']))}</li>"
                   for k, l in (sig or {}).get("lamps", {}).items())
    return (f"<!doctype html><meta charset=utf-8><title>진입 레이더</title>"
            f"<body style='font-family:sans-serif;background:#fff;color:#111;padding:16px'>"
            f"<h1>진입 레이더 {sig.get('date') if sig else ''}</h1><p>템플릿 렌더 실패: "
            f"{html.escape(err)}</p><ul>{rows}</ul></body>")


def write_html(sig):
    OUT.mkdir(exist_ok=True)
    try:
        page = render(sig)
    except Exception as ex:
        page = fallback_html(sig, f"{type(ex).__name__}: {ex}")
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / "signals.json").write_text(json.dumps(sig, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def push():
    if not (OUT / ".git").exists():
        print("out/.git 없음 — push 생략")
        return
    for cmd in (["add", "-A"], ["commit", "-m", f"radar {dt.date.today()}"], ["push"]):
        subprocess.run(["git", "-C", str(OUT), *cmd], check=False)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--lamp")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--force", action="store_true", help="이미 있는 과거 기록도 다시 쓴다")
    ap.add_argument("--check-llm", metavar="PATH")
    a = ap.parse_args()

    if a.check_llm:
        validate_llm(json.loads(Path(a.check_llm).read_text(encoding="utf-8")))
        print("LLM 결과 형식 정상")
        return
    if a.lamp:
        r = safe(LAMPS[a.lamp.zfill(2)], Ctx(a.date))
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return

    DATA.mkdir(exist_ok=True)
    sig = None
    try:
        if a.render:
            p = DATA / f"{a.date}.json"
            sig = apply_llm(json.loads((p if p.exists() else OUT / "signals.json").read_text(encoding="utf-8")))
        else:
            sig = build(a.date)
        path = DATA / f"{sig['date']}.json"
        final = not sig.get("provisional")
        # 지난 거래일 기록은 그날 저녁 값을 보존한다 (다음 날 아침 재실행으로 덮어쓰지 않음)
        if final and (a.force or a.render or not path.exists() or sig["date"] == str(dt.date.today())):
            path.write_text(json.dumps(sig, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except Exception:
        traceback.print_exc()
        sig = sig or {"date": str(a.date), "stage": "wait", "veto": False, "ok_count": 0,
                      "lamps": {k: na("수집 실패") for k in LAMPS}, "tape": {}, "next": [], "llm": {},
                      "errors": ["전체 실행 실패: radar.log 확인"]}
    write_html(sig)
    print(f"{sig['date']} stage={sig['stage']} veto={sig['veto']} ok={sig['ok_count']}/8"
          f"{' 잠정' if sig.get('provisional') else ''}")
    for k, l in sig["lamps"].items():
        print(f"  {k} {l['s']:4} {l['val']}  [{l.get('verify')}]")
    for x in sig.get("errors") or []:
        print(f"  실패: {x}")
    if a.push:
        push()


if __name__ == "__main__":
    main()
