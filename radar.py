"""진입 레이더: 수집 → 판정 → signals.json → index.html  (RADAR_SPEC.md)

python radar.py                 # 전체 실행 (data/YYYY-MM-DD.json, out/index.html)
python radar.py --lamp 01       # 램프 하나만 계산해 출력 (저장 안 함)
python radar.py --render        # 오늘 json(LLM 머지 후)으로 HTML만 다시 생성
python radar.py --date 2026-11-04 --push
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

from sources import dram, kis, manual, naver, us  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA, OUT, TEMPLATE = ROOT / "data", ROOT / "out", ROOT / "template" / "index.html"
KIS_FUT_CACHE = DATA / "kis_fut.json"  # 커밋 대상: Actions 러너에서도 이력 유지

DATA.mkdir(exist_ok=True)
logging.basicConfig(filename=DATA / "radar.log", encoding="utf-8", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radar")

CRASH = -0.02          # 01 급락일 기준
CRASH_LOOKBACK = 60    # 직전 급락일 탐색 범위(거래일)
STALE_DAYS = 5         # 01 판정 유지 기간(거래일)


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


def pick(primary, secondary):
    """1차 우선 채택, 없으면 2차. (series, verify) 반환."""
    if primary is None:
        return secondary, ("1src" if secondary is not None else None)
    if secondary is None:
        return primary, "1src"
    common = primary.index.intersection(secondary.index)
    v = verify(float(primary[common[-1]]), float(secondary[common[-1]])) if len(common) else "1src"
    return primary, v


class Ctx:
    """수집 결과 캐시. 램프마다 필요한 것만 수집한다."""

    def __init__(self, day):
        self.day = day
        self.m = manual.load()
        self._c = {}

    def get(self, key, fn):
        """소스 호출 1회 + 캐시. 실패는 None + 로그 (다음 소스/램프로 진행)."""
        if key not in self._c:
            try:
                self._c[key] = fn()
            except Exception as ex:
                log.warning("source %s 실패: %s: %s", key, type(ex).__name__, ex)
                self._c[key] = None
        return self._c[key]

    def upto(self, s):
        return s[[d <= self.day for d in s.index]] if s is not None else None

    def kospi(self):
        return pick(self.upto(self.get("nv_kospi", lambda: naver.daily_close("KOSPI"))),
                    self.upto(self.get("yf_kospi", us.kospi_yf)))

    def trading_days(self):
        s, _ = self.kospi()
        return list(s.index) if s is not None else []

    def last_trading_day(self):
        d = self.trading_days()
        return d[-1] if d else self.day

    def _chain(self, nv, k, m):
        """1차 네이버, 2차 KIS(교차검증 겸 대체), 3차 manual."""
        if nv is None:
            nv, k = (k, None) if k is not None else (m, None)
        return pick(nv, k if k is not None else m)

    def frgn(self, dates):
        """코스피 외인 순매수(원)."""
        nv = self.get(f"nv_frgn{dates[0]}", lambda: naver.trend_series("KOSPI", dates, self.day))
        k = self.get("kis_frgn", lambda: kis.foreign_net_kospi(self.day)) if kis.configured() else None
        return self._chain(nv, self.upto(k), manual.series(self.m, "frgn_net"))

    def fut(self, dates):
        """외인 코스피200 선물 순매수(원). KIS는 당일분만 주므로 매일 호출해 로컬에 쌓는다."""
        nv = self.get(f"nv_fut{dates[0]}", lambda: naver.trend_series("FUT", dates, self.day))
        k = self.get("kis_fut", lambda: kis_fut_series(self.last_trading_day())) if kis.configured() else None
        return self._chain(nv, k, manual.series(self.m, "fut_frgn_net"))


def kis_fut_series(day):
    """KIS는 당일분만 주므로 data/cache/kis_fut.json에 날짜별로 쌓는다."""
    try:
        hist = json.loads(KIS_FUT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    t = kis.fut_foreign_today()
    if t["net"] or t["qty"]:  # 장 시작 전 등 전부 0이면 미집계로 보고 저장하지 않음
        hist[str(day)] = t["net"]
    else:
        log.warning("KIS 선물 %s: 전부 0 → 저장 생략", day)
    KIS_FUT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    KIS_FUT_CACHE.write_text(json.dumps(hist), encoding="utf-8")
    return pd.Series({dt.date.fromisoformat(k): v for k, v in hist.items()}).sort_index()


# ---------- 램프 ----------
def lamp01(c):
    k, kv = c.kospi()
    if k is None or len(k) < 2:
        return na()
    chg = k.pct_change().dropna()
    crash = [d for d in chg.index[-CRASH_LOOKBACK:] if chg[d] <= CRASH]
    if not crash:
        return {"s": "warn", "val": f"최근 {CRASH_LOOKBACK}거래일 급락일 없음",
                "raw": {"kospi_chg": float(chg.iloc[-1])}, "verify": kv}
    d0 = crash[-1]
    since = len(chg.index) - 1 - list(chg.index).index(d0)
    raw = {"crash_day": str(d0), "kospi_chg": float(chg[d0]), "days_since": since}

    days = c.trading_days()
    i0 = days.index(d0)
    f, fv = c.frgn(days[max(0, i0 - 20):i0 + 1])
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
    return {"s": s, "val": f"{md(d0)} {jo(net)} · 20일평균 {ratio:.1f}배{tail}",
            "raw": raw, "verify": fv if kv != "mismatch" else "mismatch"}


def lamp02(c):
    days = c.trading_days()
    if not days:
        return na()
    d = days[-1]
    f, fv = c.fut(days[-5:])
    if f is None or d not in f.index:
        return na("외인 선물 미수집")
    w = f[[x <= d for x in f.index]].tail(5)
    if len(w) < 5:
        return na(f"외인 선물 이력 부족 ({len(w)}/5일)")
    cum5, today = float(w.sum()), float(w.iloc[-1])
    s = "no" if cum5 <= 0 else ("ok" if today >= 0 else "warn")
    return {"s": s, "val": f"5일 누적 {jo(cum5)} · 당일 {jo(today)}",
            "raw": {"date": str(d), "cum5": cum5, "today": today,
                    "window": {str(k): float(v) for k, v in w.items()}, "oi": None},
            "verify": fv}


def _on(s, d):
    return float(s[d]) if s is not None and d in s.index else None


def lamp03(c):
    d = c.last_trading_day()
    k = (c.m.get("k200") or {}).get(str(d)) or {}
    spot_n = _on(c.get("nv_k200", lambda: naver.daily_close("KPI200", 10)), d)
    fut_n = _on(c.get("nv_fut", lambda: naver.daily_close("FUT", 10)), d)
    spot = spot_n if spot_n is not None else k.get("spot")
    fut = fut_n if fut_n is not None else k.get("fut")
    if spot is None or fut is None:
        return na("코스피200 현물·선물 종가 미수집")
    basis = fut - spot
    s = "ok" if basis >= 0 else ("warn" if basis >= -1.0 else "no")
    theory = k.get("theory")
    tail = f" · 이론 {theory:+.2f}" if theory is not None else ""
    month = c.get("nv_month", naver.futures_month)
    vs = [verify(spot_n, k.get("spot")), verify(fut_n, k.get("fut"))]
    return {"s": s, "val": f"시장 {basis:+.2f}pt{tail}",
            "raw": {"date": str(d), "spot": spot, "fut": fut, "basis": basis, "theory": theory, "month": month},
            "verify": "mismatch" if "mismatch" in vs else ("2src" if vs == ["2src", "2src"] else "1src")}


def lamp04(c):
    x = c.get("sox", us.sox)
    if x is None:
        return na()
    s = "ok" if x["close"] > x["ma20"] else "no"
    return {"s": s, "val": f"{x['close']:,.2f} · 20MA {x['ma20']:,.2f}",
            "raw": x, "verify": verify(x["close"], x["close_2"])}


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
    return {"s": "ok" if chg > 0 else "no", "val": f"DDR5 16Gb ${price:.2f} · 30일 {chg * 100:+.1f}%",
            "raw": raw, "verify": verify(p1, p2)}


def lamp06(c):
    return na()


def lamp07(c):
    """공개 자동 소스 없음(네이버 해당 페이지 종료, 금투협 접속 불가) → manual.json 최신값, 주 1회 갱신."""
    m = manual.series(c.m, "frgn_ratio")
    if m is None:
        return na("외인 시총 비중 미입력 (data/manual.json frgn_ratio)")
    d, r = m.index[-1], float(m.iloc[-1])
    age = (c.day - d).days
    s = "ok" if r <= 33.0 else ("warn" if r <= 35.0 else "no")
    stale = " · 갱신 필요" if age > 7 else ""
    return {"s": s, "val": f"{r:.2f}% ({md(d)} 기준){stale} · 바닥 32~33%",
            "raw": {"ratio": r, "asof": str(d), "age_days": age, "src": "manual"}, "verify": "1src"}


def lamp08(c):
    return {"s": "warn", "val": "LLM 미채움", "raw": {}, "verify": None}


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


def apply_llm(sig):
    """llm.lamps 에 06·08 등이 채워져 있으면 그 값 사용 후 재판정."""
    for k, v in (sig.get("llm", {}).get("lamps") or {}).items():
        if k in sig["lamps"] and v.get("s"):
            sig["lamps"][k] = {**sig["lamps"][k], **v, "verify": v.get("verify", "llm")}
    sig["stage"], sig["veto"], sig["ok_count"] = judge(sig["lamps"])
    return sig


def build(day):
    c = Ctx(day)
    lamps = {k: safe(fn, c) for k, fn in LAMPS.items()}
    try:
        tape, _ = us.tape()
    except Exception:
        tape = {}
    old = DATA / f"{day}.json"
    llm = {"wallst_note": "", "candidates_updates": [], "verdict": ""}
    if old.exists():  # 재실행해도 LLM이 채운 블록은 보존
        try:
            llm = json.loads(old.read_text(encoding="utf-8")).get("llm", llm)
        except Exception:
            pass
    sig = {"date": str(day), "stage": None, "veto": None, "ok_count": None, "lamps": lamps,
           "tape": tape, "next": c.m.get("next", []), "llm": llm}
    return apply_llm(sig)


# ---------- HTML ----------
STAGE = {"wait": ("대기", "--no"), "s1": ("1단계", "--warn"), "s2": ("2단계", "--ok")}
STATE_KO = {"ok": "충족", "warn": "주의", "no": "미충족", "na": "미수집"}
VERIFY_KO = {"2src": "2소스 일치", "1src": "1소스", "mismatch": "소스 불일치", "llm": "LLM", None: "—"}
RING_LEN = 2 * 3.14159265 * 52  # template 링 반지름 52


def render(sig):
    e = lambda x: html.escape(str(x if x is not None else "—"))  # noqa: E731
    label, color = STAGE[sig["stage"]]
    v = {
        "date": sig["date"], "stage": sig["stage"], "stage_label": label,
        "stage_color": f"var({color})", "ok_count": sig["ok_count"],
        "ring_dash": f"{RING_LEN * sig['ok_count'] / 8:.1f} {RING_LEN:.1f}",
        "veto": "on" if sig["veto"] else "",
        "veto_text": "무효 램프 점등 — D램 또는 이익 컨센서스 꺾임" if sig["veto"] else "",
        "wallst_note": sig["llm"].get("wallst_note") or "LLM 미채움",
        "verdict": sig["llm"].get("verdict") or f"{label} · 충족 {sig['ok_count']}/8",
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
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

    t = TEMPLATE.read_text(encoding="utf-8")
    raw_keys = {"next_rows"}
    t = re.sub(r"\{\{(\w+)\}\}", lambda m: v.get(m[1], "") if m[1] in raw_keys else e(v.get(m[1])), t)
    for u in sig["llm"].get("candidates_updates") or []:
        if u.get("id") and u.get("html"):
            t = re.sub(rf'(<tr id="{re.escape(u["id"])}"[^>]*>).*?(</tr>)',
                       lambda m: m[1] + u["html"] + m[2], t, flags=re.S)
    return t


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
    a = ap.parse_args()

    if a.lamp:
        r = safe(LAMPS[a.lamp.zfill(2)], Ctx(a.date))
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return

    DATA.mkdir(exist_ok=True)
    path = DATA / f"{a.date}.json"
    sig = None
    try:
        if a.render:
            sig = apply_llm(json.loads(path.read_text(encoding="utf-8")))
        else:
            sig = build(a.date)
        path.write_text(json.dumps(sig, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except Exception:
        traceback.print_exc()
        sig = sig or {"date": str(a.date), "stage": "wait", "veto": False, "ok_count": 0,
                      "lamps": {k: na("수집 실패") for k in LAMPS}, "tape": {}, "next": [], "llm": {}}
    write_html(sig)
    print(f"{sig['date']} stage={sig['stage']} veto={sig['veto']} ok={sig['ok_count']}/8")
    for k, l in sig["lamps"].items():
        print(f"  {k} {l['s']:4} {l['val']}  [{l.get('verify')}]")
    if a.push:
        push()


if __name__ == "__main__":
    main()
