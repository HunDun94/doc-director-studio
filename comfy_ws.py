# -*- coding: utf-8 -*-
"""
comfy_ws.py · 极简 WebSocket 客户端
--------------------------------------------------
只干一件事：连上 ComfyUI 的 /ws，收执行进度事件（progress / executing / error）。
纯标准库实现，不依赖 websocket-client。

ComfyUI 用 API 提交的任务不会在画布上亮绿框（那是界面自己执行才有的），
所以进度只能从 ws 事件里拿。
"""
import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.parse


class ComfyWS(threading.Thread):
    def __init__(self, base_url, client_id, on_msg, logger=None):
        super().__init__(daemon=True)
        self.base_url = base_url
        self.client_id = client_id
        self.on_msg = on_msg
        self.log = logger or (lambda m: None)
        self._stop = threading.Event()
        self.sock = None
        self.connected = False

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 线程主体
    def run(self):
        while not self._stop.is_set():
            try:
                self._loop()
            except Exception as e:
                self.connected = False
                if not self._stop.is_set():
                    self.log(f"进度通道未连接（{type(e).__name__}），{5}s 后重试")
            if self._stop.is_set():
                break
            time.sleep(5)

    def _loop(self):
        u = urllib.parse.urlparse(self.base_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 8188
        s = socket.create_connection((host, port), timeout=8)
        self.sock = s
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /ws?clientId={self.client_id} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise IOError("握手无响应")
            buf += chunk
            if len(buf) > 65536:
                raise IOError("握手响应过长")
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise IOError("握手被拒绝：" + head.split(b"\r\n")[0].decode("latin1", "ignore"))

        self.connected = True
        s.settimeout(None)
        while not self._stop.is_set():
            frame, buf = self._pop_frame(buf)
            if frame is None:
                chunk = s.recv(65536)
                if not chunk:
                    raise IOError("连接关闭")
                buf += chunk
                continue
            opcode, payload = frame
            if opcode == 0x1:
                try:
                    self.on_msg(json.loads(payload.decode("utf-8")))
                except Exception:
                    pass
            elif opcode == 0x8:
                raise IOError("服务端关闭连接")
            elif opcode == 0x9:
                self._send_pong(payload)

    # ------------------------------------------------------------ 帧解析
    def _pop_frame(self, buf):
        if len(buf) < 2:
            return None, buf
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        pos = 2
        if ln == 126:
            if len(buf) < 4:
                return None, buf
            ln = struct.unpack(">H", buf[2:4])[0]
            pos = 4
        elif ln == 127:
            if len(buf) < 10:
                return None, buf
            ln = struct.unpack(">Q", buf[2:10])[0]
            pos = 10
        mask = None
        if masked:
            if len(buf) < pos + 4:
                return None, buf
            mask = buf[pos:pos + 4]
            pos += 4
        if len(buf) < pos + ln:
            return None, buf
        payload = buf[pos:pos + ln]
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return (opcode, payload), buf[pos + ln:]

    def _send_pong(self, data):
        n = len(data)
        mask = os.urandom(4)
        hdr = bytearray([0x8A])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        try:
            self.sock.sendall(bytes(hdr) + masked)
        except Exception:
            pass
