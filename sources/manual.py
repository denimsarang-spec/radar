"""data/manual.json 수동 입력 (KRX 정보데이터시스템 CSV·HTS에서 옮겨 적은 값).

형식 (모든 키 선택, 날짜는 YYYY-MM-DD, 금액은 원):
{
  "frgn_net":     {"2026-11-05": -2502000000000},          # 외인 코스피 순매수
  "fut_frgn_net": {"2026-11-05": -239000000000},           # 외인 코스피200 선물 순매수
  "k200":         {"2026-11-05": {"spot": 750.10, "fut": 748.12, "theory": 2.60}},
  "frgn_ratio":   {"2026-11-05": 37.38},                    # 외인 코스피 시총 비중 %
  "next":         [{"d": "11/12 목", "n": "옵션 만기 → 03"}]
}
"""
import datetime as dt
import json
from pathlib import Path

import pandas as pd

PATH = Path(__file__).resolve().parent.parent / "data" / "manual.json"


def load():
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def series(m, key):
    """{날짜: 값} → 정렬된 Series. 없으면 None."""
    d = m.get(key)
    if not d:
        return None
    s = pd.Series({dt.date.fromisoformat(k): float(v) for k, v in d.items()}).sort_index()
    return s
