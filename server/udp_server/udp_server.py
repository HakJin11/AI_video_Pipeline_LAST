"""AI 영상 파이프라인 UDP 서버.

ComfyUI와 영상 앱(FastAPI, 기본 http://127.0.0.1:8010)이 돌아가는 서버 PC에서 실행한다. 다른 PC의 클라이언트가 UDP로
보내는 명령을 받아 로컬 앱의 HTTP API로 전달하고, 완성 영상은 조각으로 나눠 UDP로 돌려준다. 클라이언트의
"웹 화면 열기"는 앱의 웹 화면 전체(모든 설정)를 UDP로 중계한다 (cmd_http).

    AIVideoServer.exe [--port 9999] [--app http://127.0.0.1:8010] [--password 비밀번호]

설정은 실행 파일 옆의 server_config.json 으로도 줄 수 있다: {"port": 9999, "password": "...", "app": "..."}
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # proto.py sits next to this file
import proto  # noqa: E402

ACTIVE = ("queued", "rendering")


def base_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def log(*a) -> None:
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


class AppAPI:
    """The local video app's HTTP API (stdlib only, so the exe stays small)."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("detail")
            except Exception:  # noqa: BLE001
                detail = None
            raise RuntimeError(detail or f"앱 오류 {e.code}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"영상 앱({self.url})에 연결할 수 없어요: {e.reason}") from None

    def get(self, path: str, timeout: float = 60):
        return self._req("GET", path, timeout=timeout)

    def post(self, path: str, body: dict | None = None, timeout: float = 120):
        return self._req("POST", path, body if body is not None else {}, timeout=timeout)

    def download(self, path: str) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=300) as r:
            return r.read()


def summarize(d: dict) -> dict:
    """What the client shows for one run."""
    cuts = d.get("cuts") or []
    active = [c["position"] + 1 for c in cuts if c.get("status") in ACTIVE]
    errors = [{"cut": c["position"] + 1, "error": c.get("error_message")} for c in cuts if c.get("status") == "error"]
    return {
        "run_id": d["id"], "title": d.get("title"), "length_sec": d.get("length_sec"),
        "cuts": len(cuts), "done": sum(1 for c in cuts if c.get("clip_url")), "active": active,
        "missing": [c["position"] + 1 for c in cuts if not c.get("clip_url")],
        "errors": errors,
        "stage": (d.get("render_stage") or {}).get("text"),
        "eta_sec": (d.get("eta") or {}).get("sec"),
        "estimate_sec": (d.get("estimate") or {}).get("sec"),
        "background": (d.get("background") or {}).get("name"),
        "final": bool(d.get("final_video_url")), "compare": bool(d.get("compare_video_url")),
        "fast": bool(d.get("fast_preview")),
    }


class Server:
    def __init__(self, port: int, app: str, password: str):
        self.port = port
        self.api = AppAPI(app)
        self.password = password
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        self.sock.bind(("0.0.0.0", port))
        self.send_lock = threading.Lock()
        self.replies: dict[tuple, tuple[float, bytes]] = {}  # (addr, id) -> (time, answer): retries get the same answer
        self.files: dict[tuple, dict] = {}  # (run, kind) -> {"data", "sha256", "url", "at"}
        self.inflight: set[tuple] = set()  # (addr, id) being worked on right now
        self.uploads: dict[str, dict] = {}  # upload id -> {"parts": {i: bytes}, "total", "at"} (big request bodies)
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ commands
    def cmd_ping(self, m):
        try:
            h = self.api.get("/api/health", timeout=15)
        except RuntimeError as e:
            return {"app": False, "error_detail": str(e)}
        return {"app": True, "comfyui": h.get("comfyui"), "ollama": h.get("ollama"), "server_time": time.time()}

    def cmd_catalog(self, m):
        return {"items": [{"id": t["id"], "title": t["title"], "description": t.get("description", ""),
                           "length_options": t.get("length_options", []), "cast_count": t.get("cast_count")}
                          for t in self.api.get("/api/follow/catalog")]}

    def cmd_runs(self, m):
        runs = self.api.get("/api/follow?kind=run")[: int(m.get("limit", 30))]
        return {"items": [{"run_id": r["id"], "title": r["title"], "length_sec": r["length_sec"],
                           "created_at": r.get("created_at"), "final": bool(r.get("final_video_path"))}
                          for r in runs]}

    def cmd_start(self, m):
        d = self.api.post(f"/api/follow/catalog/{int(m['template_id'])}/start", {"length_sec": int(m["length_sec"])})
        if m.get("render", True):
            self.api.post(f"/api/follow/{d['id']}/render-all")
            d = self.api.get(f"/api/follow/{d['id']}")
        log(f"새 실행 {d['id']} 시작 ({m['length_sec']}초)")
        return summarize(d)

    def cmd_render(self, m):
        rid = int(m["run_id"])
        r = self.api.post(f"/api/follow/{rid}/render-all")
        log(f"실행 {rid}: 남은 컷 {len(r.get('queued', []))}개 만들기")
        return summarize(self.api.get(f"/api/follow/{rid}"))

    def cmd_status(self, m):
        return summarize(self.api.get(f"/api/follow/{int(m['run_id'])}"))

    def cmd_assemble(self, m):
        rid = int(m["run_id"])
        job = self.api.post(f"/api/follow/{rid}/assemble")["job_id"]
        for _ in range(240):  # a few seconds normally
            j = self.api.get(f"/api/follow/jobs/{job}")
            if j.get("status") == "done":
                break
            if j.get("status") == "error":
                raise RuntimeError(f"합치기 실패: {j.get('error')}")
            time.sleep(1)
        log(f"실행 {rid}: 합치기 완료")
        return summarize(self.api.get(f"/api/follow/{rid}"))

    def cmd_file_info(self, m):
        rid, kind = int(m["run_id"]), m.get("kind", "compare")
        d = self.api.get(f"/api/follow/{rid}")
        url = d.get("compare_video_url" if kind == "compare" else "final_video_url")
        if not url:
            raise RuntimeError("아직 완성 영상이 없어요 (모든 컷을 만든 뒤 합치기가 필요해요)")
        with self.lock:
            f = self.files.get((rid, kind))
        if not f or f["url"] != url:
            data = self.api.download(url)
            f = {"data": data, "sha256": hashlib.sha256(data).hexdigest(), "url": url, "at": time.time()}
            with self.lock:
                self.files[(rid, kind)] = f
        f["at"] = time.time()
        size = len(f["data"])
        log(f"실행 {rid}: {kind} 영상 보내기 준비 ({size / 1e6:.1f} MB)")
        return {"size": size, "chunk_size": proto.CHUNK_SIZE, "chunks": (size + proto.CHUNK_SIZE - 1) // proto.CHUNK_SIZE,
                "sha256": f["sha256"], "name": f"run{rid}_{kind}_{Path(url).name}"}

    # ------------------------------------------------------------------ 웹 화면 전체 (2026-10-12)
    # The client PC opens the app's whole web screen in its own browser; its local relay sends every browser request
    # here as one "http" command (big bodies first as upload_part pieces) and gets the answer back the same way as a
    # video: small answers inline, big ones as chunks. Everything stays UDP.
    HTTP_INLINE = 30000

    def cmd_upload_part(self, m):
        uid = str(m["upload_id"])
        with self.lock:
            u = self.uploads.setdefault(uid, {"parts": {}, "total": int(m["total"]), "at": time.time()})
            u["parts"][int(m["index"])] = base64.b64decode(m["data"])
            u["at"] = time.time()
            return {"have": len(u["parts"]), "total": u["total"]}

    def cmd_http(self, m):
        method = str(m.get("method", "GET")).upper()
        path = str(m.get("path", "/"))
        if not path.startswith("/") or ".." in path.split("?")[0]:
            raise RuntimeError("잘못된 경로")
        body = None
        if m.get("upload_id"):
            with self.lock:
                u = self.uploads.pop(str(m["upload_id"]), None)
            if not u or len(u["parts"]) != u["total"]:
                raise RuntimeError("올린 데이터가 다 도착하지 않았어요. 다시 시도해 주세요")
            body = b"".join(u["parts"][i] for i in range(u["total"]))
        elif m.get("body_b64"):
            body = base64.b64decode(m["body_b64"])
        elif method in ("POST", "PUT", "PATCH"):
            body = b""
        headers = {k: v for k, v in (m.get("headers") or {}).items()
                   if k.lower() in ("content-type", "accept")}
        req = urllib.request.Request(self.api.url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                status, data, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, data, hdrs = e.code, e.read(), e.headers
        except urllib.error.URLError as e:
            raise RuntimeError(f"영상 앱({self.api.url})에 연결할 수 없어요: {e.reason}") from None
        except OSError as e:  # the app dropped the connection (e.g. an unknown upload route)
            raise RuntimeError(f"영상 앱이 요청을 끊었어요 ({method} {path.split('?')[0]}): {e}") from None
        out = {"status": status, "size": len(data),
               "headers": {k: hdrs[k] for k in ("Content-Type", "Cache-Control", "Content-Disposition") if hdrs.get(k)}}
        if len(data) <= self.HTTP_INLINE:
            out["body_b64"] = base64.b64encode(data).decode("ascii")
            return out
        token = uuid.uuid4().hex
        with self.lock:
            self.files[("http", token)] = {"data": data, "sha256": hashlib.sha256(data).hexdigest(), "url": path,
                                           "at": time.time()}
        out.update(key=token, chunk_size=proto.CHUNK_SIZE, chunks=(len(data) + proto.CHUNK_SIZE - 1) // proto.CHUNK_SIZE,
                   sha256=hashlib.sha256(data).hexdigest())
        return out

    def send_chunks(self, m, addr):
        # a finished video (run_id + kind) or a big web-screen answer (key, see cmd_http)
        fkey = ("http", str(m["key"])) if m.get("key") else (int(m["run_id"]), m.get("kind", "compare"))
        with self.lock:
            f = self.files.get(fkey)
        if not f:
            self.reply(addr, {"id": m.get("id"), "ok": False, "error": "file_info를 먼저 요청하세요"})
            return
        data = f["data"]
        f["at"] = time.time()
        for i in list(m.get("idx", []))[:256]:
            i = int(i)
            payload = data[i * proto.CHUNK_SIZE:(i + 1) * proto.CHUNK_SIZE]
            if payload:
                with self.send_lock:
                    self.sock.sendto(proto.pack_chunk(int(m.get("id", 0)), i, payload), addr)

    # ------------------------------------------------------------------ plumbing
    def reply(self, addr, obj: dict) -> bytes:
        data = proto.encode(obj)
        with self.send_lock:
            self.sock.sendto(data, addr)
        return data

    def handle(self, raw: bytes, addr) -> None:
        m = proto.decode(raw)
        if not m or "cmd" not in m:
            return
        rid = m.get("id")
        if self.password and m.get("pw") != self.password:
            self.reply(addr, {"id": rid, "ok": False, "error": "비밀번호가 틀렸어요"})
            log(f"{addr[0]}: 비밀번호 틀림 ({m.get('cmd')})")
            return
        cmd = m["cmd"]
        if cmd == "chunks":
            self.send_chunks(m, addr)
            return
        key = (addr, rid)
        with self.lock:
            cached = self.replies.get(key)
            busy = key in self.inflight
            if not cached and not busy:
                self.inflight.add(key)
        if cached:  # the same request again (the answer was lost): same answer, nothing done twice
            with self.send_lock:
                self.sock.sendto(cached[1], addr)
            return
        if busy:  # a retry while the first copy is still being worked on (e.g. a slow start): ignore it
            return
        try:
            self._run(m, cmd, key, addr)
        finally:
            with self.lock:
                self.inflight.discard(key)

    def _run(self, m: dict, cmd: str, key: tuple, addr) -> None:
        rid = m.get("id")
        if cmd == "http" and (m.get("method", "GET") != "GET" or not str(m.get("path", "")).startswith(("/api/follow/", "/media/", "/api/health"))):
            log(f"{addr[0]}:{addr[1]} -> 웹 {m.get('method')} {str(m.get('path'))[:80]}")
        elif cmd not in ("status", "http", "upload_part"):  # status / web polling come every few seconds
            log(f"{addr[0]}:{addr[1]} -> {cmd} {({k: v for k, v in m.items() if k not in ('v', 'id', 'cmd', 'pw')})}")
        fn = getattr(self, f"cmd_{cmd}", None)
        try:
            if not fn:
                raise RuntimeError(f"모르는 명령: {cmd}")
            out = {"id": rid, "ok": True, **fn(m)}
        except (KeyError, ValueError, TypeError) as e:
            out = {"id": rid, "ok": False, "error": f"요청 형식 오류: {e}"}
        except RuntimeError as e:
            out = {"id": rid, "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - one bad request must not stop the server
            out = {"id": rid, "ok": False, "error": f"서버 오류: {e}"}
        data = self.reply(addr, out)
        if cmd not in ("ping", "status"):
            with self.lock:
                self.replies[key] = (time.time(), data)

    def cleanup(self) -> None:
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                for k in [k for k, (t, _) in self.replies.items() if now - t > 300]:
                    del self.replies[k]
                for k in [k for k, f in self.files.items() if now - f["at"] > (120 if k[0] == "http" else 600)]:
                    del self.files[k]
                for k in [k for k, u in self.uploads.items() if now - u["at"] > 300]:
                    del self.uploads[k]

    def serve(self) -> None:
        threading.Thread(target=self.cleanup, daemon=True).start()
        while True:
            try:
                raw, addr = self.sock.recvfrom(proto.MAX_DATAGRAM)
            except ConnectionResetError:  # Windows: an ICMP "port unreachable" from an earlier send
                continue
            # slow commands (assemble, download) must not block the other clients
            threading.Thread(target=self.handle, args=(raw, addr), daemon=True).start()


def local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet is sent; this only picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main() -> None:
    cfg_path = base_dir() / "server_config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except ValueError:
            print("server_config.json 을 읽지 못했어요 (JSON 형식 확인)")
    ap = argparse.ArgumentParser(description="AI 영상 파이프라인 UDP 서버")
    ap.add_argument("--port", type=int, default=cfg.get("port", proto.DEFAULT_PORT))
    ap.add_argument("--app", default=cfg.get("app", "http://127.0.0.1:8010"), help="영상 앱 주소")
    ap.add_argument("--password", default=cfg.get("password", os.environ.get("AIVIDEO_UDP_PASSWORD", "")))
    a = ap.parse_args()
    srv = Server(a.port, a.app, a.password)
    print("=" * 60)
    print(" AI 영상 파이프라인 UDP 서버")
    print(f" 포트: UDP {a.port}   영상 앱: {a.app}")
    print(" 클라이언트에 입력할 이 PC의 IP:", ", ".join(local_ips()) or "(찾지 못함 - ipconfig 로 확인)")
    print(" 비밀번호:", "설정됨" if a.password else "없음 (누구나 접속 가능 - --password 로 설정 권장)")
    print(" 창을 닫으면 서버가 꺼집니다.")
    print("=" * 60, flush=True)
    srv.serve()


if __name__ == "__main__":
    main()
