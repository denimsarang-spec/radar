"""평일 아침 LLM 갱신: PC의 Claude가 06·07·08, 월가 해석, 후보군, 일정을 조사해 data/llm/latest.json에 쓴다.

python scripts/llm_daily.py
형식 검사를 통과하고 오늘 날짜일 때만 교체한다. 실패하면 이전 결과를 유지하고 radar.log에 남긴다.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import radar  # noqa: E402

ROOT = radar.ROOT
PROMPT = ROOT / "scripts" / "llm_prompt.md"
LAST = radar.DATA / "cache" / "llm_last_output.txt"  # 실패 원인 확인용
LIVE = "http://127.0.0.1:8765/state.json"
TIMEOUT = 1500
log = radar.log


def claude_path():
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")


def current_signals():
    """실행 중인 live.py의 판정값, 없으면 새로 계산."""
    try:
        sig = requests.get(LIVE, timeout=10).json()["sig"]
        if sig:
            return sig
    except Exception as ex:
        log.warning("LLM 갱신: live.py 응답 없음, 직접 계산: %s", ex)
    return radar.build(dt.date.today())


def main():
    sig = current_signals()
    brief = {"date": sig["date"], "stage": sig["stage"], "tape": sig["tape"], "errors": sig.get("errors"),
             "lamps": {k: {f: l.get(f) for f in ("s", "val", "verify")} | {"by": (l.get("raw") or {}).get("by")}
                       for k, l in sig["lamps"].items()}}
    prev = radar.LLM_PATH.read_text(encoding="utf-8") if radar.LLM_PATH.exists() else "{}"
    prompt = (PROMPT.read_text(encoding="utf-8").replace("{{today}}", str(dt.date.today()))
              .replace("{{signals}}", json.dumps(brief, ensure_ascii=False, default=str)).replace("{{previous}}", prev))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([claude_path(), "-p", "--model", "claude-opus-5-5", "--allowedTools",
                        "WebSearch,WebFetch,Agent,ToolSearch"],
                       input=prompt, encoding="utf-8", errors="replace", capture_output=True, cwd=ROOT,
                       env=env, timeout=TIMEOUT)
    LAST.parent.mkdir(parents=True, exist_ok=True)
    LAST.write_text(f"{r.stdout}\n--- stderr\n{r.stderr}", encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"claude 종료 코드 {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
    blocks = re.findall(r"```json\s*(.*?)```", r.stdout or "", flags=re.S)
    if not blocks:
        raise ValueError(f"JSON 블록 없음: {(r.stdout or '')[-300:]}")
    d = radar.validate_llm(json.loads(blocks[-1]))
    if d["date"] != str(dt.date.today()):
        raise ValueError(f"날짜 불일치: {d['date']}")
    radar.LLM_PATH.parent.mkdir(parents=True, exist_ok=True)
    radar.LLM_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("LLM 갱신 완료: %s", d["date"])
    print(f"완료: {d['date']}, 램프 {sorted(d.get('lamps') or {})}, 후보군 {len(d.get('candidates') or [])}행")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        log.exception("LLM 갱신 실패")
        print(f"LLM 갱신 실패: {type(ex).__name__}: {ex}", file=sys.stderr)
        raise SystemExit(1)
