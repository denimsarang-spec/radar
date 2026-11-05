"""yfinance(1차) / stooq(2차) 수집: SOX 20일선, 코스피 지수 보조, 테이프."""
import io

import pandas as pd
import requests
import yfinance as yf


def yf_close(ticker, period="3mo"):
    """일별 종가 Series (index=date). 실패 시 None."""
    try:
        h = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if h.empty:
            return None
        s = h["Close"].dropna()
        s.index = [d.date() for d in s.index]
        return s
    except Exception:
        return None


def stooq_close(symbol):
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={symbol}&i=d", timeout=15)
        df = pd.read_csv(io.StringIO(r.text))
        if "Close" not in df.columns or df.empty:
            return None
        s = df.set_index(pd.to_datetime(df["Date"]).dt.date)["Close"].dropna()
        return s.tail(90)
    except Exception:
        return None


def sox():
    """SOX 종가·20MA. 반환 dict 또는 None."""
    s1 = yf_close("^SOX")
    s2 = stooq_close("^sox")
    if s1 is None or len(s1) < 20:
        return None
    out = {
        "date": str(s1.index[-1]),
        "close": float(s1.iloc[-1]),
        "ma20": float(s1.tail(20).mean()),
        "chg": float(s1.iloc[-1] / s1.iloc[-2] - 1),
        "close_2": None,
    }
    if s2 is not None and s1.index[-1] in s2.index:
        out["close_2"] = float(s2.loc[s1.index[-1]])
    return out


def kospi_yf():
    """yfinance ^KS11 일별 종가 (네이버 교차검증용)."""
    return yf_close("^KS11")


def _fmt_pct(x):
    return f"{x * 100:+.2f}%"


def tape():
    """테이프 표시용 문자열 dict. 항목별 실패는 None."""
    def last2(t):
        s = yf_close(t, "1mo")
        if s is None or len(s) < 2:
            return None, None
        return float(s.iloc[-1]), float(s.iloc[-1] / s.iloc[-2] - 1)

    out, raw = {}, {}
    for key, t in [("dow", "^DJI"), ("sox", "^SOX"), ("vix", "^VIX"), ("us10y", "^TNX"),
                   ("dxy", "DX-Y.NYB"), ("wti", "CL=F"), ("kospi", "^KS11"), ("usdkrw", "KRW=X")]:
        v, c = last2(t)
        raw[key] = {"v": v, "chg": c}
        if v is None:
            out[key] = None
        elif key in ("dow", "sox"):
            out[key] = _fmt_pct(c)
        elif key == "vix":
            out[key] = f"{c * 100:+.0f}%"
        elif key == "us10y":
            out[key] = f"{v:.3f}%"
        elif key == "dxy":
            out[key] = f"{v:.2f}"
        elif key == "wti":
            out[key] = f"${v:.2f} {c * 100:+.1f}%"
        elif key == "kospi":
            out[key] = f"{v:,.2f} {_fmt_pct(c)}"
        elif key == "usdkrw":
            out[key] = f"{v:,.2f}"
    return out, raw
