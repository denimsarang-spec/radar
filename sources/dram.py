"""D램 현물가: DRAMeXchange(1차, 현재가) / memoryindex.io(2차, 30일 변화 포함)."""
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


def dramexchange():
    """{품목명: 세션 평균가}. 실패 시 None."""
    try:
        r = requests.get("https://www.dramexchange.com/", timeout=20)
        s = BeautifulSoup(r.text, "lxml")
        out = {}
        for a in s.find_all("a", href=re.compile("Dram_Spot")):
            tr = a.find_parent("tr")
            tds = [td.get_text(strip=True) for td in tr.find_all("td")] if tr else []
            if len(tds) >= 6 and tds[0] in ITEMS:
                out[tds[0]] = _num(tds[5])  # Session Average
        return out or None
    except Exception:
        return None


def memoryindex():
    """{품목명: {"spot":..., "chg30":...}}. 실패 시 None."""
    try:
        r = requests.get("https://memoryindex.io/", timeout=20)
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
        return out or None
    except Exception:
        return None
