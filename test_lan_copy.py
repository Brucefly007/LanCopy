#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lan_copy import (
    DISCOVER_TYPE,
    clipboard_text_hash,
    decode_frames,
    encode_frame,
    make_clipboard_text,
    make_discover_hello,
    make_file_chunk,
    make_file_done,
    make_file_meta,
    make_hello,
    make_hello_ok,
    make_ping,
    make_pong,
)


class TestProtocol(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        msg = make_hello("", "host-a")
        out = decode_frames(bytearray(encode_frame(msg)))
        self.assertEqual(out[0]["type"], "hello")

    def test_multiple_frames(self):
        buf = bytearray(encode_frame(make_ping()) + encode_frame(make_pong(1.5)) + encode_frame(make_hello_ok(True)))
        self.assertEqual([m["type"] for m in decode_frames(buf)], ["ping", "pong", "hello_ok"])

    def test_partial_frame(self):
        frame = encode_frame(make_clipboard_text("你好", 1))
        buf = bytearray(frame[:5])
        self.assertEqual(decode_frames(buf), [])
        buf.extend(frame[5:])
        self.assertEqual(decode_frames(buf)[0]["text"], "你好")

    def test_file_chunk_b64(self):
        import base64
        data = b"\x00\x01" + "中".encode()
        out = decode_frames(bytearray(encode_frame(make_file_chunk("a", 0, data))))[0]
        self.assertEqual(base64.b64decode(out["data"]), data)

    def test_file_meta_path_slash(self):
        self.assertEqual(make_file_meta(r"a\b.txt", 1, "i")["name"], "a/b.txt")

    def test_bad_length_raises(self):
        import struct
        with self.assertRaises(ValueError):
            decode_frames(bytearray(struct.pack(">I", 100_000_000) + b"x"))

    def test_unicode_json(self):
        out = decode_frames(bytearray(encode_frame(make_clipboard_text("😀中", 1))))[0]
        self.assertEqual(out["text"], "😀中")


class TestLoopbackSuppress(unittest.TestCase):
    def test_hash_stable(self):
        self.assertEqual(clipboard_text_hash("a"), clipboard_text_hash("a"))
        self.assertNotEqual(clipboard_text_hash("a"), clipboard_text_hash("b"))


class TestDiscover(unittest.TestCase):
    def test_discover_hello_shape(self):
        h = make_discover_hello("abc-id", 3721, "host-a")
        self.assertEqual(h["type"], DISCOVER_TYPE)
        self.assertEqual(h["id"], "abc-id")
        self.assertEqual(h["tcp_port"], 3721)

    def test_id_lex_order(self):
        self.assertTrue("aaa" < "bbb")

    def test_udp_broadcast_roundtrip(self):
        port = 43722
        got = {}

        def recv():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.settimeout(2)
            try:
                data, addr = s.recvfrom(65535)
                got["msg"] = json.loads(data.decode("utf-8"))
                got["addr"] = addr[0]
            finally:
                s.close()

        th = threading.Thread(target=recv)
        th.start()
        time.sleep(0.1)
        payload = json.dumps(make_discover_hello("id-1", 3721, "t")).encode("utf-8")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(payload, ("127.0.0.1", port))
        s.close()
        th.join(3)
        self.assertEqual(got.get("msg", {}).get("type"), DISCOVER_TYPE)
        self.assertEqual(got["msg"]["id"], "id-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
