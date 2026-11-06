"""Npay 증권(m.stock.naver.com) 공개 JSON. 구 finance.naver.com/sise 페이지는 410(종료).

- {code}/price      일별 시세 (KOSPI, KPI200, FUT=코스피200 선물 최근월물)
- {code}/trend      일별 투자자 순매수 (bizdate=YYYYMMDD, 단위 억원)
- FUT/integration   선물 월물(month)
구조가 바뀌면 예외를 그대로 올린다 → radar.py가 na + 로그 처리.
"""
import datetime as dt
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

BASE = "https://m.stock.naver.com/api/index/"
POLL = "https://polling.finance.naver.com/api/realtime/"
FX = "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_USDKRW"
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


def _realtime(path):
    """polling.finance.naver.com 실시간 시세. (시세 dict, 서버 권장 폴링 간격 초) 반환."""
    r = requests.get(POLL + path, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    j = r.json()
    return j["datas"][0], j.get("pollingInterval", 70000) / 1000


def _usdkrw():
    r = requests.get(FX, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()["result"]["closePrice"]


def live_tape():
    """테이프용 실시간 값 + 코스피 장 상태. 항목별 실패는 빠진 채로 반환 (직전 값 유지는 호출자 몫)."""
    def pct(d):
        return f"{_num(d['fluctuationsRatio']):+.2f}%"

    paths = {"kospi": "domestic/index/KOSPI", "dow": "worldstock/index/.DJI", "sox": "worldstock/index/.SOX"}
    with ThreadPoolExecutor(len(paths) + 1) as ex:
        futs = {k: ex.submit(_realtime, p) for k, p in paths.items()}
        fx = ex.submit(_usdkrw)
    tape, wait, status = {}, [], None
    for k, f in futs.items():
        try:
            d, sec = f.result()
        except Exception:
            continue
        wait.append(sec)
        if k == "kospi":
            tape[k] = f"{d['closePrice']} {pct(d)}"
            status = d.get("marketStatus")
        else:
            tape[k] = pct(d)
    try:
        tape["usdkrw"] = fx.result()
    except Exception:
        pass
    return tape, status, min(wait) if wait else None
