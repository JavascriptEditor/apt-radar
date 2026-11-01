"""텔레그램 알림(collector/notify.py) 테스트. 실제 접속 없음 — 가짜 Telegram API 서버(스레드)와 가짜 국토부·네이버 서버만.

확인: 환경변수 없으면 skip · 요약 1통(급매 매물·관심 단지 허가·D-day) · 중복 전송 방지(notify_state.json) ·
전송 실패 시 상태 유지(다음 실행에 재시도) · minGrade/maxPrice/grps 필터 · ALERTS_JSON 환경변수.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import engine as eng_mod  # noqa: E402
from collector import notify  # noqa: E402
from collector.engine import Engine  # noqa: E402
from collector.molit import MolitClient  # noqa: E402
from collector.naver import NaverClient, load_cfg  # noqa: E402
from collector.store import Store  # noqa: E402
from collector.toheo.store import ToheoStore  # noqa: E402
from tests import fake_servers as fs  # noqa: E402


class TgHandler(BaseHTTPRequestHandler):
    """가짜 Telegram Bot API. POST /bot<token>/sendMessage 본문을 기록. fail=True 면 400 을 돌려준다."""
    received: list[dict] = []
    fail = False

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        TgHandler.received.append({"path": self.path, "body": body})
        if TgHandler.fail:
            out = json.dumps({"ok": False, "description": "Bad Request: chat not found"}).encode()
            self.send_response(400)
        else:
            out = json.dumps({"ok": True, "result": {"message_id": len(TgHandler.received)}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # 조용히
        pass


@pytest.fixture(scope="module")
def tg():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), TgHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def server():
    srv, base = fs.start(now_ym=eng_mod.this_ym())
    yield base
    srv.shutdown()


@pytest.fixture(scope="module")
def world(tmp_path_factory, server):
    """가짜 세계로 실거래→모델→매칭→매물까지 한 번 돌린 data 디렉터리(강남구)."""
    tmp = tmp_path_factory.mktemp("notify")
    store = Store(tmp / "data")
    molit = MolitClient("key", url=server + "/molit", call_budget=9500, sleep=0)
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp / "data" / "naver_cfg.json")
    e = Engine(store, molit, nv, region_filter="11680", log=lambda *a: None)
    e.run_trades(reserve=100)
    e.run_model()
    e.run_match()
    assert e.run_listings()["status"] == "ok"
    return tmp


def make_engine(tmp) -> Engine:
    return Engine(Store(tmp / "data"), None, None, region_filter="11680", log=lambda *a: None)


@pytest.fixture(autouse=True)
def reset(monkeypatch, tg):
    TgHandler.received = []
    TgHandler.fail = False
    monkeypatch.setenv("TELEGRAM_API_BASE", tg)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ALERTS_JSON", raising=False)
    yield


def write_cfg(tmp, **over) -> Path:
    cfg = {"complexes": [], "regions": ["11680"], "minGrade": "S", "maxPrice": None, "grps": [],
           "toheo": {"permitsForComplexes": True, "designationDdays": [30, 7]}}
    cfg.update(over)
    p = tmp / "alerts.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return p


def test_import_has_no_side_effects():
    # 모듈 수준에서 네트워크·파일 접근 없음: requests 는 send_telegram 안에서만 import
    import importlib
    src = Path(notify.__file__).read_text(encoding="utf-8")
    assert "\nimport requests" not in src
    assert importlib.reload(notify)


def test_skip_without_env(world):
    e = make_engine(world)
    r = notify.send_digest(e, "https://x.github.io/r", cfg_path=write_cfg(world))
    assert r["skipped"] is True and "TELEGRAM" in r["reason"]
    assert TgHandler.received == []
    assert not (world / "data" / "notify_state.json").exists()


def test_digest_once_and_dedupe(world, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    e = make_engine(world)
    cfg = write_cfg(world, minGrade="S")
    r = notify.send_digest(e, "https://x.github.io/r", cfg_path=cfg)
    assert r["sent"] is True and r["listings"] > 0 and r["error"] is None
    # 메시지는 1통, HTML, 등급 표시·단지 링크 포함
    assert len(TgHandler.received) == 1
    msg = TgHandler.received[0]
    assert msg["path"] == "/bot123:abc/sendMessage"
    body = msg["body"]
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert "급매 매물" in body["text"] and "https://x.github.io/r/#c-" in body["text"]
    assert all(("[SS]" in ln or "[S]" in ln) for ln in body["text"].splitlines() if ln.startswith("• ") and "㎡" in ln)
    assert len(body["text"]) <= notify.TELEGRAM_MAX_CHARS
    st = json.loads((world / "data" / "notify_state.json").read_text(encoding="utf-8"))
    assert len(st["listings"]) == r["listings"] and st["lastSentAt"]

    # 같은 상태로 다시 실행하면 보낼 것이 없어 메시지 없음
    r2 = notify.send_digest(make_engine(world), "https://x.github.io/r", cfg_path=cfg)
    assert r2["skipped"] is True and r2["reason"] == "nothing"
    assert len(TgHandler.received) == 1


def test_send_failure_keeps_state(world, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    e = make_engine(world)
    e.store.write_json("notify_state.json", {})          # 보낸 기록 초기화
    TgHandler.fail = True
    r = notify.send_digest(e, "", cfg_path=write_cfg(world, minGrade=""))
    assert r["sent"] is False and "chat not found" in r["error"]
    assert not (world / "data" / "notify_state.json").exists() or \
        json.loads((world / "data" / "notify_state.json").read_text(encoding="utf-8")).get("listings", {}) == {}
    TgHandler.fail = False
    r = notify.send_digest(make_engine(world), "", cfg_path=write_cfg(world, minGrade=""))
    assert r["sent"] is True and r["listings"] == len(e.store.read_listings(active_only=True))


def test_filters_and_env_cfg(world, monkeypatch):
    e = make_engine(world)
    e.store.write_json("notify_state.json", {})
    sent = []

    def fake_sender(text):
        sent.append(text)
        return {"ok": True}

    active = e.store.read_listings(active_only=True)
    # grps 필터: 존재하지 않는 평형만 허용 → 매물 0건
    r = notify.send_digest(e, "", cfg_path=write_cfg(world, minGrade="", grps=[1]), sender=fake_sender)
    assert r["skipped"] and r["reason"] == "nothing" and not sent
    # maxPrice 필터
    prices = sorted(int(l["price"]) for l in active.values())
    r = notify.send_digest(e, "", cfg_path=write_cfg(world, minGrade="", maxPrice=prices[0]), sender=fake_sender)
    assert r["sent"] and r["listings"] == sum(1 for p in prices if p <= prices[0])
    # 관심 단지만 (regions 비움) — 상태 초기화 후
    e.store.write_json("notify_state.json", {})
    cid = next(iter(active.values()))["cid"]
    r = notify.send_digest(e, "", cfg_path=write_cfg(world, minGrade="", regions=[], complexes=[cid]), sender=fake_sender)
    assert r["sent"] and r["listings"] == sum(1 for l in active.values() if l["cid"] == cid)
    # 파일이 없으면 ALERTS_JSON 환경변수
    e.store.write_json("notify_state.json", {})
    monkeypatch.setenv("ALERTS_JSON", json.dumps({"regions": ["강남구"], "minGrade": ""}))
    r = notify.send_digest(e, "", cfg_path=world / "없는파일.json", sender=fake_sender)
    assert r["sent"] and r["listings"] == len(active)
    # 설정이 전혀 없으면(기본값: 구독 없음) 보낼 것 없음
    monkeypatch.delenv("ALERTS_JSON")
    e.store.write_json("notify_state.json", {})
    r = notify.send_digest(e, "", cfg_path=world / "없는파일.json", sender=fake_sender)
    assert r["skipped"] and r["reason"] == "nothing"


def test_permits_and_ddays(world, monkeypatch):
    e = make_engine(world)
    e.store.write_json("notify_state.json", {})
    cid = next(cid for cid, c in e.complexes.items() if c["name"] == "은마")
    tst = ToheoStore(world / "data" / "toheo")
    tst.save_permits({
        "11680|2026|10|1": {"key": "11680|2026|10|1", "sgg": "11680", "gu": "강남구", "dong": "대치동", "jibun": "316",
                             "hndl_ymd": "2026-10-28", "use_purp": "주거용", "job_gbn": "허가", "cid": cid, "cname": "은마"},
        "11680|2026|10|2": {"key": "11680|2026|10|2", "sgg": "11680", "gu": "강남구", "dong": "대치동", "jibun": "316",
                             "hndl_ymd": "2026-10-29", "use_purp": "주거용", "job_gbn": "불허가", "cid": cid, "cname": "은마"},
        "11680|2026|10|3": {"key": "11680|2026|10|3", "sgg": "11680", "gu": "강남구", "dong": "역삼동", "jibun": "1",
                             "hndl_ymd": "2026-10-29", "use_purp": "주거용", "job_gbn": "허가", "cid": "", "cname": ""},
    })
    sent = []
    sender = lambda t: (sent.append(t) or {"ok": True})  # noqa: E731
    # 10·15 대책 지정(~2026-12-31) D-30 = 2026-12-01
    cfg = write_cfg(world, regions=[], complexes=[cid], minGrade="SS", maxPrice=1)
    r = notify.send_digest(e, "https://s", cfg_path=cfg, sender=sender, today="2026-12-01")
    assert r["sent"] and r["listings"] == 0 and r["permits"] == 1 and r["ddays"] >= 1
    text = sent[-1]
    assert "은마" in text and "허가 완료" in text and "주거용 1" in text and "D-30" in text and "2026-12-31" in text
    st = json.loads((world / "data" / "notify_state.json").read_text(encoding="utf-8"))
    assert st["permits"] == {"11680|2026|10|1": "2026-12-01"} and any(k.endswith("|D-30") for k in st["ddays"])
    # 같은 날 다시: 중복 없음. D-7 은 다른 날 한 번 더
    r = notify.send_digest(e, "https://s", cfg_path=cfg, sender=sender, today="2026-12-01")
    assert r["skipped"] and r["reason"] == "nothing"
    r = notify.send_digest(e, "https://s", cfg_path=cfg, sender=sender, today="2026-12-24")
    assert r["sent"] and r["ddays"] >= 1 and "D-7" in sent[-1]
    # permitsForComplexes=false 면 허가 제외
    e.store.write_json("notify_state.json", {})
    cfg2 = write_cfg(world, regions=[], complexes=[cid], minGrade="SS", maxPrice=1,
                     toheo={"permitsForComplexes": False, "designationDdays": []})
    r = notify.send_digest(e, "https://s", cfg_path=cfg2, sender=sender, today="2026-12-01")
    assert r["skipped"] and r["reason"] == "nothing"


def test_format_price():
    assert notify.fmt_price(145000) == "14억 5,000"
    assert notify.fmt_price(280000) == "28억"
    assert notify.fmt_price(9500) == "9,500"
    assert notify.fmt_price(None) == "-"
