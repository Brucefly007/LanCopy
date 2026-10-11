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
    CHUNK_SIZE,
    DISCOVER_TYPE,
    clipboard_text_hash,
    decode_frames,
    encode_frame,
    make_bin_chunk_hdr,
    make_clipboard_text,
    make_discover_hello,
    make_file_offer,
    make_file_request,
    make_file_stream_done,
    make_hello,
    try_decode_one,
)


class TestProtocol(unittest.TestCase):
    def test_json_roundtrip(self):
        msg = make_hello("h")
        buf = bytearray(encode_frame(msg))
        out = try_decode_one(buf)
        self.assertEqual(out["type"], "hello")
        self.assertEqual(len(buf), 0)

    def test_file_offer(self):
        offer = make_file_offer("oid", [{"id": "a", "name": "t.txt", "size": 10}])
        out = try_decode_one(bytearray(encode_frame(offer)))
        self.assertEqual(out["type"], "file_offer")
        self.assertEqual(out["files"][0]["name"], "t.txt")

    def test_bin_chunk_embedded(self):
        data = b"\x00\x01\x02" + b"x" * 100
        hdr = make_bin_chunk_hdr("fid", 0, len(data), "req1")
        buf = bytearray(encode_frame(hdr) + data)
        msgs = decode_frames(buf)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["type"], "bin_chunk")
        self.assertEqual(msgs[0]["_data"], data)
        self.assertEqual(len(buf), 0)

    def test_no_base64_in_chunk_hdr(self):
        hdr = make_bin_chunk_hdr("f", 0, 3, "r")
        self.assertNotIn("data", hdr)
        raw = encode_frame(hdr)
        self.assertNotIn(b"AQID", raw)  # base64 of \x01\x02\x03 would appear if embedded

    def test_chunk_size_large(self):
        self.assertGreaterEqual(CHUNK_SIZE, 4 * 1024 * 1024)

    def test_file_request_resume_offset(self):
        m = make_file_request("o", "f", 123456789, "rid")
        self.assertEqual(m["offset"], 123456789)

    def test_stream_done(self):
        self.assertTrue(make_file_stream_done("f", "r", True)["ok"])

    def test_unicode_text(self):
        out = try_decode_one(bytearray(encode_frame(make_clipboard_text("中文😀", 1))))
        self.assertEqual(out["text"], "中文😀")

    def test_hash(self):
        self.assertEqual(clipboard_text_hash("a"), clipboard_text_hash("a"))


class TestDiscover(unittest.TestCase):
    def test_shape(self):
        h = make_discover_hello("id", 3721, "n")
        self.assertEqual(h["type"], DISCOVER_TYPE)

    def test_udp(self):
        port = 44722
        got = {}

        def recv():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.settimeout(2)
            try:
                data, _ = s.recvfrom(65535)
                got["m"] = json.loads(data.decode("utf-8"))
            finally:
                s.close()

        th = threading.Thread(target=recv)
        th.start()
        time.sleep(0.05)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(json.dumps(make_discover_hello("i", 1)).encode(), ("127.0.0.1", port))
        s.close()
        th.join(3)
        self.assertEqual(got["m"]["type"], DISCOVER_TYPE)


class TestBinaryStreamLoopback(unittest.TestCase):
    def test_peer_session_bin_chunk(self):
        from lan_copy import PeerSession, make_file_request, make_file_stream_done as done_msg

        results = {}
        ready = threading.Event()

        def server():
            ls = socket.socket()
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind(("127.0.0.1", 19821))
            ls.listen(1)
            ready.set()
            c, _ = ls.accept()
            ls.close()
            sess_ref = []

            def on_msg(m):
                if m.get("type") == "file_request" and sess_ref:
                    payload = b"HELLO-RAW-BYTES"
                    sess_ref[0].send_bin_chunk(m["file_id"], 0, payload, m["req_id"])
                    sess_ref[0].send(done_msg(m["file_id"], m["req_id"], True))

            sess = PeerSession(c, on_msg, lambda: None, lambda x: None)
            sess_ref.append(sess)
            for _ in range(40):
                if results.get("done"):
                    break
                time.sleep(0.05)
            sess.close()

        def client():
            ready.wait(2)
            time.sleep(0.05)
            s = socket.create_connection(("127.0.0.1", 19821), timeout=3)

            def on_msg(m):
                if m.get("type") == "bin_chunk":
                    results["data"] = m.get("_data")
                elif m.get("type") == "file_stream_done":
                    results["done"] = m.get("ok")

            sess = PeerSession(s, on_msg, lambda: None, lambda x: None)
            time.sleep(0.05)
            sess.send(make_file_request("o", "f1", 0, "reqx"))
            for _ in range(50):
                if results.get("done"):
                    break
                time.sleep(0.05)
            sess.close()

        th = threading.Thread(target=server)
        th.start()
        client()
        th.join(5)
        self.assertEqual(results.get("data"), b"HELLO-RAW-BYTES")
        self.assertTrue(results.get("done"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
