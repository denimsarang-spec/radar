"""FRED: ICE BofA 미국 하이일드 OAS (06). 키 불필요, 일부 국내 회선에서는 연결이 끊긴다."""
import datetime as dt
import io
import re

import pandas as pd
import requests

CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
PAGE = "https://fred.stlouisfed.org/data/BAMLH0A0HYM2"  # CSV가 막히거나 비었을 때의 대체 경로
UA = {"User-Agent": "Mozilla/5.0"}


def _csv(days):
    start = dt.date.today() - dt.timedelta(days=days)
    r = requests.get(CSV, params={"id": "BAMLH0A0HYM2", "cosd": str(start)}, timeout=20, headers=UA)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    v = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    return pd.Series(v.values, index=[dt.date.fromisoformat(d) for d in df.iloc[:, 0]]).dropna()


def _page(days):
    r = requests.get(PAGE, timeout=20, headers=UA)
    r.raise_for_status()
    start = dt.date.today() - dt.timedelta(days=days)
    rows = re.findall(r"(\d{4}-\d{2}-\d{2})\s*(?:<[^>]+>\s*)*(\d+\.\d+)", r.text)
    s = {dt.date.fromisoformat(d): float(x) for d, x in rows}
    return pd.Series({d: x for d, x in s.items() if d >= start}, dtype=float)


def hy_oas(days=120):
    """일별 OAS(%) Series. 휴장일 '.'은 제외. 두 경로 모두 실패하면 마지막 오류를 올린다."""
    err = None
    for fn in (_csv, _page):
        try:
            s = fn(days)
            if len(s):
                return s.sort_index()
            err = ValueError(f"{fn.__name__}: 빈 응답")
        except Exception as ex:
            err = ex
    raise err
