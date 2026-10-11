#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网复制 (LanCopy)
- UDP 自动发现 + TCP 传输
- 文字：即时同步剪贴板
- 文件：Ctrl+C 只发名单(file_offer)；对端点「拉取待传文件」才下载到接收目录并写入 CF_HDROP
  （避免无意复制误传；托盘右键也可拉取；文字仍即时同步）
- 大文件：二进制分块（无 base64），边收边写，可续传
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

# ---- optional deps ----
_HAS_WIN32 = False
try:
    if sys.platform == "win32":
        import win32clipboard  # type: ignore
        import win32con  # type: ignore

        _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False

_HAS_TRAY = False
try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore

    _HAS_TRAY = True
except Exception:
    pystray = None  # type: ignore
    Image = ImageDraw = None  # type: ignore

tk = None  # type: ignore
ttk = filedialog = messagebox = scrolledtext = None  # type: ignore


def _ensure_tk():
    global tk, ttk, filedialog, messagebox, scrolledtext
    if tk is not None:
        return
    import tkinter as _tk
    from tkinter import ttk as _ttk, filedialog as _fd, messagebox as _mb, scrolledtext as _st

    tk, ttk, filedialog, messagebox, scrolledtext = _tk, _ttk, _fd, _mb, _st


APP_NAME = "局域网复制"
DEFAULT_PORT = 3721
DISCOVER_PORT = 3722
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB 二进制块
PROTO_VERSION = 2
HEADER = struct.Struct(">I")
DISCOVER_TYPE = "lan_copy_hello"
DISCOVER_INTERVAL = 2.0
PEER_STALE_SEC = 8.0
MAX_JSON_FRAME = 16 * 1024 * 1024


# ============================== 协议 ==============================


def encode_frame(obj: dict) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HEADER.pack(len(payload)) + payload


def try_decode_one(buf: bytearray) -> Optional[dict]:
    """尝试从缓冲拆一帧 JSON；不够则返回 None；若是 bin_chunk 需调用方再读 raw。"""
    if len(buf) < HEADER.size:
        return None
    (n,) = HEADER.unpack_from(buf, 0)
    if n < 0 or n > MAX_JSON_FRAME:
        raise ValueError(f"非法帧长度: {n}")
    if len(buf) < HEADER.size + n:
        return None
    raw = bytes(buf[HEADER.size : HEADER.size + n])
    del buf[: HEADER.size + n]
    return json.loads(raw.decode("utf-8"))


def decode_frames(buf: bytearray) -> list[dict]:
    """仅用于单测：连续 JSON 帧（不含 bin 后续）。"""
    msgs: list[dict] = []
    while True:
        m = try_decode_one(buf)
        if m is None:
            break
        if m.get("type") == "bin_chunk":
            # 单测里若带 _raw 字段则嵌入；真实网络由 PeerSession 处理
            n = int(m.get("n") or 0)
            if len(buf) < n:
                # 放回帧头，等待更多数据——单测应保证数据完整
                raise ValueError("bin_chunk 缺少后续字节（测试数据不完整）")
            m["_data"] = bytes(buf[:n])
            del buf[:n]
        msgs.append(m)
    return msgs


def clipboard_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_hello(peer_name: str = "") -> dict:
    return {
        "type": "hello",
        "ver": PROTO_VERSION,
        "password": "",
        "name": peer_name or socket.gethostname(),
        "ts": time.time(),
    }


def make_hello_ok(ok: bool = True, reason: str = "") -> dict:
    return {"type": "hello_ok", "ok": bool(ok), "reason": reason, "ver": PROTO_VERSION}


def make_discover_hello(node_id: str, tcp_port: int, name: str = "") -> dict:
    return {
        "type": DISCOVER_TYPE,
        "ver": PROTO_VERSION,
        "id": node_id,
        "tcp_port": int(tcp_port),
        "name": name or socket.gethostname(),
        "ts": time.time(),
    }


def make_clipboard_text(text: str, seq: int) -> dict:
    return {
        "type": "clipboard_text",
        "text": text,
        "hash": clipboard_text_hash(text),
        "seq": seq,
    }


def make_file_offer(offer_id: str, files: list[dict]) -> dict:
    """files: [{id, name, size}]"""
    return {"type": "file_offer", "offer_id": offer_id, "files": files, "ver": PROTO_VERSION}


def make_file_request(offer_id: str, file_id: str, offset: int, req_id: str) -> dict:
    return {
        "type": "file_request",
        "offer_id": offer_id,
        "file_id": file_id,
        "offset": int(offset),
        "req_id": req_id,
    }


def make_bin_chunk_hdr(file_id: str, offset: int, n: int, req_id: str) -> dict:
    return {
        "type": "bin_chunk",
        "id": file_id,
        "offset": int(offset),
        "n": int(n),
        "req_id": req_id,
    }


def make_file_stream_done(file_id: str, req_id: str, ok: bool = True, reason: str = "") -> dict:
    return {
        "type": "file_stream_done",
        "id": file_id,
        "req_id": req_id,
        "ok": bool(ok),
        "reason": reason,
    }


def make_ping() -> dict:
    return {"type": "ping", "ts": time.time()}


def make_pong(ts: float = 0) -> dict:
    return {"type": "pong", "ts": ts or time.time()}


# ============================== 网络辅助 ==============================


def list_lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except Exception:
        pass
    if not ips:
        ips.append("127.0.0.1")
    return ips


def default_recv_dir() -> Path:
    desk = Path.home() / "Desktop"
    if not desk.is_dir():
        desk = Path.home() / "桌面"
    if not desk.is_dir():
        desk = Path.home()
    return desk / "局域网接收"


def build_hdrop_blob(paths: list[str]) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DROPFILES(ctypes.Structure):
        _fields_ = [
            ("pFiles", wintypes.DWORD),
            ("pt", wintypes.POINT),
            ("fNC", wintypes.BOOL),
            ("fWide", wintypes.BOOL),
        ]

    df = DROPFILES()
    df.pFiles = ctypes.sizeof(DROPFILES)
    df.pt.x = 0
    df.pt.y = 0
    df.fNC = 0
    df.fWide = 1
    nul = chr(0)
    files_bytes = (nul.join(paths) + nul + nul).encode("utf-16-le")
    return bytes(df) + files_bytes


# ============================== 剪贴板 ==============================


class ClipboardHelper:
    def __init__(self, root: Any):
        self.root = root
        self._last_set_hash: Optional[str] = None
        self._last_set_files_sig: Optional[str] = None
        self._suppress_until = 0.0
        self._seq = 0

    def get_text(self) -> Optional[str]:
        if _HAS_WIN32:
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                        return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
                        data = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                        if isinstance(data, bytes):
                            return data.decode("gbk", errors="ignore")
                        return str(data) if data else None
                finally:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass
            except Exception:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        try:
            return self.root.clipboard_get()
        except Exception:
            return None

    def set_text(self, text: str) -> None:
        h = clipboard_text_hash(text)
        self._last_set_hash = h
        self._last_set_files_sig = None
        self._suppress_until = time.time() + 2.0
        if _HAS_WIN32:
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                finally:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass
                return
            except Exception:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            pass

    def set_files(self, paths: list[str]) -> bool:
        paths = [str(Path(p).resolve()) for p in paths if p and Path(p).exists()]
        if not paths:
            return False
        sig = "|".join(paths)
        self._last_set_files_sig = sig
        self._last_set_hash = None
        self._suppress_until = time.time() + 2.0
        if not _HAS_WIN32:
            self.set_text(chr(10).join(paths))
            self._last_set_files_sig = sig
            return False
        try:
            import ctypes
            from ctypes import wintypes
            import struct as _struct

            blob = build_hdrop_blob(paths)
            GMEM_MOVEABLE = 0x0002
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(blob))
            if not handle:
                raise OSError("GlobalAlloc 失败")
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, blob, len(blob))
            kernel32.GlobalUnlock(handle)
            effect_handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, 4)
            if effect_handle:
                ep = kernel32.GlobalLock(effect_handle)
                if ep:
                    ctypes.memmove(ep, _struct.pack("<I", 2), 4)
                    kernel32.GlobalUnlock(effect_handle)
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_HDROP, int(handle))
                if effect_handle:
                    fmt = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
                    win32clipboard.SetClipboardData(fmt, int(effect_handle))
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
            return True
        except Exception:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
            return False

    def set_delayed_hdrop(self, hwnd: int) -> bool:
        """声明延迟 CF_HDROP：等 Explorer GetClipboardData/粘贴时再渲染。"""
        if not _HAS_WIN32:
            return False
        try:
            win32clipboard.OpenClipboard(hwnd)
            try:
                win32clipboard.EmptyClipboard()
                # NULL/0 = delayed rendering
                win32clipboard.SetClipboardData(win32con.CF_HDROP, 0)
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
            self._last_set_files_sig = "__delayed__"
            self._last_set_hash = None
            self._suppress_until = time.time() + 2.0
            return True
        except Exception:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
            return False

    def should_ignore_local(self, text: str) -> bool:
        h = clipboard_text_hash(text)
        if self._last_set_hash and h == self._last_set_hash:
            return True
        return False

    def should_ignore_files(self, paths: list[str]) -> bool:
        sig = "|".join(paths)
        if self._last_set_files_sig == "__delayed__":
            return True
        return bool(self._last_set_files_sig and sig == self._last_set_files_sig)

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def get_file_paths(self) -> list[str]:
        if not _HAS_WIN32:
            return []
        paths: list[str] = []
        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                        data = win32clipboard.GetClipboardData(win32con.CF_HDROP)
                        if data:
                            if isinstance(data, (list, tuple)):
                                paths = [str(p) for p in data]
                            else:
                                paths = [str(data)]
                finally:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass
                break
            except Exception:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
                time.sleep(0.05)
        out = []
        for p in paths:
            try:
                if p and Path(p).exists():
                    out.append(str(Path(p)))
            except Exception:
                pass
        return out


# ============================== 会话 ==============================


class PeerSession:
    def __init__(
        self,
        sock: socket.socket,
        on_message: Callable[[dict], None],
        on_close: Callable[[], None],
        log: Callable[[str], None],
    ):
        self.sock = sock
        self.on_message = on_message
        self.on_close = on_close
        self.log = log
        self._send_lock = threading.Lock()
        self._alive = True
        self._buf = bytearray()
        self.peer_name = ""
        threading.Thread(target=self._recv_loop, name="recv", daemon=True).start()

    def send(self, obj: dict) -> bool:
        if not self._alive:
            return False
        data = encode_frame(obj)
        with self._send_lock:
            try:
                self.sock.sendall(data)
                return True
            except Exception as e:
                self.log(f"发送失败: {e}")
                self.close()
                return False

    def send_bin_chunk(self, file_id: str, offset: int, data: bytes, req_id: str) -> bool:
        if not self._alive:
            return False
        hdr = encode_frame(make_bin_chunk_hdr(file_id, offset, len(data), req_id))
        with self._send_lock:
            try:
                self.sock.sendall(hdr)
                self.sock.sendall(data)
                return True
            except Exception as e:
                self.log(f"发送块失败: {e}")
                self.close()
                return False

    def _recv_exact_from_buf(self, n: int) -> Optional[bytes]:
        """从已有缓冲或 socket 再读，凑齐 n 字节。"""
        while len(self._buf) < n and self._alive:
            try:
                chunk = self.sock.recv(max(65536, min(CHUNK_SIZE, n - len(self._buf) + 65536)))
            except OSError:
                return None
            if not chunk:
                return None
            self._buf.extend(chunk)
        if len(self._buf) < n:
            return None
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _recv_loop(self) -> None:
        try:
            while self._alive:
                try:
                    chunk = self.sock.recv(256 * 1024)
                except OSError:
                    break
                if not chunk:
                    break
                self._buf.extend(chunk)
                while self._alive:
                    try:
                        msg = try_decode_one(self._buf)
                    except Exception as e:
                        self.log(f"协议错误: {e}")
                        return
                    if msg is None:
                        break
                    if msg.get("type") == "bin_chunk":
                        n = int(msg.get("n") or 0)
                        raw = self._recv_exact_from_buf(n)
                        if raw is None:
                            return
                        msg["_data"] = raw
                    try:
                        self.on_message(msg)
                    except Exception:
                        self.log("消息处理异常:\n" + traceback.format_exc())
        finally:
            self.close()

    def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            self.on_close()
        except Exception:
            pass


# ============================== 引擎 ==============================


class LanCopyEngine:
    def __init__(self, ui_queue: queue.Queue):
        self.ui_queue = ui_queue
        self.port = DEFAULT_PORT
        self.discover_port = DISCOVER_PORT
        self.peer_ip = ""
        self.recv_dir = default_recv_dir()
        self.open_after_recv = False
        self.node_id = str(uuid.uuid4())
        self.node_name = socket.gethostname()

        self._listen_sock: Optional[socket.socket] = None
        self._udp_sock: Optional[socket.socket] = None
        self._session: Optional[PeerSession] = None
        self._session_lock = threading.Lock()
        self._running = False
        self._want_client = False
        self._connecting = False
        self._peers_lock = threading.Lock()
        self._peers: dict[str, dict] = {}

        self._clip: Optional[ClipboardHelper] = None
        self._last_clip_hash: Optional[str] = None
        self._last_hdrop_sig: Optional[str] = None

        # 本机作为「复制源」：offer_id -> {file_id: Path}
        self._local_offers: dict[str, dict[str, Path]] = {}
        # 对端 offer：当前延迟剪贴板
        self._remote_offer: Optional[dict] = None  # {offer_id, files:[{id,name,size}]}
        self._remote_offer_lock = threading.Lock()
        # 拉取等待：req_id -> {event, ok, error, file_path, got, size}
        self._pull_wait: dict[str, dict] = {}
        self._pull_lock = threading.Lock()
        self._hwnd = 0  # 可选：延迟剪贴板窗口（已非主路径）
        self._rendering = False
        self._auto_pulling_id: Optional[str] = None
        self._auto_pull_lock = threading.Lock()

    def post(self, kind: str, **kw: Any) -> None:
        self.ui_queue.put({"kind": kind, **kw})

    def log(self, msg: str) -> None:
        self.post("log", text=msg)

    def set_clipboard(self, clip: ClipboardHelper) -> None:
        self._clip = clip

    def set_hwnd(self, hwnd: int) -> None:
        self._hwnd = int(hwnd or 0)

    def start(self, port: int, peer_ip: str, recv_dir: Path, discover_port: int = DISCOVER_PORT) -> None:
        self.stop()
        self.port = int(port)
        self.discover_port = int(discover_port)
        self.peer_ip = (peer_ip or "").strip()
        self.recv_dir = Path(recv_dir)
        self.recv_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._start_listener()
        self._start_discovery()
        self._want_client = True
        threading.Thread(target=self._client_loop, name="client", daemon=True).start()
        self.log(f"已启动 TCP :{self.port} / UDP :{self.discover_port} id={self.node_id[:8]}…")
        self.log("文件：复制只同步名单；对端点「拉取待传文件」（或托盘右键）才下载")
        self.post("status", text="发现中…", connected=False)
        self._publish_peers()

    def stop(self) -> None:
        self._running = False
        self._want_client = False
        sess = None
        with self._session_lock:
            sess = self._session
            self._session = None
        if sess:
            try:
                sess.close()
            except Exception:
                pass
        if self._listen_sock:
            try:
                self._listen_sock.close()
            except Exception:
                pass
            self._listen_sock = None
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except Exception:
                pass
            self._udp_sock = None
        with self._peers_lock:
            self._peers.clear()
        self.post("discovered", peers=[])
        self.post("status", text="已停止", connected=False)

    def _start_listener(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", self.port))
            s.listen(5)
        except OSError as e:
            self.log(f"TCP 监听失败: {e}")
            s.close()
            return
        self._listen_sock = s
        threading.Thread(target=self._accept_loop, name="accept", daemon=True).start()

    def _start_discovery(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        try:
            s.bind(("0.0.0.0", self.discover_port))
        except OSError as e:
            self.log(f"UDP 发现绑定失败: {e}")
            s.close()
            return
        s.settimeout(0.5)
        self._udp_sock = s
        threading.Thread(target=self._discover_recv_loop, name="udp-recv", daemon=True).start()
        threading.Thread(target=self._discover_bcast_loop, name="udp-bcast", daemon=True).start()

    def _discover_payload(self) -> bytes:
        return json.dumps(
            make_discover_hello(self.node_id, self.port, self.node_name),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _discover_bcast_loop(self) -> None:
        targets = ["255.255.255.255"]
        for ip in list_lan_ips():
            parts = ip.split(".")
            if len(parts) == 4 and not ip.startswith("127."):
                targets.append(".".join(parts[:3] + ["255"]))
        while self._running and self._udp_sock:
            payload = self._discover_payload()
            for t in targets:
                try:
                    self._udp_sock.sendto(payload, (t, self.discover_port))
                except Exception:
                    pass
            self._prune_peers()
            time.sleep(DISCOVER_INTERVAL)

    def _discover_recv_loop(self) -> None:
        while self._running and self._udp_sock:
            try:
                data, addr = self._udp_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("type") != DISCOVER_TYPE:
                continue
            peer_id = str(msg.get("id") or "")
            if not peer_id or peer_id == self.node_id:
                continue
            with self._peers_lock:
                self._peers[peer_id] = {
                    "id": peer_id,
                    "ip": addr[0],
                    "tcp_port": int(msg.get("tcp_port") or self.port),
                    "name": msg.get("name") or peer_id[:8],
                    "last_seen": time.time(),
                }
            self._publish_peers()

    def _prune_peers(self) -> None:
        now = time.time()
        changed = False
        with self._peers_lock:
            dead = [k for k, v in self._peers.items() if now - v.get("last_seen", 0) > PEER_STALE_SEC]
            for k in dead:
                del self._peers[k]
                changed = True
        if changed:
            self._publish_peers()

    def _publish_peers(self) -> None:
        with self._peers_lock:
            peers = sorted(self._peers.values(), key=lambda x: x.get("name") or "")
        self.post("discovered", peers=list(peers))
        with self._session_lock:
            connected = self._session is not None
        if not connected and self._running:
            if peers:
                names = ", ".join(f"{p['name']}({p['ip']})" for p in peers[:3])
                self.post("status", text=f"已发现 {len(peers)} 台：{names}", connected=False)
            else:
                self.post("status", text="发现中…", connected=False)

    def _pick_connect_target(self) -> Optional[tuple[str, int, str, str]]:
        if self.peer_ip:
            return (self.peer_ip, self.port, self.peer_ip, "manual")
        with self._peers_lock:
            peers = list(self._peers.values())
        candidates = [p for p in peers if self.node_id < p["id"]]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (-p.get("last_seen", 0), p["id"]))
        p = candidates[0]
        return (p["ip"], int(p["tcp_port"]), p.get("name") or "", p["id"])

    def _accept_loop(self) -> None:
        while self._running and self._listen_sock:
            try:
                self._listen_sock.settimeout(1.0)
                conn, addr = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.log(f"接入 {addr[0]}:{addr[1]}")
            self._attach_socket(conn)

    def _client_loop(self) -> None:
        backoff = 1.0
        while self._running and self._want_client:
            with self._session_lock:
                has = self._session is not None
            if has or self._connecting:
                time.sleep(0.5)
                backoff = 1.0
                continue
            target = self._pick_connect_target()
            if not target:
                time.sleep(0.5)
                continue
            ip, port, name, _pid = target
            self._connecting = True
            try:
                self.post("status", text=f"连接中 {name or ip}:{port}…", connected=False)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect((ip, port))
                s.settimeout(None)
                self.log(f"已连上 {name or ''} {ip}:{port}")
                self._attach_socket(s)
                backoff = 1.0
            except Exception as e:
                self.log(f"连接失败: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 8.0)
            finally:
                self._connecting = False

    def _attach_socket(self, sock: socket.socket) -> None:
        with self._session_lock:
            if self._session:
                try:
                    sock.close()
                except Exception:
                    pass
                self.log("已有连接，忽略")
                return

            def on_msg(m: dict) -> None:
                self._handle_message(m, sess)

            def on_close() -> None:
                with self._session_lock:
                    if self._session is sess:
                        self._session = None
                self.log("连接已断开")
                if self._running:
                    self._publish_peers()
                else:
                    self.post("status", text="已停止", connected=False)

            sess = PeerSession(sock, on_msg, on_close, self.log)
            self._session = sess
        sess.send(make_hello(self.node_name))

    def _handle_message(self, m: dict, sess: PeerSession) -> None:
        t = m.get("type")
        if t == "hello":
            sess.send(make_hello_ok(True))
            sess.peer_name = m.get("name") or ""
            self.post("status", text=f"已连接：{sess.peer_name}", connected=True)
            self.log(f"握手成功（{sess.peer_name}）")
        elif t == "hello_ok":
            if m.get("ok") is False:
                self.log(f"握手被拒: {m.get('reason')}")
                sess.close()
                return
            self.post("status", text="已连接：对端", connected=True)
            self.log("对方确认握手")
        elif t == "clipboard_text":
            text = m.get("text") or ""
            if self._clip:
                self._last_clip_hash = m.get("hash") or clipboard_text_hash(text)
                self._clip.set_text(text)
            preview = text.replace("\n", " ")[:80]
            self.log(f"← 文字 ({len(text)} 字): {preview}")
        elif t == "file_offer":
            self._on_file_offer(m)
        elif t == "file_offer_autopull":
            # 兼容旧对端：不再自动下载，仍需本机点「拉取」
            self.log("对端请求推送文件；请本机点「拉取待传文件」或托盘右键拉取")
        elif t == "file_request":
            threading.Thread(target=self._serve_file_request, args=(m, sess), daemon=True).start()
        elif t == "bin_chunk":
            self._on_bin_chunk(m)
        elif t == "file_stream_done":
            self._on_stream_done(m)
        elif t == "ping":
            sess.send(make_pong(m.get("ts") or 0))
        elif t == "pong":
            pass
        else:
            self.log(f"未知消息: {t}")

    # ---- 复制端：只发 offer ----

    def poll_clipboard(self) -> None:
        if not self._clip:
            return
        with self._session_lock:
            sess = self._session
        if not sess:
            return
        # 若本机持有「延迟 CF_HDROP」，禁止 GetClipboardData，否则会触发自渲染/误拉取
        if getattr(self._clip, "_last_set_files_sig", None) == "__delayed__":
            text = self._clip.get_text()
            if text is None:
                return
            h = clipboard_text_hash(text)
            if h == self._last_clip_hash or self._clip.should_ignore_local(text):
                self._last_clip_hash = h
                return
            self._last_clip_hash = h
            sess.send(make_clipboard_text(text, self._clip.next_seq()))
            return
        paths = self._clip.get_file_paths()
        if paths:
            sig = "|".join(paths)
            if self._clip.should_ignore_files(paths) or sig == self._last_hdrop_sig:
                self._last_hdrop_sig = sig
                return
            self._last_hdrop_sig = sig
            self._send_file_offer(paths)
            return
        text = self._clip.get_text()
        if text is None:
            return
        h = clipboard_text_hash(text)
        if h == self._last_clip_hash:
            return
        if self._clip.should_ignore_local(text):
            self._last_clip_hash = h
            return
        self._last_clip_hash = h
        seq = self._clip.next_seq()
        if sess.send(make_clipboard_text(text, seq)):
            preview = text.replace("\n", " ")[:80]
            self.log(f"→ 文字 ({len(text)} 字): {preview}")

    def _send_file_offer(self, paths: list[str]) -> None:
        with self._session_lock:
            sess = self._session
        if not sess:
            return
        files_meta = []
        mapping: dict[str, Path] = {}
        for p in paths:
            path = Path(p)
            if path.is_file():
                fid = hashlib.md5(f"{path}:{path.stat().st_mtime_ns}".encode()).hexdigest()[:16]
                files_meta.append({"id": fid, "name": path.name, "size": path.stat().st_size})
                mapping[fid] = path
            elif path.is_dir():
                base = path
                for root, _dirs, fnames in os.walk(path):
                    for fn in fnames:
                        fp = Path(root) / fn
                        rel = str(fp.relative_to(base.parent)).replace("\\", "/")
                        fid = hashlib.md5(f"{fp}:{fp.stat().st_mtime_ns}".encode()).hexdigest()[:16]
                        files_meta.append({"id": fid, "name": rel, "size": fp.stat().st_size})
                        mapping[fid] = fp
        if not files_meta:
            return
        offer_id = uuid.uuid4().hex[:12]
        self._local_offers[offer_id] = mapping
        # 限制缓存数量
        while len(self._local_offers) > 8:
            self._local_offers.pop(next(iter(self._local_offers)))
        if sess.send(make_file_offer(offer_id, files_meta)):
            total = sum(f["size"] for f in files_meta)
            self.log(
                f"→ 文件名单 {len(files_meta)} 个（{self._fmt_size(total)}），"
                f"等待对端点「拉取」 offer={offer_id}"
            )

    def _serve_file_request(self, m: dict, sess: PeerSession) -> None:
        offer_id = m.get("offer_id") or ""
        file_id = m.get("file_id") or ""
        offset = int(m.get("offset") or 0)
        req_id = m.get("req_id") or ""
        mapping = self._local_offers.get(offer_id) or {}
        path = mapping.get(file_id)
        if not path or not path.is_file():
            sess.send(make_file_stream_done(file_id, req_id, False, "文件不存在或 offer 过期"))
            return
        try:
            size = path.stat().st_size
            sent = offset
            t0 = time.time()
            with open(path, "rb") as f:
                if offset:
                    f.seek(offset)
                while True:
                    data = f.read(CHUNK_SIZE)
                    if not data:
                        break
                    if not sess.send_bin_chunk(file_id, sent, data, req_id):
                        return
                    sent += len(data)
                    elapsed = max(time.time() - t0, 1e-3)
                    self.post(
                        "progress",
                        value=(sent / size * 100) if size else 100,
                        text=f"上传 {path.name}  {self._fmt_size(sent)}/{self._fmt_size(size)}  {self._fmt_size(sent/elapsed)}/s",
                    )
            sess.send(make_file_stream_done(file_id, req_id, True))
            self.log(f"→ 已按需发送 {path.name} ({self._fmt_size(size)})")
        except Exception as e:
            sess.send(make_file_stream_done(file_id, req_id, False, str(e)))
            self.log(f"发送文件失败: {e}")

    # ---- 接收端：offer → 延迟剪贴板；粘贴时拉取 ----

    def _on_file_offer(self, m: dict) -> None:
        """收到名单：只缓存，不下载；等用户点「拉取待传文件」或托盘菜单。"""
        offer_id = m.get("offer_id") or ""
        files = m.get("files") or []
        with self._remote_offer_lock:
            self._remote_offer = {"offer_id": offer_id, "files": files}
        names = ", ".join((f.get("name") or "?") for f in files[:5])
        total = sum(int(f.get("size") or 0) for f in files)
        self.log(
            f"← 文件名单 {len(files)} 个（{self._fmt_size(total)}）：{names}"
            f"{'…' if len(files) > 5 else ''} — 请点「拉取待传文件」或托盘右键拉取"
        )
        self.post("pending_offer", count=len(files), total=total, names=names)
        self.post("tray_tip", text=f"有 {len(files)} 个待传文件，点托盘「拉取待传文件」")

    def _manual_pull_and_set_clipboard(self, offer_id: str) -> None:
        with self._auto_pull_lock:
            if self._auto_pulling_id == offer_id:
                self.log("正在拉取中，请稍候…")
                return
            self._auto_pulling_id = offer_id
        try:
            with self._remote_offer_lock:
                offer = self._remote_offer
            if not offer or (offer.get("offer_id") or "") != offer_id:
                self.log("没有待传文件名单（可能已过期）")
                return
            paths = self._pull_offer_files(offer)
            if not paths:
                self.log("← 拉取失败（无文件落地）")
                return
            if self._clip and self._clip.set_files(paths):
                self.log(
                    f"← 已下载并放入剪贴板 {len(paths)} 个文件，可在本机 Ctrl+V 粘贴"
                )
                self.post("tray_tip", text=f"文件已就绪，可粘贴（{len(paths)}）")
            else:
                self.log(
                    f"← 已下载到 {self.recv_dir}，写入剪贴板失败；请到接收目录取文件"
                )
                self.post("tray_tip", text="文件已下载到接收目录")
            self.post("pending_offer", count=0, total=0, names="")
        except Exception as e:
            self.log(f"← 拉取异常: {e}")
        finally:
            with self._auto_pull_lock:
                if self._auto_pulling_id == offer_id:
                    self._auto_pulling_id = None

    def render_delayed_hdrop(self) -> bool:
        """WM_RENDERFORMAT 回调：拉取文件并 SetClipboardData(CF_HDROP)。"""
        if self._rendering:
            return False
        with self._remote_offer_lock:
            offer = self._remote_offer
        if not offer:
            return False
        self._rendering = True
        try:
            paths = self._pull_offer_files(offer)
            if not paths:
                return False
            return self._set_clipboard_hdrop_now(paths)
        finally:
            self._rendering = False

    def _set_clipboard_hdrop_now(self, paths: list[str]) -> bool:
        if not _HAS_WIN32:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            import struct as _struct

            blob = build_hdrop_blob(paths)
            GMEM_MOVEABLE = 0x0002
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(blob))
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, blob, len(blob))
            kernel32.GlobalUnlock(handle)
            # 渲染时剪贴板应由本窗口持有，直接 SetClipboardData
            win32clipboard.SetClipboardData(win32con.CF_HDROP, int(handle))
            try:
                effect = kernel32.GlobalAlloc(GMEM_MOVEABLE, 4)
                ep = kernel32.GlobalLock(effect)
                ctypes.memmove(ep, _struct.pack("<I", 2), 4)
                kernel32.GlobalUnlock(effect)
                fmt = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
                win32clipboard.SetClipboardData(fmt, int(effect))
            except Exception:
                pass
            if self._clip:
                self._clip._last_set_files_sig = "|".join(paths)
                self._last_hdrop_sig = "|".join(paths)
            self.log(f"← 粘贴触发完成，已提供 {len(paths)} 个文件给系统剪贴板")
            return True
        except Exception as e:
            self.log(f"写入 HDROP 失败: {e}")
            return False

    def _pull_offer_files(self, offer: dict) -> list[str]:
        offer_id = offer.get("offer_id") or ""
        files = offer.get("files") or []
        out_paths: list[str] = []
        for meta in files:
            fid = meta.get("id") or ""
            name = (meta.get("name") or "file").replace("\\", "/")
            name = "/".join(p for p in name.split("/") if p not in ("", ".", ".."))
            size = int(meta.get("size") or 0)
            dest = self.recv_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 断点：已有部分文件则续传
            offset = 0
            if dest.exists():
                existing = dest.stat().st_size
                if existing == size and size > 0:
                    out_paths.append(str(dest.resolve()))
                    self.log(f"← 已存在完整文件，跳过: {name}")
                    continue
                if existing < size:
                    offset = existing
                    self.log(f"← 续传 {name} 从 {self._fmt_size(offset)}")
                else:
                    # 尺寸异常，重写
                    offset = 0
            ok = self._pull_one_file(offer_id, fid, dest, size, offset)
            if ok:
                out_paths.append(str(dest.resolve()))
            else:
                self.log(f"← 拉取失败: {name}")
        if out_paths and self.open_after_recv:
            self.post("open_dir", path=str(Path(out_paths[0]).parent))
        return out_paths

    def _pull_one_file(self, offer_id: str, file_id: str, dest: Path, size: int, offset: int) -> bool:
        with self._session_lock:
            sess = self._session
        if not sess:
            return False
        req_id = uuid.uuid4().hex[:10]
        done = threading.Event()
        state = {
            "event": done,
            "ok": False,
            "error": "",
            "got": offset,
            "size": size,
            "fh": None,
            "dest": dest,
        }
        mode = "ab" if offset else "wb"
        try:
            state["fh"] = open(dest, mode)
        except Exception as e:
            self.log(f"无法写入 {dest}: {e}")
            return False
        with self._pull_lock:
            self._pull_wait[req_id] = state
        if not sess.send(make_file_request(offer_id, file_id, offset, req_id)):
            try:
                state["fh"].close()
            except Exception:
                pass
            with self._pull_lock:
                self._pull_wait.pop(req_id, None)
            return False
        # 大文件可能很久；最多等 24h
        while not done.wait(0.5):
            if not self._running or not sess._alive:
                break
            got = state["got"]
            sz = state["size"] or 1
            self.post(
                "progress",
                value=min(100.0, got / sz * 100),
                text=f"下载 {dest.name}  {self._fmt_size(got)}/{self._fmt_size(sz)}",
            )
        try:
            state["fh"].close()
        except Exception:
            pass
        with self._pull_lock:
            self._pull_wait.pop(req_id, None)
        if state["ok"]:
            self.post("progress", value=100, text=f"完成 {dest.name}")
            return True
        self.log(f"拉取中断: {state.get('error')}")
        return False

    def _on_bin_chunk(self, m: dict) -> None:
        req_id = m.get("req_id") or ""
        with self._pull_lock:
            state = self._pull_wait.get(req_id)
        if not state:
            return
        data = m.get("_data") or b""
        try:
            state["fh"].write(data)
            state["got"] = int(m.get("offset") or 0) + len(data)
        except Exception as e:
            state["error"] = str(e)
            state["ok"] = False
            state["event"].set()

    def _on_stream_done(self, m: dict) -> None:
        req_id = m.get("req_id") or ""
        with self._pull_lock:
            state = self._pull_wait.get(req_id)
        if not state:
            return
        state["ok"] = bool(m.get("ok"))
        state["error"] = m.get("reason") or ""
        state["event"].set()

    def pull_pending_now(self) -> None:
        """手动：立即拉取当前 offer 到接收目录并放入剪贴板。"""
        with self._remote_offer_lock:
            offer = self._remote_offer
        if not offer:
            self.log("没有待传文件名单")
            self.post("tray_tip", text="没有待传文件")
            return
        offer_id = offer.get("offer_id") or ""
        threading.Thread(
            target=self._manual_pull_and_set_clipboard,
            args=(offer_id,),
            daemon=True,
        ).start()

    # ---- 主动发送（按钮/拖放）：仍走 offer+立即拉取到对端？改为直接流式推到对端接收目录 ----
    def send_paths(self, paths: list[str]) -> None:
        """拖放/按钮：只发名单，对端需点「拉取待传文件」才下载。"""
        with self._session_lock:
            sess = self._session
        if not sess:
            self.log("未连接，无法发送")
            return
        self._send_file_offer(paths)

    @staticmethod
    def _fmt_size(n: float) -> str:
        n = float(n)
        for u in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or u == "TB":
                return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
            n /= 1024
        return f"{n:.1f}TB"


# ============================== GUI ==============================


class App:
    def __init__(self) -> None:
        _ensure_tk()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("640x540")
        self.root.minsize(520, 440)

        self.ui_queue: queue.Queue = queue.Queue()
        self.engine = LanCopyEngine(self.ui_queue)
        self.clip = ClipboardHelper(self.root)
        self.engine.set_clipboard(self.clip)

        self._tray = None
        self._tray_thread = None
        self._exiting = False
        self._connected = False
        self._tray_state = "idle"
        self._render_hooked = False

        self._build()
        self.root.after(200, self._poll_ui)
        self.root.after(400, self._poll_clip)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.after(100, self._setup_hwnd_and_tray)
        self.root.after(400, self._auto_start)

    def _setup_hwnd_and_tray(self) -> None:
        try:
            hwnd = int(self.root.winfo_id())
            # 提升到顶层窗口
            if sys.platform == "win32":
                import ctypes

                user32 = ctypes.windll.user32
                parent = user32.GetParent(hwnd)
                top = hwnd
                while parent:
                    top = parent
                    parent = user32.GetParent(top)
                hwnd = top
            self.engine.set_hwnd(hwnd)
            self._hook_clipboard_render(hwnd)
        except Exception as e:
            self._append_log(f"窗口句柄/延迟剪贴板钩子: {e}")
        self._init_tray()

    def _hook_clipboard_render(self, hwnd: int) -> None:
        """可选保留延迟渲染钩子；文件主路径已改为自动拉取。

        64 位 HWND/WNDPROC 必须用 pointer-sized ctypes，禁止把 .value 当 int 传入。
        """
        if not _HAS_WIN32 or self._render_hooked:
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WM_RENDERFORMAT = 0x0306
        WM_RENDERALLFORMATS = 0x0307
        GWL_WNDPROC = -4
        HWND = ctypes.c_void_p
        LRESULT = ctypes.c_ssize_t
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            _set = user32.SetWindowLongPtrW
            _get = user32.GetWindowLongPtrW
        else:
            _set = user32.SetWindowLongW
            _get = user32.GetWindowLongW
        _get.argtypes = [HWND, ctypes.c_int]
        _get.restype = ctypes.c_void_p
        _set.argtypes = [HWND, ctypes.c_int, ctypes.c_void_p]
        _set.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallWindowProcW.restype = LRESULT
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        hwnd_p = HWND(hwnd)
        old = _get(hwnd_p, GWL_WNDPROC)
        if not old:
            raise RuntimeError("GetWindowLongPtr 失败")
        app = self
        old_proc = ctypes.c_void_p(old)

        @WNDPROC
        def wndproc(h, msg, wp, lp):
            if msg in (WM_RENDERFORMAT, WM_RENDERALLFORMATS):
                if msg == WM_RENDERALLFORMATS or int(wp) == win32con.CF_HDROP:
                    try:
                        app.engine.render_delayed_hdrop()
                    except Exception:
                        app.root.after(
                            0,
                            lambda: app._append_log(
                                "延迟渲染失败:\n" + traceback.format_exc()
                            ),
                        )
                    return 0
            return user32.CallWindowProcW(old_proc, h, msg, wp, lp)

        self._clip_wndproc = wndproc
        self._clip_old_proc = old_proc
        new_proc = ctypes.cast(wndproc, ctypes.c_void_p)
        _set(hwnd_p, GWL_WNDPROC, new_proc)
        self._render_hooked = True
        self._append_log(f"窗口就绪 hwnd=0x{hwnd:X}")

    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        frm = ttk.Frame(self.root, padding=8)
        frm.pack(fill=tk.BOTH, expand=True)

        ips = list_lan_ips()
        row0 = ttk.Frame(frm)
        row0.pack(fill=tk.X, **pad)
        ttk.Label(row0, text="本机 IP:").pack(side=tk.LEFT)
        self.var_ips = tk.StringVar(value="  |  ".join(ips))
        ttk.Entry(row0, textvariable=self.var_ips, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(row0, text="刷新", width=6, command=self._refresh_ips).pack(side=tk.LEFT)

        row1 = ttk.Frame(frm)
        row1.pack(fill=tk.X, **pad)
        ttk.Label(row1, text="TCP 端口:").pack(side=tk.LEFT)
        self.var_port = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(row1, textvariable=self.var_port, width=8).pack(side=tk.LEFT, padx=4)
        ttk.Label(row1, text=f"发现 UDP {DISCOVER_PORT}").pack(side=tk.LEFT, padx=4)
        self.btn_conn = ttk.Button(row1, text="开始", command=self._connect)
        self.btn_conn.pack(side=tk.RIGHT, padx=2)
        self.btn_disc = ttk.Button(row1, text="停止", command=self._disconnect, state=tk.DISABLED)
        self.btn_disc.pack(side=tk.RIGHT)

        row_disc = ttk.Frame(frm)
        row_disc.pack(fill=tk.X, **pad)
        ttk.Label(row_disc, text="已发现:").pack(side=tk.LEFT)
        self.var_discovered = tk.StringVar(value="（无）")
        ttk.Label(row_disc, textvariable=self.var_discovered, foreground="#06c").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        row2 = ttk.Frame(frm)
        row2.pack(fill=tk.X, **pad)
        ttk.Label(row2, text="手动 IP(可选):").pack(side=tk.LEFT)
        self.var_peer = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.var_peer, width=18).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="一般留空，自动发现即可").pack(side=tk.LEFT)

        row3 = ttk.Frame(frm)
        row3.pack(fill=tk.X, **pad)
        ttk.Label(row3, text="接收目录:").pack(side=tk.LEFT)
        self.var_recv = tk.StringVar(value=str(default_recv_dir()))
        ttk.Entry(row3, textvariable=self.var_recv).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(row3, text="浏览…", width=8, command=self._browse_recv).pack(side=tk.LEFT)
        self.var_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="接收后打开", variable=self.var_open).pack(side=tk.LEFT, padx=4)

        row4 = ttk.Frame(frm)
        row4.pack(fill=tk.X, **pad)
        ttk.Label(row4, text="状态:").pack(side=tk.LEFT)
        self.var_status = tk.StringVar(value="启动中…")
        ttk.Label(row4, textvariable=self.var_status, foreground="#0a0").pack(side=tk.LEFT, padx=4)
        ttk.Button(row4, text="发送文件…", command=self._pick_files).pack(side=tk.RIGHT, padx=2)
        # 醒目「拉取」：缩托盘时用右键同名菜单
        self.btn_pull = tk.Button(
            row4,
            text="⬇ 拉取待传文件",
            command=self._pull_pending,
            bg="#1a7f37",
            fg="white",
            activebackground="#146c2e",
            activeforeground="white",
            relief=tk.RAISED,
            padx=10,
            pady=2,
        )
        self.btn_pull.pack(side=tk.RIGHT, padx=4)
        self.var_pending = tk.StringVar(value="")
        ttk.Label(row4, textvariable=self.var_pending, foreground="#c60").pack(side=tk.RIGHT, padx=6)

        tip = "文字即时同步；文件复制只传名单，对端点「拉取待传文件」（窗口按钮或托盘右键）才下载，避免误传。"
        if _HAS_TRAY:
            tip += " 关闭/最小化进托盘。"
        ttk.Label(frm, text=tip, foreground="#666", wraplength=600).pack(anchor=tk.W, padx=6)

        self.var_prog = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.var_prog).pack(fill=tk.X, padx=6)
        self.prog = ttk.Progressbar(frm, maximum=100, mode="determinate")
        self.prog.pack(fill=tk.X, padx=6, pady=2)

        ttk.Label(frm, text="日志:").pack(anchor=tk.W, padx=6, pady=(6, 0))
        self.log = scrolledtext.ScrolledText(frm, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._enable_dnd(self.log)

        self._append_log(f"{APP_NAME} 已就绪。自动发现同伴；文件需点「拉取待传文件」或托盘右键才下载。")
        self._append_log(f"本机 IP: {' / '.join(ips)}  TCP {DEFAULT_PORT} / UDP {DISCOVER_PORT}")

    def _enable_dnd(self, widget: Any) -> None:
        try:
            import windnd  # type: ignore

            def _drop(files: list) -> None:
                paths = []
                for f in files:
                    if isinstance(f, bytes):
                        try:
                            f = f.decode("gbk")
                        except Exception:
                            f = f.decode("utf-8", errors="ignore")
                    paths.append(str(f))
                if paths:
                    self._append_log(f"拖入 {len(paths)} 项 → 发名单到对端（请对端点「拉取」）")
                    threading.Thread(target=self.engine.send_paths, args=(paths,), daemon=True).start()

            windnd.hook_dropfiles(widget, func=_drop)
            self._append_log("已启用拖放（windnd）")
        except Exception:
            pass

    def _refresh_ips(self) -> None:
        self.var_ips.set("  |  ".join(list_lan_ips()))

    def _browse_recv(self) -> None:
        d = filedialog.askdirectory(initialdir=self.var_recv.get() or str(Path.home()))
        if d:
            self.var_recv.set(d)

    def _pick_files(self) -> None:
        files = filedialog.askopenfilenames(title="选择要发送的文件")
        if files:
            threading.Thread(target=self.engine.send_paths, args=(list(files),), daemon=True).start()

    def _pull_pending(self) -> None:
        self.engine.pull_pending_now()

    def _auto_start(self) -> None:
        try:
            self._connect()
        except Exception:
            self._append_log("自动启动失败，请点「开始」")

    def _connect(self) -> None:
        try:
            port = int(self.var_port.get().strip())
        except ValueError:
            messagebox.showerror(APP_NAME, "端口必须是数字")
            return
        peer = self.var_peer.get().strip()
        recv = Path(self.var_recv.get().strip() or str(default_recv_dir()))
        self.engine.open_after_recv = bool(self.var_open.get())
        self.engine.start(port, peer, recv)
        self.btn_conn.config(state=tk.DISABLED)
        self.btn_disc.config(state=tk.NORMAL)
        self.var_status.set("发现中…")

    def _disconnect(self) -> None:
        self.engine.stop()
        self.btn_conn.config(state=tk.NORMAL)
        self.btn_disc.config(state=tk.DISABLED)
        self.var_status.set("已停止")
        self.var_discovered.set("（无）")
        self._connected = False
        self._sync_tray_status("已停止")

    def _append_log(self, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{ts}] {text}\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _poll_ui(self) -> None:
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                kind = msg.get("kind")
                if kind == "log":
                    self._append_log(msg.get("text", ""))
                elif kind == "status":
                    self.var_status.set(msg.get("text", ""))
                    self._connected = bool(msg.get("connected"))
                    self._sync_tray_status(msg.get("text", ""))
                elif kind == "discovered":
                    peers = msg.get("peers") or []
                    if not peers:
                        self.var_discovered.set("（无）")
                    else:
                        self.var_discovered.set("、".join(f"{p.get('name')}({p.get('ip')})" for p in peers))
                elif kind == "progress":
                    self.prog["value"] = float(msg.get("value") or 0)
                    self.var_prog.set(msg.get("text") or "")
                elif kind == "pending_offer":
                    n = int(msg.get("count") or 0)
                    if n > 0:
                        names = msg.get("names") or ""
                        self.var_pending.set(f"待传 {n} 个" + (f"：{names}" if names else ""))
                        try:
                            self.btn_pull.config(bg="#d97706", activebackground="#b45309")
                        except Exception:
                            pass
                        if not self.root.winfo_viewable():
                            # 缩在托盘时额外提醒一次
                            self._tray_notify(msg.get("names") and f"有待传文件：{names}" or f"有 {n} 个待传文件")
                    else:
                        self.var_pending.set("")
                        try:
                            self.btn_pull.config(bg="#1a7f37", activebackground="#146c2e")
                        except Exception:
                            pass
                elif kind == "tray_tip":
                    self._tray_notify(msg.get("text") or "")
                elif kind == "open_dir":
                    p = msg.get("path") or ""
                    if p and sys.platform == "win32":
                        try:
                            os.startfile(p)  # noqa: S606
                        except Exception:
                            pass
        except queue.Empty:
            pass
        self.root.after(200, self._poll_ui)

    def _poll_clip(self) -> None:
        try:
            if self.engine._running:
                self.engine.poll_clipboard()
        except Exception:
            pass
        self.root.after(500, self._poll_clip)

    # ---- 托盘 ----
    @staticmethod
    def _make_tray_image(color: str):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        colors = {
            "connected": (40, 180, 60, 255),
            "discovering": (230, 160, 20, 255),
            "idle": (140, 140, 140, 255),
        }
        fill = colors.get(color, colors["idle"])
        draw.ellipse((4, 4, 60, 60), fill=fill)
        draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 220))
        return img

    def _init_tray(self) -> None:
        if not _HAS_TRAY:
            self._append_log("未启用托盘（可装 pystray、Pillow）")
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._tray_show, default=True),
                pystray.MenuItem("拉取待传文件", lambda i, it: self.root.after(0, self._pull_pending)),
                pystray.MenuItem("退出", self._tray_exit),
            )
            icon = pystray.Icon("LanCopy", self._make_tray_image("discovering"), APP_NAME, menu)
            self._tray = icon
            threading.Thread(target=icon.run, name="tray", daemon=True).start()
            self._append_log("已启用系统托盘")
        except Exception as e:
            self._append_log(f"托盘失败: {e}")

    def _sync_tray_status(self, text: str) -> None:
        if not self._tray:
            return
        if self._connected or (text or "").startswith("已连接"):
            state = "connected"
        elif (text or "").startswith("已停止"):
            state = "idle"
        else:
            state = "discovering"
        try:
            self._tray.icon = self._make_tray_image(state)
            self._tray.title = f"{APP_NAME} - {text or state}"
        except Exception:
            pass

    def _tray_notify(self, text: str) -> None:
        if self._tray:
            try:
                self._tray.notify(text, APP_NAME)
            except Exception:
                pass

    def _hide_to_tray(self) -> None:
        if self._tray:
            try:
                self.root.withdraw()
            except Exception:
                pass

    def _tray_show(self, icon=None, item=None) -> None:
        self.root.after(0, lambda: (self.root.deiconify(), self.root.state("normal"), self.root.lift()))

    def _tray_exit(self, icon=None, item=None) -> None:
        self._exiting = True
        self.root.after(0, self._quit_app)

    def _quit_app(self) -> None:
        self._exiting = True
        try:
            self.engine.stop()
        except Exception:
            pass
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        try:
            self.root.destroy()
        except Exception:
            pass

    def _on_unmap(self, event: Any) -> None:
        if self._exiting or not self._tray:
            return
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after(50, self._hide_to_tray)

    def _on_close(self) -> None:
        if self._tray and not self._exiting:
            self._hide_to_tray()
            return
        self._quit_app()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
