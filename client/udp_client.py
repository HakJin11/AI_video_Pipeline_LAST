"""AI 영상 파이프라인 UDP 클라이언트 (창 프로그램).

서버 PC의 IP·포트·비밀번호를 넣고 연결하면, 따라하기 영상을 고르고 만들기를 시작하고, 진행 상황을 보고,
완성 영상(원본|AI 비교 또는 AI 결과)을 UDP로 받아 '다운로드\\AI영상_받은파일' 폴더에 저장한다.
"""
import base64
import hashlib
import json
import os
import queue
import random
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))  # proto.py sits next to this file
import proto  # noqa: E402

CONFIG = Path(os.environ.get("APPDATA", Path.home())) / "AIVideoClient" / "config.json"
SAVE_DIR = Path.home() / "Downloads" / "AI영상_받은파일"


class UdpError(Exception):
    pass


class UdpClient:
    """Request/answer over UDP with resends, and file download by chunks."""

    def __init__(self, host: str, port: int, password: str, bind_ip: str = ""):
        self.addr = (socket.gethostbyname(host.strip()), int(port))
        self.password = password
        self.bind_ip = bind_ip  # send from this local IP (a PC with several network cards; tests)
        self._id = random.randrange(1, 2 ** 30)
        self._lock = threading.Lock()

    def _socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.bind_ip:
            s.bind((self.bind_ip, 0))
        return s

    def _next_id(self) -> int:
        with self._lock:
            self._id = (self._id + 1) % 2 ** 31
            return self._id

    def request(self, cmd: str, wait: float = 15.0, resend: float = 2.0, **args) -> dict:
        """Send, resend every `resend` s until an answer with our id arrives or `wait` s pass."""
        rid = self._next_id()
        data = proto.encode({"v": proto.VERSION, "id": rid, "cmd": cmd, "pw": self.password, **args})
        s = self._socket()
        try:
            end = time.time() + wait
            while time.time() < end:
                s.sendto(data, self.addr)
                s.settimeout(min(resend, max(0.1, end - time.time())))
                t_next = time.time() + resend
                while time.time() < min(t_next, end):
                    try:
                        raw, _ = s.recvfrom(proto.MAX_DATAGRAM)
                    except socket.timeout:
                        break
                    except ConnectionResetError:  # Windows: nothing listens on that port (yet)
                        time.sleep(0.3)
                        break
                    m = proto.decode(raw)
                    if m and m.get("id") == rid:
                        if not m.get("ok"):
                            raise UdpError(m.get("error") or "서버 오류")
                        return m
            raise UdpError(f"서버({self.addr[0]}:{self.addr[1]})가 응답하지 않아요. IP·포트·방화벽(UDP {self.addr[1]})을 확인하세요")
        finally:
            s.close()

    def download(self, run_id: int, kind: str, dest_dir: Path, progress=None) -> Path:
        info = self.request("file_info", wait=180, resend=5, run_id=run_id, kind=kind)
        data = self._fetch_chunks({"run_id": run_id, "kind": kind}, info["chunks"], info["size"], info["sha256"],
                                  progress)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / info["name"]
        path.write_bytes(data)
        return path

    # ------------------------------------------------------------------ 웹 화면 중계 (2026-10-12)
    UPLOAD_PART = 30000  # bytes of a request body per datagram (base64 ~40 KB)

    def http(self, method: str, path: str, content_type: str | None, body: bytes) -> tuple[int, dict, bytes]:
        """One browser request to the server's web app, carried over UDP (see web_tunnel.py)."""
        args = {"method": method, "path": path, "headers": {"Content-Type": content_type} if content_type else {}}
        if body and len(body) > self.UPLOAD_PART:
            uid = f"{random.getrandbits(64):016x}"
            parts = [body[i:i + self.UPLOAD_PART] for i in range(0, len(body), self.UPLOAD_PART)]
            for i, part in enumerate(parts):
                self.request("upload_part", wait=60, resend=3, upload_id=uid, index=i, total=len(parts),
                             data=base64.b64encode(part).decode("ascii"))
            args["upload_id"] = uid
        elif body:
            args["body_b64"] = base64.b64encode(body).decode("ascii")
        r = self.request("http", wait=900, resend=5, **args)
        if "body_b64" in r:
            data = base64.b64decode(r["body_b64"])
        else:
            data = self._fetch_chunks({"key": r["key"]}, r["chunks"], r["size"], r["sha256"])
        return r["status"], r.get("headers") or {}, data

    def _fetch_chunks(self, which: dict, n: int, size: int, sha256: str, progress=None) -> bytes:
        parts: dict[int, bytes] = {}
        s = self._socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        window, stalls = 64, 0
        try:
            while len(parts) < n:
                want = [i for i in range(n) if i not in parts][:window]
                rid = self._next_id()
                s.sendto(proto.encode({"v": proto.VERSION, "id": rid, "cmd": "chunks", "pw": self.password,
                                       **which, "idx": want}), self.addr)
                got = 0
                end = time.time() + 2.0
                # first chunk may take a moment; after that a 0.3 s gap means the rest of this burst was lost
                s.settimeout(0.8)
                while got < len(want) and time.time() < end:
                    try:
                        raw, _ = s.recvfrom(proto.MAX_DATAGRAM)
                    except socket.timeout:
                        break
                    except ConnectionResetError:
                        break
                    c = proto.unpack_chunk(raw)
                    if c is None:
                        m = proto.decode(raw)
                        if m and not m.get("ok") and m.get("error"):
                            raise UdpError(m["error"])
                        continue
                    _, idx, payload = c
                    if 0 <= idx < n and idx not in parts:
                        parts[idx] = payload
                        got += 1
                        s.settimeout(0.3)
                # a clean link gets bigger bursts; only heavy loss (an overflowing buffer) makes them smaller
                if got == len(want):
                    window = min(256, window * 2)
                elif got < len(want) // 2:
                    window = max(16, window // 2)
                stalls = 0 if got else stalls + 1
                if stalls >= 15:
                    raise UdpError("영상 조각이 계속 오지 않아요 (네트워크가 끊겼거나 방화벽이 막고 있어요)")
                if progress:
                    progress(len(parts), n)
        finally:
            s.close()
        data = b"".join(parts[i] for i in range(n))[:size]
        if hashlib.sha256(data).hexdigest() != sha256:
            raise UdpError("받은 파일이 손상됐어요. 다시 받아 주세요")
        return data


def local_time(iso: str | None) -> str:
    """The server stores UTC ("2026-10-07T04:44:00+00:00"); show this PC's local time."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return (iso or "")[:16].replace("T", " ")


def fmt_sec(s) -> str:
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 60}분 {s % 60}초" if s >= 60 else f"{s}초"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI 영상 파이프라인 - UDP 클라이언트")
        self.geometry("780x720")
        self.minsize(680, 560)
        self.client: UdpClient | None = None
        self.catalog: list[dict] = []
        self.runs: list[dict] = []
        self.current_run: int | None = None
        self.ui_queue: queue.Queue = queue.Queue()
        self.polling = False
        self._build()
        self._load_config()
        self.after(100, self._drain)

    # ------------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        top = ttk.LabelFrame(self, text="1. 서버 연결")
        top.pack(fill="x", **pad)
        ttk.Label(top, text="서버 IP").grid(row=0, column=0, **pad)
        self.ip = ttk.Entry(top, width=18)
        self.ip.grid(row=0, column=1, **pad)
        ttk.Label(top, text="포트").grid(row=0, column=2, **pad)
        self.port = ttk.Entry(top, width=7)
        self.port.grid(row=0, column=3, **pad)
        ttk.Label(top, text="비밀번호").grid(row=0, column=4, **pad)
        self.pw = ttk.Entry(top, width=14, show="*")
        self.pw.grid(row=0, column=5, **pad)
        ttk.Button(top, text="연결", command=self.connect).grid(row=0, column=6, **pad)
        self.conn_label = ttk.Label(top, text="연결 전", foreground="#777")
        self.conn_label.grid(row=1, column=0, columnspan=7, sticky="w", **pad)

        mid = ttk.LabelFrame(self, text="2. 새로 만들기")
        mid.pack(fill="x", **pad)
        ttk.Label(mid, text="영상").grid(row=0, column=0, **pad)
        self.cat_box = ttk.Combobox(mid, state="readonly", width=40)
        self.cat_box.grid(row=0, column=1, **pad)
        self.cat_box.bind("<<ComboboxSelected>>", lambda e: self._fill_lengths())
        ttk.Label(mid, text="길이").grid(row=0, column=2, **pad)
        self.len_box = ttk.Combobox(mid, state="readonly", width=6)
        self.len_box.grid(row=0, column=3, **pad)
        ttk.Button(mid, text="▶ 만들기 시작", command=self.start_new).grid(row=0, column=4, **pad)

        runs = ttk.LabelFrame(self, text="3. 내가 만든 영상 (고르면 진행 상황이 보여요)")
        runs.pack(fill="both", expand=True, **pad)
        self.run_list = tk.Listbox(runs, height=8)
        self.run_list.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.run_list.bind("<<ListboxSelect>>", lambda e: self._select_run())
        sb = ttk.Scrollbar(runs, command=self.run_list.yview)
        sb.pack(side="left", fill="y")
        self.run_list.config(yscrollcommand=sb.set)
        ttk.Button(runs, text="새로고침", command=self.refresh_runs).pack(side="top", padx=6, pady=6)

        st = ttk.LabelFrame(self, text="4. 진행 상황 · 받기")
        st.pack(fill="x", **pad)
        self.status = ttk.Label(st, text="영상을 고르세요", wraplength=700, justify="left")
        self.status.pack(fill="x", padx=6, pady=4)
        self.bar = ttk.Progressbar(st, maximum=100)
        self.bar.pack(fill="x", padx=6, pady=4)
        row = ttk.Frame(st)
        row.pack(fill="x", padx=6, pady=4)
        self.btn_render = ttk.Button(row, text="남은 컷 만들기", command=self.render_rest)
        self.btn_render.pack(side="left", padx=3)
        self.btn_cmp = ttk.Button(row, text="⬇ 원본|AI 비교 영상 받기", command=lambda: self.get_video("compare"))
        self.btn_cmp.pack(side="left", padx=3)
        self.btn_fin = ttk.Button(row, text="⬇ AI 결과만 받기", command=lambda: self.get_video("final"))
        self.btn_fin.pack(side="left", padx=3)
        ttk.Button(row, text="받은 폴더 열기", command=self.open_folder).pack(side="left", padx=3)
        self.dl_label = ttk.Label(st, text="")
        self.dl_label.pack(fill="x", padx=6, pady=2)

        web = ttk.LabelFrame(self, text="5. 웹 화면 — 배경·인물·목소리·컷마다 대사/동작/말투/프롬프트까지 모든 설정")
        web.pack(fill="x", **pad)
        wrow = ttk.Frame(web)
        wrow.pack(fill="x", padx=6, pady=6)
        ttk.Button(wrow, text="🌐 웹 화면 열기", command=lambda: self.open_web("/")).pack(side="left", padx=3)
        ttk.Button(wrow, text="🔧 관리자 화면 열기 (새 영상 가져오기)",
                   command=lambda: self.open_web("/?admin=1")).pack(side="left", padx=3)
        self.web_label = ttk.Label(web, text="서버의 화면을 이 PC 브라우저로 엽니다 (통신은 UDP, 이 창을 닫으면 끊겨요)",
                                   foreground="#555")
        self.web_label.pack(fill="x", padx=6, pady=(0, 6))

    # ------------------------------------------------------------------ helpers
    def _load_config(self):
        try:
            c = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
        self.ip.insert(0, c.get("ip", ""))
        self.port.insert(0, str(c.get("port", proto.DEFAULT_PORT)))
        self.pw.insert(0, c.get("password", ""))

    def _save_config(self):
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({"ip": self.ip.get().strip(), "port": int(self.port.get() or proto.DEFAULT_PORT),
                                          "password": self.pw.get()}, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _bg(self, fn, done=None, fail=None):
        """Run fn in a thread; done(result) / fail(message) back on the window thread."""
        def work():
            try:
                r = fn()
                self.ui_queue.put((done, r) if done else (None, None))
            except (UdpError, OSError, ValueError) as e:
                self.ui_queue.put(((fail or self._error), str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _drain(self):
        try:
            while True:
                cb, arg = self.ui_queue.get_nowait()
                if cb:
                    cb(arg)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _error(self, msg):
        messagebox.showerror("오류", msg)

    def _need(self) -> bool:
        if not self.client:
            messagebox.showinfo("연결 필요", "먼저 서버 IP를 넣고 '연결'을 누르세요")
            return False
        return True

    # ------------------------------------------------------------------ actions
    def connect(self):
        ip = self.ip.get().strip()
        if not ip:
            messagebox.showinfo("서버 IP", "서버 PC의 IP를 넣으세요 (서버 창에 표시돼요)")
            return
        try:
            self.client = UdpClient(ip, int(self.port.get() or proto.DEFAULT_PORT), self.pw.get())
        except (OSError, ValueError) as e:
            self._error(f"주소가 올바르지 않아요: {e}")
            return
        self._save_config()
        self.conn_label.config(text="연결 확인 중…", foreground="#777")

        def work():
            p = self.client.request("ping", wait=8)
            c = self.client.request("catalog", wait=15)
            return p, c

        def done(r):
            p, c = r
            if not p.get("app"):
                self.conn_label.config(text=f"서버는 켜졌지만 영상 앱이 꺼져 있어요: {p.get('error_detail', '')}",
                                       foreground="#c00")
            else:
                ok = lambda v: "켜짐" if v else "꺼짐"  # noqa: E731
                self.conn_label.config(text=f"연결됨 · ComfyUI {ok(p.get('comfyui'))} · Ollama {ok(p.get('ollama'))}",
                                       foreground="#080")
            self.catalog = c["items"]
            self.cat_box["values"] = [t["title"] for t in self.catalog]
            if self.catalog:
                self.cat_box.current(0)
                self._fill_lengths()
            self.refresh_runs()

        def fail(msg):
            self.conn_label.config(text=msg, foreground="#c00")
            self.client = None
        self._bg(work, done, fail)

    def _fill_lengths(self):
        i = self.cat_box.current()
        if i < 0:
            return
        opts = [str(n) for n in self.catalog[i].get("length_options", [])]
        self.len_box["values"] = opts
        if opts:
            self.len_box.current(0)

    def start_new(self):
        if not self._need() or self.cat_box.current() < 0 or not self.len_box.get():
            return
        t = self.catalog[self.cat_box.current()]
        n = int(self.len_box.get())
        if not messagebox.askyesno("만들기 시작", f"'{t['title']}' {n}초 영상을 새로 만들까요?\n(서버 PC의 그래픽카드로 만들어요)"):
            return
        self.status.config(text="시작하는 중…")
        self._bg(lambda: self.client.request("start", wait=120, resend=4, template_id=t["id"], length_sec=n),
                 lambda s: (self.refresh_runs(), self._show(s), self._watch(s["run_id"])))

    def refresh_runs(self):
        if not self._need():
            return

        def done(r):
            self.runs = r["items"]
            self.run_list.delete(0, "end")
            for x in self.runs:
                when = local_time(x.get("created_at"))
                self.run_list.insert("end", f"#{x['run_id']}  {x['title']}  {int(x['length_sec'])}초  {when}"
                                            + ("  ✔ 완성" if x["final"] else ""))
        self._bg(lambda: self.client.request("runs", wait=15), done)

    def _select_run(self):
        sel = self.run_list.curselection()
        if not sel or not self._need():
            return
        self._watch(self.runs[sel[0]]["run_id"])

    def _watch(self, run_id: int):
        self.current_run = run_id
        self._poll_once()

    def _poll_once(self):
        if not self.client or self.current_run is None:
            return
        rid = self.current_run

        def done(s):
            if s["run_id"] != self.current_run:
                return
            self._show(s)
            if s["active"]:
                self.after(5000, self._poll_once)
            elif not s["missing"] and not s["compare"]:
                self.status.config(text=self.status.cget("text") + "\n모든 컷 완성 — 합치는 중…")
                self._bg(lambda: self.client.request("assemble", wait=180, resend=5, run_id=rid), self._show)

        def fail(msg):
            self.status.config(text=f"상태를 못 받았어요 ({msg}) — 5초 뒤 다시 시도")
            self.after(5000, self._poll_once)
        self._bg(lambda: self.client.request("status", wait=10, run_id=rid), done, fail)

    def _show(self, s: dict):
        pct = 100 * s["done"] / max(1, s["cuts"])
        self.bar["value"] = pct
        lines = [f"#{s['run_id']} {s['title']} · {int(s['length_sec'])}초 · 배경: {s.get('background') or '기본'}",
                 f"완성 {s['done']}/{s['cuts']}컷"]
        if s["active"]:
            lines.append(f"만드는 중 — 지금: {s.get('stage') or '차례 기다리는 중'} · 남은 시간 약 {fmt_sec(s.get('eta_sec'))}")
        elif s["missing"]:
            lines.append(f"아직 안 만든 컷: {', '.join(map(str, s['missing']))} — '남은 컷 만들기'를 누르세요"
                         f" (예상 {fmt_sec(s.get('estimate_sec'))})")
        if s["errors"]:
            lines.append("실패한 컷: " + ", ".join(f"{e['cut']}번({e['error']})" for e in s["errors"]))
        if s["compare"]:
            lines.append("✔ 완성 영상 준비됨 — 아래 버튼으로 받으세요")
        self.status.config(text="\n".join(lines))
        ready = "normal" if s["compare"] else "disabled"
        self.btn_cmp.config(state=ready)
        self.btn_fin.config(state="normal" if s["final"] else "disabled")
        self.btn_render.config(state="normal" if (s["missing"] and not s["active"]) else "disabled")

    def render_rest(self):
        if not self._need() or self.current_run is None:
            return
        rid = self.current_run
        self._bg(lambda: self.client.request("render", wait=60, resend=4, run_id=rid),
                 lambda s: (self._show(s), self._poll_once()))

    def get_video(self, kind: str):
        if not self._need() or self.current_run is None:
            return
        rid = self.current_run
        self.dl_label.config(text="받을 준비 중…")
        t0 = time.time()

        def prog(have, total):
            self.ui_queue.put((lambda _: (self.dl_label.config(text=f"받는 중 {have}/{total} 조각 ({100 * have // total}%)"),
                                         self.bar.config(value=100 * have / total)), None))

        def done(path):
            self.dl_label.config(text=f"✔ 저장했어요 ({time.time() - t0:.0f}초): {path}")
            try:
                os.startfile(path)  # opens in the default video player (Windows)
            except (AttributeError, OSError):
                pass
        self._bg(lambda: self.client.download(rid, kind, SAVE_DIR, prog), done,
                 lambda m: (self.dl_label.config(text=f"받기 실패: {m}"), self._error(m)))

    def open_web(self, path: str):
        """Start the local relay (web_tunnel) for the connected server and open the browser on it."""
        if not self._need():
            return
        import webbrowser
        import web_tunnel
        if getattr(self, "_tunnel_for", None) is not self.client:
            if getattr(self, "_tunnel", None):
                self._tunnel.shutdown()
            try:
                self._tunnel, self._tunnel_port = web_tunnel.start(self.client)
            except OSError as e:
                self._error(str(e))
                return
            self._tunnel_for = self.client
        url = f"http://127.0.0.1:{self._tunnel_port}{path}"
        self.web_label.config(text=f"열었어요: {url}  (서버 {self.client.addr[0]} 와 UDP로 연결 · 이 창을 닫으면 끊겨요)",
                              foreground="#080")
        webbrowser.open(url)

    def open_folder(self):
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(SAVE_DIR)
        except (AttributeError, OSError):
            messagebox.showinfo("폴더", str(SAVE_DIR))


if __name__ == "__main__":
    App().mainloop()
