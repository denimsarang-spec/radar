"""FRED: ICE BofA 미국 하이일드 OAS (06). 키 불필요, 일부 국내 회선에서는 연결이 끊긴다."""
import datetime as dt
import io

import pandas as pd
import requests

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def hy_oas(days=120):
    """일별 OAS(%) Series. 휴장일 '.'은 제외."""
    start = dt.date.today() - dt.timedelta(days=days)
    r = requests.get(URL, params={"id": "BAMLH0A0HYM2", "cosd": str(start)}, timeout=15,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    v = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s = pd.Series(v.values, index=[dt.date.fromisoformat(d) for d in df.iloc[:, 0]]).dropna()
    if s.empty:
        raise ValueError("FRED 빈 응답")
    return s.sort_index()
