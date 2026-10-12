"""웹 화면 중계 (2026-10-12): 서버 PC의 영상 앱 웹 화면 전체를 이 PC의 브라우저로 연다. 통신은 UDP.

브라우저 ──HTTP──▶ 이 PC 127.0.0.1:8765 (이 파일) ──UDP──▶ 서버 PC AIVideoServer ──HTTP──▶ 영상 앱 :8010

배경·인물·목소리·컷마다 대사/동작/말투/구도/프롬프트, 관리자 화면(/?admin=1)까지 서버에서 쓰는 화면과 똑같다.
사진(/media/...)은 한 번 받으면 잠깐 이 PC에 두고, 영상은 브라우저가 앞뒤로 넘길 수 있게 Range 요청을 이 PC에서 처리한다.
"""
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MEDIA_TTL = 600  # s a picture / clip stays in this PC's memory
MEDIA_MAX = 800 * 2 ** 20  # bytes kept at most


class _Cache:
    def __init__(self):
        self.items: dict[str, tuple[float, int, dict, bytes]] = {}
        self.lock = threading.Lock()
        self.fetching: dict[str, threading.Event] = {}

    def get(self, key):
        with self.lock:
            it = self.items.get(key)
            if it and time.time() - it[0] < MEDIA_TTL:
                return it
            self.items.pop(key, None)
            return None

    def put(self, key, status, headers, data):
        with self.lock:
            self.items[key] = (time.time(), status, headers, data)
            total = sum(len(v[3]) for v in self.items.values())
            for k in sorted(self.items, key=lambda k: self.items[k][0]):
                if total <= MEDIA_MAX:
                    break
                total -= len(self.items.pop(k)[3])


def make_server(client, port: int = 8765) -> ThreadingHTTPServer:
    """client: udp_client.UdpClient. Returns a started-ready HTTP server bound to 127.0.0.1 only."""
    cache = _Cache()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            self._relay()

        def do_POST(self):
            self._relay()

        def do_PATCH(self):
            self._relay()

        def do_PUT(self):
            self._relay()

        def do_DELETE(self):
            self._relay()

        def do_HEAD(self):
            self._relay(head=True)

        def _relay(self, head: bool = False):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            method = "GET" if head else self.command
            try:
                if method == "GET" and self.path.startswith("/media/"):
                    hit = cache.get(self.path)
                    if not hit:
                        status, hdrs, data = client.http("GET", self.path, None, b"")
                        if status == 200:
                            cache.put(self.path, status, hdrs, data)
                    else:
                        _, status, hdrs, data = hit
                else:
                    status, hdrs, data = client.http(method, self.path, self.headers.get("Content-Type"), body)
            except Exception as e:  # noqa: BLE001 - shown in the browser instead of a dead tab
                msg = f"서버와 통신하지 못했어요: {e}".encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            # videos: the browser asks for byte ranges
            rng = self.headers.get("Range")
            m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
            if m and status == 200 and method == "GET" and data:
                a = int(m.group(1)) if m.group(1) else max(0, len(data) - int(m.group(2) or 0))
                b = int(m.group(2)) if (m.group(1) and m.group(2)) else len(data) - 1
                b = min(b, len(data) - 1)
                part = data[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                out = part
            else:
                self.send_response(status)
                out = data
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            if not head:
                self.wfile.write(out)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start(client, port: int = 8765) -> tuple[ThreadingHTTPServer, int]:
    """Start the relay in a background thread on the first free port from `port`."""
    last = None
    for p in range(port, port + 20):
        try:
            srv = make_server(client, p)
        except OSError as e:
            last = e
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, p
    raise OSError(f"중계용 포트를 열지 못했어요: {last}")
