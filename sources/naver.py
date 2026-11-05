"""Npay 증권(m.stock.naver.com) 공개 JSON. 구 finance.naver.com/sise 페이지는 410(종료).

- {code}/price      일별 시세 (KOSPI, KPI200, FUT=코스피200 선물 최근월물)
- {code}/trend      일별 투자자 순매수 (bizdate=YYYYMMDD, 단위 억원)
- FUT/integration   선물 월물(month)
구조가 바뀌면 예외를 그대로 올린다 → radar.py가 na + 로그 처리.
"""
import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import requests

BASE = "https://m.stock.naver.com/api/index/"
CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "naver_trend.json"


def _get(path, **params):
    r = requests.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    if "json" not in (r.headers.get("content-type") or ""):
        raise ValueError(f"{path}: JSON 아님 (구조 변경?)")
    return r.json()


def _num(s):
    return float(str(s).replace(",", "").replace("+", ""))


def daily_close(code, n=80):
    """일별 종가 Series (index=date, 오래된 순)."""
    rows, page = [], 1
    while len(rows) < n and page <= 10:
        got = _get(f"{code}/price", pageSize=60, page=page)
        if not got:
            break
        rows += got
        page += 1
    if not rows:
        raise ValueError(f"{code}/price: 빈 응답")
    s = pd.Series({dt.date.fromisoformat(r["localTradedAt"][:10]): _num(r["closePrice"]) for r in rows})
    return s.sort_index()


def futures_month():
    return _get("FUT/integration").get("month")


def _load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def trend_series(code, dates, today):
    """주어진 거래일들의 외국인 순매수(원) Series. 과거일은 캐시.

    휴장일·미집계일은 세 주체가 모두 0으로 오므로 결측 처리한다.
    """
    cache = _load_cache()
    bucket = cache.setdefault(code, {})
    out = {}
    for d in dates:
        key = d.strftime("%Y%m%d")
        row = bucket.get(key)
        if row is None:
            row = _get(f"{code}/trend", bizdate=key)
            if row.get("bizdate") != key:
                raise ValueError(f"{code}/trend: bizdate 불일치 {row.get('bizdate')} != {key}")
            if d < today:
                bucket[key] = row
            time.sleep(0.2)
        vals = [_num(row[k]) for k in ("personalValue", "foreignValue", "institutionalValue")]
        if any(vals):
            out[d] = vals[1] * 1e8
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return pd.Series(out, dtype=float).sort_index() if out else None
