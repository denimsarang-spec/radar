"""D램 현물가: DRAMeXchange(1차, 현재가) / memoryindex.io(2차, 30일 변화 포함)."""
import datetime as dt
import re

import requests
from bs4 import BeautifulSoup

# DRAMeXchange 품목명 → memoryindex 코드
ITEMS = {
    "DDR5 16Gb (2Gx8) 4800/5600": "DDR5-16G",
    "DDR4 16Gb (2Gx8) 3200": "DDR4-16G",
    "DDR4 8Gb (1Gx8) 3200": "DDR4-8G",
}


def _num(t):
    m = re.search(r"[-+]?\d[\d,]*\.?\d*", t or "")
    return float(m.group().replace(",", "")) if m else None


def _asof(text):
    """DRAM Spot 표의 'Last Update: Nov.6 2026 18:10 (GMT+8)' → ISO 문자열."""
    m = re.search(r"DRAM Spot Price\s*Last Update:\s*([A-Za-z]{3})\.(\d+)\s+(\d{4})\s+(\d+:\d+)", text)
    if not m:
        return None
    return dt.datetime.strptime(" ".join(m.groups()), "%b %d %Y %H:%M").isoformat()


def dramexchange():
    """{품목명: 세션 평균가, "asof": 갱신 시각}. 실패·구조 변경은 예외 → radar.py가 로그·표시."""
    r = requests.get("https://www.dramexchange.com/", timeout=20)
    r.raise_for_status()
    s = BeautifulSoup(r.text, "lxml")
    out = {}
    for a in s.find_all("a", href=re.compile("Dram_Spot")):
        tr = a.find_parent("tr")
        tds = [td.get_text(strip=True) for td in tr.find_all("td")] if tr else []
        if len(tds) >= 6 and tds[0] in ITEMS:
            out[tds[0]] = _num(tds[5])  # Session Average
    if not out:
        raise ValueError("DRAMeXchange 표 없음 (구조 변경?)")
    out["asof"] = _asof(s.get_text(" ", strip=True))
    return out


def memoryindex():
    """{품목명: {"spot":..., "chg30":...}}. 실패·구조 변경은 예외."""
    r = requests.get("https://memoryindex.io/", timeout=20)
    r.raise_for_status()
    s = BeautifulSoup(r.text, "lxml")
    code2name = {v: k for k, v in ITEMS.items()}
    out = {}
    for sp in s.select("td span.num.text-primary"):
        code = sp.get_text(strip=True)
        if code not in code2name:
            continue
        tds = [td.get_text(" ", strip=True) for td in sp.find_parent("tr").find_all("td")]
        # Contract, Spot, Daily H/L, Session H/L, 24h, 30d, ...
        out[code2name[code]] = {"spot": _num(tds[1]), "chg30": _num(tds[5]) / 100}
    if not out:
        raise ValueError("memoryindex 표 없음 (구조 변경?)")
    return out
