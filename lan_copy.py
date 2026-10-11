#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网复制 (LanCopy) — 虚拟机 ↔ 宿主机 剪贴板互传（对齐 etcp）
无需 VMware Tools。打开即 UDP 自动发现局域网同伴并连接；一端 Ctrl+C，另一端 Ctrl+V。
文字写入对端剪贴板；文件先落到接收目录再写入 CF_HDROP。
协议：UDP 发现(3722) + TCP 传输(3721)；TCP 帧为 4 字节长度前缀 + UTF-8 JSON。
"""

from __future__ import annotations

import base64
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

# ---------------------------------------------------------------------------
# 可选：pywin32 用于 CF_HDROP（复制文件自动发送）
# ---------------------------------------------------------------------------
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

# tkinter 延迟导入，便于无 GUI 环境跑协议单元测试
tk = None  # type: ignore
ttk = filedialog = messagebox = scrolledtext = None  # type: ignore


def _ensure_tk():
    global tk, ttk, filedialog, messagebox, scrolledtext
    if tk is not None:
        return
    try:
        import tkinter as _tk
        from tkinter import ttk as _ttk, filedialog as _fd, messagebox as _mb, scrolledtext as _st
    except Exception as e:  # pragma: no cover
        print("需要 tkinter:", e)
        sys.exit(1)
    tk = _tk
    ttk = _ttk
    filedialog = _fd
    messagebox = _mb
    scrolledtext = _st

APP_NAME = "局域网复制"
DEFAULT_PORT = 3721
DISCOVER_PORT = 3722
DEFAULT_PASS = ""  # 无口令
CHUNK_SIZE = 1024 * 1024  # 1 MiB
PROTO_VERSION = 1
HEADER = struct.Struct(">I")  # 4-byte big-endian length
DISCOVER_TYPE = "lan_copy_hello"
DISCOVER_INTERVAL = 2.0  # 秒
PEER_STALE_SEC = 8.0


# ============================== 协议 ==============================


def encode_frame(obj: dict) -> bytes:
    """dict -> 长度前缀帧。"""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HEADER.pack(len(payload)) + payload


def decode_frames(buf: bytearray) -> list[dict]:
    """从缓冲中拆出完整 JSON 帧，返回消息列表，buf 原地消费。"""
    msgs: list[dict] = []
    while True:
        if len(buf) < HEADER.size:
            break
        (n,) = HEADER.unpack_from(buf, 0)
        if n < 0 or n > 64 * 1024 * 1024:
            raise ValueError(f"非法帧长度: {n}")
        if len(buf) < HEADER.size + n:
            break
        raw = bytes(buf[HEADER.size : HEADER.size + n])
        del buf[: HEADER.size + n]
        msgs.append(json.loads(raw.decode("utf-8")))
    return msgs


def clipboard_text_hash(text: str) -> str:
    """用于回环抑制的稳定哈希。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_hello(password: str = "", peer_name: str = "") -> dict:
    return {
        "type": "hello",
        "ver": PROTO_VERSION,
        "password": "",  # 兼容旧字段，不再校验
        "name": peer_name or socket.gethostname(),
        "ts": time.time(),
    }


def make_hello_ok(ok: bool, reason: str = "") -> dict:
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


def make_file_meta(rel: str, size: int, file_id: str, sha256: str = "") -> dict:
    return {
        "type": "file_meta",
        "id": file_id,
        "name": rel.replace("\\", "/"),
        "size": int(size),
        "sha256": sha256,
    }


def make_file_chunk(file_id: str, offset: int, data: bytes) -> dict:
    return {
        "type": "file_chunk",
        "id": file_id,
        "offset": int(offset),
        "data": base64.b64encode(data).decode("ascii"),
    }


def make_file_done(file_id: str, ok: bool = True, reason: str = "") -> dict:
    return {"type": "file_done", "id": file_id, "ok": bool(ok), "reason": reason}


def make_ping() -> dict:
    return {"type": "ping", "ts": time.time()}


def make_pong(ts: float = 0) -> dict:
    return {"type": "pong", "ts": ts or time.time()}


# ============================== 网络辅助 ==============================


def list_lan_ips() -> list[str]:
    """本机局域网 IPv4 列表（排除 127.x）。"""
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # 备用：UDP 探测默认路由网卡（短超时，避免无外网时卡住）
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


# ============================== 剪贴板 ==============================


class ClipboardHelper:
    """文本 + Windows CF_HDROP 文件剪贴板（对齐 etcp：对端可 Ctrl+V）。"""

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
                    # 本机 SetClipboardText 只写 CF_TEXT；用 UNICODE 更稳
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
            self.root.update_idletasks()
        except Exception:
            pass

    def set_files(self, paths: list[str]) -> bool:
        """把本地文件路径写入 CF_HDROP，供资源管理器 Ctrl+V 粘贴。"""
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
            blob = bytes(df) + files_bytes

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
            if not ptr:
                raise OSError("GlobalLock 失败")
            ctypes.memmove(ptr, blob, len(blob))
            kernel32.GlobalUnlock(handle)

            effect_handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, 4)
            if effect_handle:
                ep = kernel32.GlobalLock(effect_handle)
                if ep:
                    ctypes.memmove(ep, _struct.pack("<I", 2), 4)  # DROPEFFECT_COPY
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
            try:
                self.set_text(chr(10).join(paths))
                self._last_set_files_sig = sig
            except Exception:
                pass
            return False

    def should_ignore_local(self, text: str) -> bool:
        """本机轮询到文本时，是否为对端刚写入（防回环）。"""
        if time.time() < self._suppress_until:
            if self._last_set_hash and clipboard_text_hash(text) == self._last_set_hash:
                return True
        if self._last_set_hash and clipboard_text_hash(text) == self._last_set_hash:
            return True
        return False

    def should_ignore_files(self, paths: list[str]) -> bool:
        sig = "|".join(paths)
        return bool(self._last_set_files_sig and sig == self._last_set_files_sig)

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def get_file_paths(self) -> list[str]:
        """Windows CF_HDROP；无 pywin32 返回空。"""
        if not _HAS_WIN32:
            return []
        paths: list[str] = []
        for _attempt in range(3):
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
            if not p:
                continue
            try:
                if Path(p).exists():
                    out.append(str(Path(p)))
            except Exception:
                pass
        return out


# ============================== 会话 / 传输 ==============================


class PeerSession:
    """一条已握手的 TCP 连接。"""

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
        t = threading.Thread(target=self._recv_loop, name="recv", daemon=True)
        t.start()

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

    def _recv_loop(self) -> None:
        try:
            while self._alive:
                try:
                    chunk = self.sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                self._buf.extend(chunk)
                try:
                    msgs = decode_frames(self._buf)
                except Exception as e:
                    self.log(f"协议错误: {e}")
                    break
                for m in msgs:
                    try:
                        self.on_message(m)
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


class LanCopyEngine:
    """UDP 发现 + TCP 监听/连接 + 剪贴板轮询 + 文件收发。"""

    def __init__(self, ui_queue: queue.Queue):
        self.ui_queue = ui_queue
        self.port = DEFAULT_PORT
        self.discover_port = DISCOVER_PORT
        self.password = ""  # 无口令
        self.peer_ip = ""  # 手动指定时优先
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
        self._connecting = False  # 正在主动连接中
        self._peers_lock = threading.Lock()
        self._peers: dict[str, dict] = {}  # id -> {ip, tcp_port, name, last_seen}

        self._clip: Optional[ClipboardHelper] = None
        self._last_clip_hash: Optional[str] = None
        self._last_hdrop_sig: Optional[str] = None
        self._recv_files: dict[str, dict] = {}  # id -> meta+fh
        self._recv_batch_paths: list[str] = []
        self._sending = False

    def post(self, kind: str, **kw: Any) -> None:
        self.ui_queue.put({"kind": kind, **kw})

    def log(self, msg: str) -> None:
        self.post("log", text=msg)

    def set_clipboard(self, clip: ClipboardHelper) -> None:
        self._clip = clip

    # ---- 启动 / 停止 ----

    def start(self, port: int, peer_ip: str, recv_dir: Path, discover_port: int = DISCOVER_PORT) -> None:
        self.stop()
        self.port = int(port)
        self.discover_port = int(discover_port)
        self.password = ""
        self.peer_ip = (peer_ip or "").strip()
        self.recv_dir = Path(recv_dir)
        self.recv_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._start_listener()
        self._start_discovery()
        self._want_client = True
        threading.Thread(target=self._client_loop, name="client", daemon=True).start()
        self.log(
            f"已启动：TCP :{self.port}，UDP 发现 :{self.discover_port}，本机 id={self.node_id[:8]}…"
        )
        self.log(f"接收目录: {self.recv_dir}")
        if self.peer_ip:
            self.log(f"手动优先连接 {self.peer_ip}:{self.port}")
        else:
            self.log("正在局域网自动发现同伴…")
        self.post("status", text="发现中…", connected=False)
        self._publish_peers()

    def stop(self) -> None:
        self._running = False
        self._want_client = False
        # 先取出 session 再 close，避免 close→on_close 重入 _session_lock 死锁
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
            self.log(f"TCP 监听失败（端口可能被占用）: {e}")
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
            self.log(f"UDP 发现端口 {self.discover_port} 绑定失败: {e}")
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
        payload = self._discover_payload()
        targets = ["255.255.255.255"]
        # 额外向本机各网段广播（如 192.168.x.255）
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
            # 顺带清理过期同伴
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
            tcp_port = int(msg.get("tcp_port") or self.port)
            name = msg.get("name") or peer_id[:8]
            ip = addr[0]
            with self._peers_lock:
                self._peers[peer_id] = {
                    "id": peer_id,
                    "ip": ip,
                    "tcp_port": tcp_port,
                    "name": name,
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
        """返回 (ip, port, name, peer_id)；仅当本机 id 字典序更小（或手动 IP）时主动连。"""
        if self.peer_ip:
            return (self.peer_ip, self.port, self.peer_ip, "manual")
        with self._peers_lock:
            peers = list(self._peers.values())
        # 选最近活跃且 id > 本机 id 的（本机更小则主动连）
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
            self.log(f"接入连接 {addr[0]}:{addr[1]}")
            self._attach_socket(conn, as_server=True)

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
            ip, port, name, peer_id = target
            self._connecting = True
            try:
                self.post(
                    "status",
                    text=f"连接中 {name or ip}:{port}…",
                    connected=False,
                )
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect((ip, port))
                s.settimeout(None)
                self.log(f"已连上 {name or ''} {ip}:{port}")
                self._attach_socket(s, as_server=False)
                backoff = 1.0
            except Exception as e:
                self.log(f"连接 {ip}:{port} 失败，稍后重试: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 8.0)
            finally:
                self._connecting = False

    def _attach_socket(self, sock: socket.socket, as_server: bool) -> None:
        with self._session_lock:
            if self._session:
                # 已有会话则关掉新的（简易：只保持一条）
                try:
                    sock.close()
                except Exception:
                    pass
                self.log("已有连接，忽略新接入")
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

        # 握手：无口令，直接确认
        sess.send(make_hello(""))

    def _handle_message(self, m: dict, sess: PeerSession) -> None:
        t = m.get("type")
        if t == "hello":
            sess.send(make_hello_ok(True))
            sess.peer_name = m.get("name") or ""
            label = sess.peer_name or "对端"
            self.post("status", text=f"已连接：{label}", connected=True)
            self.log(f"握手成功（对方: {label}）")
        elif t == "hello_ok":
            # 兼容旧版带口令拒绝；新版始终 ok
            if m.get("ok") is False:
                self.log(f"握手被拒: {m.get('reason')}（对方可能是旧版口令程序）")
                sess.close()
                return
            self.post("status", text="已连接：对端", connected=True)
            self.log("对方确认握手")
        elif t == "clipboard_text":
            text = m.get("text") or ""
            if self._clip:
                # 记录哈希，防止马上又发回去
                h = m.get("hash") or clipboard_text_hash(text)
                self._last_clip_hash = h
                self._clip.set_text(text)
            preview = text.replace("\n", " ")[:80]
            self.log(f"← 剪贴板文字 ({len(text)} 字): {preview}")
        elif t == "file_meta":
            self._begin_recv_file(m)
        elif t == "file_chunk":
            self._recv_chunk(m)
        elif t == "file_done":
            self._finish_recv_file(m)
        elif t == "ping":
            sess.send(make_pong(m.get("ts") or 0))
        elif t == "pong":
            pass
        else:
            self.log(f"未知消息类型: {t}")

    # ---- 剪贴板轮询（由 UI 定时调用）----

    def poll_clipboard(self) -> None:
        if not self._clip:
            return
        with self._session_lock:
            sess = self._session
        if not sess:
            return

        # 文件剪贴板（Ctrl+C 复制的文件）
        paths = self._clip.get_file_paths()
        if paths:
            sig = "|".join(paths)
            if self._clip.should_ignore_files(paths) or sig == self._last_hdrop_sig:
                self._last_hdrop_sig = sig
                return
            self._last_hdrop_sig = sig
            self.log(f"检测到剪贴板文件 {len(paths)} 个，开始发送")
            threading.Thread(
                target=self.send_paths, args=(paths,), daemon=True
            ).start()
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
            self.log(f"→ 剪贴板文字 ({len(text)} 字): {preview}")

    # ---- 发文件 ----

    def send_paths(self, paths: list[str]) -> None:
        with self._session_lock:
            sess = self._session
        if not sess:
            self.log("未连接，无法发送文件")
            return
        if self._sending:
            self.log("正在发送中，请稍候")
            return
        self._sending = True
        try:
            items: list[tuple[Path, str]] = []  # abs, rel
            for p in paths:
                path = Path(p)
                if path.is_file():
                    items.append((path, path.name))
                elif path.is_dir():
                    base = path
                    for root, _dirs, files in os.walk(path):
                        for fn in files:
                            fp = Path(root) / fn
                            rel = str(fp.relative_to(base.parent)).replace("\\", "/")
                            items.append((fp, rel))
            if not items:
                self.log("没有可发送的文件")
                return
            total_bytes = sum(fp.stat().st_size for fp, _ in items)
            sent_bytes = 0
            t0 = time.time()
            self.log(f"开始发送 {len(items)} 个文件，共 {self._fmt_size(total_bytes)}")
            for fp, rel in items:
                fid = hashlib.md5(f"{time.time()}:{fp}".encode()).hexdigest()[:12]
                size = fp.stat().st_size
                sha = ""
                if not sess.send(make_file_meta(rel, size, fid, sha)):
                    return
                offset = 0
                with open(fp, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        if not sess.send(make_file_chunk(fid, offset, chunk)):
                            return
                        offset += len(chunk)
                        sent_bytes += len(chunk)
                        elapsed = max(time.time() - t0, 1e-3)
                        speed = sent_bytes / elapsed
                        pct = (sent_bytes / total_bytes * 100) if total_bytes else 100
                        self.post(
                            "progress",
                            value=pct,
                            text=f"{self._fmt_size(sent_bytes)}/{self._fmt_size(total_bytes)}  {self._fmt_size(speed)}/s  {rel}",
                        )
                sess.send(make_file_done(fid, True))
                self.log(f"→ 已发送: {rel} ({self._fmt_size(size)})")
            self.post("progress", value=100, text="发送完成")
            self.log("全部文件发送完成")
        except Exception:
            self.log("发送异常:\n" + traceback.format_exc())
        finally:
            self._sending = False

    # ---- 收文件 ----

    def _begin_recv_file(self, m: dict) -> None:
        fid = m.get("id") or ""
        name = (m.get("name") or "unnamed").replace("\\", "/")
        # 防路径穿越
        name = "/".join(p for p in name.split("/") if p not in ("", ".", ".."))
        size = int(m.get("size") or 0)
        dest = self.recv_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 若存在则加序号
        if dest.exists():
            stem, suf = dest.stem, dest.suffix
            i = 1
            while True:
                cand = dest.with_name(f"{stem}_{i}{suf}")
                if not cand.exists():
                    dest = cand
                    break
                i += 1
        try:
            fh = open(dest, "wb")
        except Exception as e:
            self.log(f"无法写入 {dest}: {e}")
            return
        if not self._recv_files:
            self._recv_batch_paths = []
        self._recv_files[fid] = {
            "path": dest,
            "size": size,
            "got": 0,
            "fh": fh,
            "t0": time.time(),
            "name": name,
        }
        self.log(f"← 接收文件: {name} ({self._fmt_size(size)})")

    def _recv_chunk(self, m: dict) -> None:
        fid = m.get("id") or ""
        info = self._recv_files.get(fid)
        if not info:
            return
        raw = base64.b64decode(m.get("data") or "")
        info["fh"].write(raw)
        info["got"] += len(raw)
        size = info["size"] or 1
        elapsed = max(time.time() - info["t0"], 1e-3)
        speed = info["got"] / elapsed
        pct = min(100.0, info["got"] / size * 100)
        self.post(
            "progress",
            value=pct,
            text=f"{self._fmt_size(info['got'])}/{self._fmt_size(size)}  {self._fmt_size(speed)}/s  {info['name']}",
        )

    def _finish_recv_file(self, m: dict) -> None:
        fid = m.get("id") or ""
        info = self._recv_files.pop(fid, None)
        if not info:
            return
        try:
            info["fh"].close()
        except Exception:
            pass
        path: Path = info["path"]
        self._recv_batch_paths.append(str(path.resolve()))
        self.log(f"← 已保存: {path}")
        self.post("progress", value=100, text=f"已保存 {info['name']}")
        # 本批全部收完 → 写入系统剪贴板（CF_HDROP），对端可 Ctrl+V
        if not self._recv_files:
            paths = list(self._recv_batch_paths)
            self._recv_batch_paths = []
            self._publish_files_to_clipboard(paths)
            if self.open_after_recv and paths:
                self.post("open_dir", path=str(Path(paths[0]).parent))

    def _publish_files_to_clipboard(self, paths: list[str]) -> None:
        if not paths:
            return
        # 若同属一个新建顶层目录，优先把该目录放进剪贴板（更像复制文件夹）
        try:
            recv = self.recv_dir.resolve()
            tops: list[Path] = []
            for p in paths:
                pp = Path(p).resolve()
                try:
                    rel = pp.relative_to(recv)
                    top = recv / rel.parts[0] if rel.parts else pp
                    if top not in tops:
                        tops.append(top)
                except Exception:
                    tops.append(pp)
            clip_paths = [str(t) for t in tops] if len(tops) == 1 else paths
        except Exception:
            clip_paths = paths
        ok = False
        if self._clip:
            ok = bool(self._clip.set_files(clip_paths))
            self._last_hdrop_sig = "|".join(clip_paths)
        if ok:
            self.log(f"← 已放入剪贴板，可直接 Ctrl+V 粘贴（{len(clip_paths)} 项）")
        else:
            self.log(
                f"← 文件已落盘；剪贴板写入失败或无 win32，路径: {'; '.join(clip_paths)}"
            )
            self.post("clip_files", paths=clip_paths)

    @staticmethod
    def _fmt_size(n: float) -> str:
        n = float(n)
        for u in ("B", "KB", "MB", "GB"):
            if n < 1024 or u == "GB":
                return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
            n /= 1024
        return f"{n:.1f}GB"


# ============================== GUI ==============================


class App:
    def __init__(self) -> None:
        _ensure_tk()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("640x520")
        self.root.minsize(520, 420)

        self.ui_queue: queue.Queue = queue.Queue()
        self.engine = LanCopyEngine(self.ui_queue)
        self.clip = ClipboardHelper(self.root)
        self.engine.set_clipboard(self.clip)

        self._tray = None
        self._tray_thread = None
        self._exiting = False
        self._connected = False
        self._tray_state = "idle"  # idle | discovering | connected

        self._build()
        self.root.after(200, self._poll_ui)
        self.root.after(400, self._poll_clip)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Unmap>", self._on_unmap)
        self._init_tray()
        # 打开即自动发现并连接
        self.root.after(300, self._auto_start)

    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        frm = ttk.Frame(self.root, padding=8)
        frm.pack(fill=tk.BOTH, expand=True)

        # 本机 IP
        ips = list_lan_ips()
        row0 = ttk.Frame(frm)
        row0.pack(fill=tk.X, **pad)
        ttk.Label(row0, text="本机 IP:").pack(side=tk.LEFT)
        self.var_ips = tk.StringVar(value="  |  ".join(ips))
        ttk.Entry(row0, textvariable=self.var_ips, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4
        )
        ttk.Button(row0, text="刷新", width=6, command=self._refresh_ips).pack(side=tk.LEFT)

        # 端口 + 启停
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

        # 已发现
        row_disc = ttk.Frame(frm)
        row_disc.pack(fill=tk.X, **pad)
        ttk.Label(row_disc, text="已发现:").pack(side=tk.LEFT)
        self.var_discovered = tk.StringVar(value="（无）")
        ttk.Label(row_disc, textvariable=self.var_discovered, foreground="#06c").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4
        )

        # 高级：手动 IP（可选）
        row2 = ttk.Frame(frm)
        row2.pack(fill=tk.X, **pad)
        ttk.Label(row2, text="手动 IP(可选):").pack(side=tk.LEFT)
        self.var_peer = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.var_peer, width=18).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="一般留空，自动发现即可").pack(side=tk.LEFT)

        # 接收目录
        row3 = ttk.Frame(frm)
        row3.pack(fill=tk.X, **pad)
        ttk.Label(row3, text="接收目录:").pack(side=tk.LEFT)
        self.var_recv = tk.StringVar(value=str(default_recv_dir()))
        ttk.Entry(row3, textvariable=self.var_recv).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4
        )
        ttk.Button(row3, text="浏览…", width=8, command=self._browse_recv).pack(side=tk.LEFT)
        self.var_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="接收后打开", variable=self.var_open).pack(side=tk.LEFT, padx=4)

        # 状态 / 发送
        row4 = ttk.Frame(frm)
        row4.pack(fill=tk.X, **pad)
        ttk.Label(row4, text="状态:").pack(side=tk.LEFT)
        self.var_status = tk.StringVar(value="启动中…")
        ttk.Label(row4, textvariable=self.var_status, foreground="#0a0").pack(side=tk.LEFT, padx=4)
        ttk.Button(row4, text="发送文件…", command=self._pick_files).pack(side=tk.RIGHT)
        tip = "打开后自动发现局域网同伴。连接成功后：一端 Ctrl+C 复制文字/文件，另一端 Ctrl+V。"
        if _HAS_WIN32:
            tip += " 也可拖文件到窗口。"
        if _HAS_TRAY:
            tip += " 关闭/最小化会进入托盘。"
        ttk.Label(frm, text=tip, foreground="#666").pack(anchor=tk.W, padx=6)

        # 进度
        self.var_prog = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.var_prog).pack(fill=tk.X, padx=6)
        self.prog = ttk.Progressbar(frm, maximum=100, mode="determinate")
        self.prog.pack(fill=tk.X, padx=6, pady=2)

        # 日志（支持拖放）
        ttk.Label(frm, text="日志 / 拖放文件到此处:").pack(anchor=tk.W, padx=6, pady=(6, 0))
        self.log = scrolledtext.ScrolledText(frm, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._enable_dnd(self.log)

        self._append_log(
            f"{APP_NAME} 已就绪。打开后自动在局域网发现同伴并连接；两端 Ctrl+C / Ctrl+V 即可。"
        )
        self._append_log(
            f"本机 IP: {' / '.join(ips)}  TCP {DEFAULT_PORT} / UDP 发现 {DISCOVER_PORT}"
        )

    def _enable_dnd(self, widget: Any) -> None:
        """尽量启用拖放；失败则忽略。"""
        # Windows tk 拖放：通过 windnd 或 tkdnd；标准库无内置。
        # 用注册 OLE 的简易方案：尝试 windnd；否则提示用按钮。
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
                    self._append_log(f"拖入 {len(paths)} 项，准备发送…")
                    threading.Thread(
                        target=self.engine.send_paths, args=(paths,), daemon=True
                    ).start()

            windnd.hook_dropfiles(widget, func=_drop)
            self._append_log("已启用拖放（windnd）")
            return
        except Exception:
            pass
        # 回退：在 Windows 上用 tk 的非法路径钩子不可靠；提供右键粘贴路径
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="从剪贴板路径发送", command=self._send_from_clip_paths)
        menu.add_command(label="选择文件发送", command=self._pick_files)

        def _popup(e: Any) -> None:
            menu.tk_popup(e.x_root, e.y_root)

        widget.bind("<Button-3>", _popup)

        # 尝试注册 Windows 拖放消息（ctypes，无第三方）
        if sys.platform == "win32":
            try:
                self._hook_win_drop(widget)
            except Exception as e:
                self._append_log(f"拖放不可用（可用「发送文件」按钮）: {e}")

    def _hook_win_drop(self, widget: Any) -> None:
        """ctypes 子类化窗口过程，处理 WM_DROPFILES（零第三方依赖）。"""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        WM_DROPFILES = 0x0233
        GWL_WNDPROC = -4
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            _set_long = user32.SetWindowLongPtrW
            _get_long = user32.GetWindowLongPtrW
        else:
            _set_long = user32.SetWindowLongW
            _get_long = user32.GetWindowLongW
        _set_long.restype = ctypes.c_void_p
        _get_long.restype = ctypes.c_void_p

        LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        # 使用 toplevel HWND（Tk 控件 id 常为子窗口）
        hwnd = self.root.winfo_id()
        try:
            parent = user32.GetParent(hwnd)
            while parent:
                hwnd = parent
                parent = user32.GetParent(hwnd)
        except Exception:
            pass

        shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
        old_proc = _get_long(wintypes.HWND(hwnd), GWL_WNDPROC)
        if not old_proc:
            raise RuntimeError("GetWindowLong 失败")

        app = self

        def _extract_paths(hdrop: int) -> list[str]:
            n = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            out: list[str] = []
            buf = ctypes.create_unicode_buffer(1024)
            for i in range(n):
                shell32.DragQueryFileW(hdrop, i, buf, 1024)
                if buf.value:
                    out.append(buf.value)
            shell32.DragFinish(hdrop)
            return out

        @WNDPROC
        def _wndproc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    paths = _extract_paths(int(wparam))
                    if paths:
                        app.root.after(
                            0,
                            lambda p=paths: (
                                app._append_log(f"拖入 {len(p)} 项，准备发送…"),
                                threading.Thread(
                                    target=app.engine.send_paths, args=(p,), daemon=True
                                ).start(),
                            ),
                        )
                except Exception as ex:
                    app.root.after(0, lambda: app._append_log(f"拖放解析失败: {ex}"))
                return 0
            return user32.CallWindowProcW(
                ctypes.c_void_p(old_proc), h, msg, wparam, lparam
            )

        # 保持引用，防止被 GC
        self._dnd_wndproc = _wndproc
        self._dnd_old_proc = old_proc
        ret = _set_long(wintypes.HWND(hwnd), GWL_WNDPROC, ctypes.cast(_wndproc, ctypes.c_void_p).value)
        if not ret and ctypes.get_last_error():
            raise RuntimeError("SetWindowLong 失败")
        self._append_log("已启用窗口拖放（Windows）")

    def _send_from_clip_paths(self) -> None:
        text = self.clip.get_text() or ""
        paths = []
        for line in text.replace("\r", "").split("\n"):
            line = line.strip().strip('"')
            if line and Path(line).exists():
                paths.append(line)
        if not paths:
            paths = self.clip.get_file_paths()
        if not paths:
            messagebox.showinfo(APP_NAME, "剪贴板中没有有效文件路径")
            return
        threading.Thread(target=self.engine.send_paths, args=(paths,), daemon=True).start()

    def _refresh_ips(self) -> None:
        ips = list_lan_ips()
        self.var_ips.set("  |  ".join(ips))

    def _browse_recv(self) -> None:
        d = filedialog.askdirectory(initialdir=self.var_recv.get() or str(Path.home()))
        if d:
            self.var_recv.set(d)

    def _pick_files(self) -> None:
        files = filedialog.askopenfilenames(title="选择要发送的文件")
        if files:
            threading.Thread(
                target=self.engine.send_paths, args=(list(files),), daemon=True
            ).start()

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
                        parts = [f"{p.get('name')}({p.get('ip')})" for p in peers]
                        self.var_discovered.set("、".join(parts))
                elif kind == "progress":
                    self.prog["value"] = float(msg.get("value") or 0)
                    self.var_prog.set(msg.get("text") or "")
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

    # ---- 系统托盘 ----

    @staticmethod
    def _make_tray_image(color: str):
        """生成简单圆形托盘图标。"""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        colors = {
            "connected": (40, 180, 60, 255),
            "discovering": (230, 160, 20, 255),
            "idle": (140, 140, 140, 255),
        }
        fill = colors.get(color, colors["idle"])
        draw.ellipse((4, 4, 60, 60), fill=fill)
        # 内圈白点，便于辨认
        draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 220))
        return img

    def _init_tray(self) -> None:
        if not _HAS_TRAY:
            self._append_log("未启用托盘（可安装 pystray、Pillow）")
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._tray_show, default=True),
                pystray.MenuItem("退出", self._tray_exit),
            )
            icon = pystray.Icon(
                "LanCopy",
                self._make_tray_image("discovering"),
                APP_NAME + " - 启动中",
                menu,
            )
            self._tray = icon
            self._tray_thread = threading.Thread(
                target=icon.run, name="tray", daemon=True
            )
            self._tray_thread.start()
            self._append_log("已启用系统托盘（关闭/最小化到托盘；双击恢复）")
        except Exception as e:
            self._tray = None
            self._append_log(f"托盘启动失败: {e}")

    def _sync_tray_status(self, text: str) -> None:
        if not self._tray:
            return
        if self._connected or (text or "").startswith("已连接"):
            state = "connected"
        elif (text or "").startswith("已停止") or text == "未连接":
            state = "idle"
        else:
            state = "discovering"
        self._tray_state = state
        try:
            self._tray.icon = self._make_tray_image(state)
            self._tray.title = f"{APP_NAME} - {text or state}"
        except Exception:
            pass

    def _hide_to_tray(self) -> None:
        if not self._tray:
            return
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _tray_show(self, icon=None, item=None) -> None:
        def _do() -> None:
            try:
                self.root.deiconify()
                self.root.state("normal")
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass

        try:
            self.root.after(0, _do)
        except Exception:
            _do()

    def _tray_exit(self, icon=None, item=None) -> None:
        self._exiting = True

        def _do() -> None:
            self._quit_app()

        try:
            self.root.after(0, _do)
        except Exception:
            self._quit_app()

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
        # 最小化时进托盘
        if self._exiting or not self._tray:
            return
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after(50, self._hide_to_tray)

    def _on_close(self) -> None:
        # 点关闭：有托盘则隐藏，否则退出
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
