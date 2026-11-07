"""실시간 레이더: 내 PC에서 수집해 보여주고 그날 기록을 남기는 로컬 서버. GitHub를 거치지 않는다.

python live.py               # http://127.0.0.1:8765 열기
python live.py --port 8080 --no-open

갱신 주기 — 시세: 네이버 권장 간격(최소 5초) / 램프: 장중 30초, 장외 5분 / 미국·D램: 10분
장중 램프는 종가 기준 스펙을 현재값으로 돌린 잠정치라 저장하지 않고, 19:30 이후 확정값만 data/YYYY-MM-DD.json에 저장한다.
"""
import argparse
import datetime as dt
import json
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import radar
from sources import naver, us

QUOTE_MIN, QUOTE_MAX = 5, 70
LAMP_OPEN, LAMP_CLOSED = 30, 300
SLOW_SEC = 600
SLOW_KEYS = ("sox", "nv_sox", "dx", "mi", "yf_kospi", "hy_live")  # 하루 1번 바뀌는 소스 → 램프 갱신마다 부르지 않는다
PAGE_POLL_MS = 3000

# 커밋 대상 data/kis_fut.json에 장중 값이 들어가지 않도록 분리
LIVE_KIS = radar.DATA / "cache" / "kis_fut_live.json"
SAVE_AT = dt.time(19, 30)  # 종가 확정 + D램 당일분(19:10) 이후 그날 기록 저장

log = radar.log


class State:
    sig = None
    quotes = {}
    status = None
    quote_at = None
    quote_every = QUOTE_MAX
    lamp_at = None
    saved = None


def now():
    return time.time()


def market_open():
    return State.status == "OPEN"


def quote_loop():
    while True:
        try:
            tape, status, sec = naver.live_tape()
            if tape:
                State.quotes = {**State.quotes, **tape}
                State.quote_at = now()
            if status:
                State.status = status
            if sec:
                State.quote_every = min(max(sec, QUOTE_MIN), QUOTE_MAX)
        except Exception:
            log.exception("실시간 시세 실패")
        time.sleep(State.quote_every)


def save_record(sig):
    """확정값을 data/<거래일>.json에 저장. 당일은 19:30 이후 1회, 지난 거래일은 기록이 없을 때만."""
    if sig.get("provisional") or State.saved == sig["date"]:
        return
    path = radar.DATA / f"{sig['date']}.json"
    now_ = dt.datetime.now()
    if sig["date"] == str(now_.date()) and now_.time() < SAVE_AT:
        return
    if sig["date"] != str(now_.date()) and path.exists():
        return
    path.write_text(json.dumps(sig, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    State.saved = sig["date"]
    log.info("기록 저장 %s", path.name)


def lamp_loop():
    slow, slow_at, tape = {}, 0, {}
    while True:
        try:
            if now() - slow_at > SLOW_SEC:
                slow, slow_at = {}, now()
                try:
                    fresh, _ = us.tape()
                    tape = {**tape, **{k: v for k, v in fresh.items() if v is not None}}
                except Exception:
                    log.exception("테이프 실패")
            c = radar.Ctx(dt.date.today())
            c._c.update(slow)
            State.sig = radar.build(c.day, c, tape)
            slow = {k: c._c[k] for k in SLOW_KEYS if k in c._c}
            State.lamp_at = now()
            save_record(State.sig)
        except Exception:
            log.exception("램프 갱신 실패")
        time.sleep(LAMP_OPEN if market_open() else LAMP_CLOSED)


def clock(t):
    return dt.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "—"


def banner():
    """시세·판정 시각을 항상 보여주고, 멈췄으면 지연 경고."""
    late = []
    if State.quote_at and now() - State.quote_at > State.quote_every * 3 + 10:
        late.append("시세")
    if State.lamp_at and now() - State.lamp_at > (LAMP_OPEN if market_open() else LAMP_CLOSED) * 2 + 60:
        late.append("판정")
    if market_open():
        mode = "장중 · 램프는 현재값 기준 잠정치(종가 아님)"
    elif State.sig and State.sig.get("provisional"):
        mode = "마감 직후 · 16:30 전 잠정치"
    else:
        mode = "장 닫힘 · 마지막 거래일 기준"
    warn = f'<b> · {"·".join(late)} 갱신 지연</b>' if late else ""
    return (f'<div class="livebar{" late" if late else ""} num">{mode}<br>'
            f"시세 {clock(State.quote_at)} · 판정 {clock(State.lamp_at)}{warn}</div>")


STYLE = """<style>
.livebar { margin-top: 10px; padding: 8px 12px; font-size: 12px; color: var(--muted);
  border: 1px solid var(--line); border-radius: 8px; }
.livebar.late { color: var(--no); border-color: var(--no); }
.livedown { display: none; margin: 10px auto; padding: 8px 12px; font-size: 12px; color: var(--no);
  border: 1px solid var(--no); border-radius: 8px; max-width: 728px; }
.livedown.on { display: block; }
</style>"""

# 화면 전체를 바꾸지 않고 달라진 글자·속성만 고쳐 스크롤과 링 애니메이션을 유지한다
SCRIPT = """<div class="livedown" id="livedown">서버 연결 끊김 — 화면 값이 멈춰 있습니다 (live.py 실행 확인)</div>
<script>
function morph(a, b) {
  if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) { a.replaceWith(b.cloneNode(true)); return; }
  if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
  for (const n of [...a.attributes]) if (!b.hasAttribute(n.name)) a.removeAttribute(n.name);
  for (const n of [...b.attributes]) if (a.getAttribute(n.name) !== n.value) a.setAttribute(n.name, n.value);
  if (a.childNodes.length !== b.childNodes.length) { a.replaceChildren(...[...b.childNodes].map(x => x.cloneNode(true))); return; }
  [...a.childNodes].forEach((x, i) => morph(x, b.childNodes[i]));
}
async function tick() {
  const down = document.getElementById('livedown');
  try {
    const r = await fetch('/', {cache: 'no-store'});
    if (!r.ok) throw new Error(r.status);
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    if (!document.querySelector('.hero') && doc.querySelector('.hero')) { location.reload(); return; }
    const sa = document.querySelector('style'), sb = doc.querySelector('style');
    if (sa && sb && sa.textContent !== sb.textContent) sa.textContent = sb.textContent;
    morph(document.querySelector('main'), doc.querySelector('main'));
    down.classList.remove('on');
  } catch (e) { down.classList.add('on'); }
}
setInterval(tick, %d);
</script>""" % PAGE_POLL_MS


def page():
    sig = State.sig
    if sig is None:
        body = ("<!doctype html><html lang=ko><head><meta charset=utf-8><title>진입 레이더</title>"
                "<style>body{font-family:sans-serif;padding:20px}</style></head><body>"
                "<main>첫 수집 중… (최대 1분)</main></body></html>")
    else:
        view = {**sig, "tape": {**sig["tape"], **State.quotes}}
        try:
            body = radar.render(view)
        except Exception as ex:
            body = radar.fallback_html(view, f"{type(ex).__name__}: {ex}")
        body = body.replace("</header>", "</header>\n" + banner(), 1).replace("</head>", STYLE + "</head>", 1)
    return body.replace("</body>", SCRIPT + "</body>", 1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/":
            out, ctype = page(), "text/html; charset=utf-8"
        elif self.path == "/state.json":
            out, ctype = json.dumps({"status": State.status, "quote_at": State.quote_at, "lamp_at": State.lamp_at,
                                     "quote_every": State.quote_every, "quotes": State.quotes, "sig": State.sig},
                                    ensure_ascii=False, default=str), "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        data = out.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    if sys.stdout:  # 작업 스케줄러(pythonw)로 실행하면 콘솔이 없다
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if not LIVE_KIS.exists() and radar.KIS_FUT_CACHE.exists():
        LIVE_KIS.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(radar.KIS_FUT_CACHE, LIVE_KIS)
    radar.KIS_FUT_CACHE, radar.KIS_SAVE_ALWAYS = LIVE_KIS, True

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)  # 인증이 없으므로 이 PC에서만 접속
    threading.Thread(target=quote_loop, daemon=True).start()
    threading.Thread(target=lamp_loop, daemon=True).start()
    url = f"http://127.0.0.1:{a.port}"
    log.info("실시간 레이더 시작 %s", url)
    if sys.stdout:
        print(f"실시간 레이더 {url} (종료: Ctrl+C)", flush=True)
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
