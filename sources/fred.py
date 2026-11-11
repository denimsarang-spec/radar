"""FRED: ICE BofA 미국 하이일드 OAS (06).

국내 회선에서는 fred.stlouisfed.org가 끊기고 api.stlouisfed.org는 열려 있다(2026-11-10 확인).
API는 무료 키가 필요하다: D:\\radar\\.env 에 FRED_API_KEY=... (없으면 웹 경로만 시도)
"""
import datetime as dt
import io
import os
import re

import pandas as pd
import requests

from sources import kis

API = "https://api.stlouisfed.org/fred/series/observations"
CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
PAGE = "https://fred.stlouisfed.org/data/BAMLH0A0HYM2"  # CSV가 막히거나 비었을 때의 대체 경로
UA = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 8  # 막힌 회선에서 20초씩 기다리지 않게


def api_key():
    return os.environ.get("FRED_API_KEY") or kis._env().get("FRED_API_KEY")


def _api(days):
    key = api_key()
    if not key:
        raise ValueError("FRED_API_KEY 없음")
    start = dt.date.today() - dt.timedelta(days=days)
    try:
        r = requests.get(API, params={"series_id": "BAMLH0A0HYM2", "api_key": key, "file_type": "json",
                                      "observation_start": str(start)}, timeout=TIMEOUT, headers=UA)
    except requests.RequestException as ex:  # 메시지에 키가 든 URL이 섞이므로 종류만 남긴다
        raise RuntimeError(f"FRED API 연결 실패: {type(ex).__name__}") from None
    if not r.ok:
        raise RuntimeError(f"FRED API HTTP {r.status_code}")
    obs = r.json()["observations"]
    v = pd.to_numeric(pd.Series([o["value"] for o in obs]), errors="coerce")
    return pd.Series(v.values, index=[dt.date.fromisoformat(o["date"]) for o in obs]).dropna()


def _csv(days):
    start = dt.date.today() - dt.timedelta(days=days)
    r = requests.get(CSV, params={"id": "BAMLH0A0HYM2", "cosd": str(start)}, timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    v = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    return pd.Series(v.values, index=[dt.date.fromisoformat(d) for d in df.iloc[:, 0]]).dropna()


def _page(days):
    r = requests.get(PAGE, timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    start = dt.date.today() - dt.timedelta(days=days)
    rows = re.findall(r"(\d{4}-\d{2}-\d{2})\s*(?:<[^>]+>\s*)*(\d+\.\d+)", r.text)
    s = {dt.date.fromisoformat(d): float(x) for d, x in rows}
    return pd.Series({d: x for d, x in s.items() if d >= start}, dtype=float)


def hy_oas(days=120):
    """일별 OAS(%) Series. 휴장일 '.'은 제외. 모든 경로가 실패하면 마지막 오류를 올린다."""
    err = None
    for fn in ((_api,) if api_key() else ()) + (_csv, _page):
        try:
            s = fn(days)
            if len(s):
                return s.sort_index()
            err = ValueError(f"{fn.__name__}: 빈 응답")
        except Exception as ex:
            err = ex
    raise err
