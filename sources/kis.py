"""KIS Open API (선택, 네이버 실패 시 01·02 대체). 키는 D:\\radar\\.env 에서만 읽는다.

.env
  KIS_APP_KEY=...
  KIS_APP_SECRET=...
  KIS_BASE_URL=https://openapi.koreainvestment.com:9443   # 생략 가능

`python -m sources.kis` 로 응답 확인. 2026-11-05 실호출 검증: 두 TR 모두 frgn_ntby_tr_pbmn(백만원)이 네이버 값과 일치.
"""
import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
TOKEN = ROOT / "data" / "cache" / "kis_token.json"

# 시장별 투자자매매동향(일별) — 코스피
TR_MARKET_DAILY = ("/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market", "FHPTJ04040000")
# 시장별 투자자매매동향(시세) — 코스피200 선물, 당일분 (일별 누적은 radar 캐시로)
TR_MARKET_TIME = ("/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market", "FHPTJ04030000")
TR_INDEX_DAILY = ("/uapi/domestic-stock/v1/quotations/inquire-index-daily-price", "FHPUP02120000")
TR_FUT_PRICE = ("/uapi/domestic-futureoption/v1/quotations/inquire-price", "FHMIF10000000")


def _env():
    env = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith("KIS_")}}


def configured():
    e = _env()
    return bool(e.get("KIS_APP_KEY") and e.get("KIS_APP_SECRET"))


def _base():
    return _env().get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")


def _token():
    """접근토큰 (24시간, 발급 1분 1회 제한 → 파일 캐시)."""
    try:
        t = json.loads(TOKEN.read_text(encoding="utf-8"))
        if dt.datetime.fromisoformat(t["exp"]) > dt.datetime.now() + dt.timedelta(minutes=10):
            return t["token"]
    except Exception:
        pass
    e = _env()
    r = requests.post(_base() + "/oauth2/tokenP", timeout=15, json={
        "grant_type": "client_credentials", "appkey": e["KIS_APP_KEY"], "appsecret": e["KIS_APP_SECRET"]})
    r.raise_for_status()
    j = r.json()
    exp = dt.datetime.now() + dt.timedelta(seconds=int(j.get("expires_in", 86400)))
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({"token": j["access_token"], "exp": exp.isoformat()}), encoding="utf-8")
    return j["access_token"]


def _get(tr, params):
    path, tr_id = tr
    e = _env()
    r = requests.get(_base() + path, params=params, timeout=15, headers={
        "authorization": f"Bearer {_token()}", "appkey": e["KIS_APP_KEY"], "appsecret": e["KIS_APP_SECRET"],
        "tr_id": tr_id, "custtype": "P", "content-type": "application/json; charset=utf-8"})
    r.raise_for_status()
    j = r.json()
    if j.get("rt_cd") != "0":
        raise ValueError(f"KIS {tr_id}: {j.get('msg_cd')} {j.get('msg1')}")
    return j


def _rows(j):
    for k in ("output", "output1", "output2"):
        v = j.get(k)
        if isinstance(v, list) and v:
            return v
    return []


def foreign_net_kospi(end):
    """코스피 외국인 일별 순매수(원). 필드 frgn_ntby_tr_pbmn = 백만원."""
    ymd = end.strftime("%Y%m%d")
    j = _get(TR_MARKET_DAILY, {
        "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001", "FID_INPUT_DATE_1": ymd,
        "FID_INPUT_ISCD_1": "KSP", "FID_INPUT_DATE_2": ymd, "FID_INPUT_ISCD_2": "0001"})
    out = {dt.datetime.strptime(r["stck_bsop_date"], "%Y%m%d").date(): float(r["frgn_ntby_tr_pbmn"]) * 1e6
           for r in _rows(j) if r.get("stck_bsop_date") and r.get("frgn_ntby_tr_pbmn") not in (None, "")}
    return pd.Series(out).sort_index() if out else None


def fut_foreign_today():
    """코스피200 선물 외국인 당일 순매수(원)·순매수 수량(계약). 필드 백만원 기준."""
    j = _get(TR_MARKET_TIME, {"FID_INPUT_ISCD": "K2I", "FID_INPUT_ISCD_2": "F001"})  # K2I=코스피200 선물
    r = (_rows(j) or [j.get("output") or {}])[0]
    return {"net": float(r["frgn_ntby_tr_pbmn"]) * 1e6, "qty": float(r.get("frgn_ntby_qty") or 0)}


def k200_daily(end):
    """코스피200 현물 일별 종가."""
    j = _get(TR_INDEX_DAILY, {"FID_PERIOD_DIV_CODE": "D", "FID_COND_MRKT_DIV_CODE": "U",
                              "FID_INPUT_ISCD": "2001", "FID_INPUT_DATE_1": end.strftime("%Y%m%d")})
    out = {dt.datetime.strptime(r["stck_bsop_date"], "%Y%m%d").date(): float(r["bstp_nmix_prpr"])
           for r in _rows(j) if r.get("stck_bsop_date") and r.get("bstp_nmix_prpr")}
    return pd.Series(out).sort_index() if out else None


def fut_price():
    """코스피200 선물 최근월물 현재가 (장 마감 뒤엔 종가). 응답에 날짜 없음."""
    return float(_get(TR_FUT_PRICE, {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": "10100000"})["output1"]["futs_prpr"])


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if not configured():
        print(".env 에 KIS_APP_KEY / KIS_APP_SECRET 없음")
        raise SystemExit(1)
    today = dt.date.today()
    for name, fn in [("코스피 외인 일별", lambda: foreign_net_kospi(today)), ("선물 외인 당일", fut_foreign_today)]:
        try:
            print(name, fn())
        except Exception as ex:
            print(name, "실패:", type(ex).__name__, ex)
