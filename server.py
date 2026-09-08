# -*- coding: utf-8 -*-
"""
VibeRemote — 手机网页遥控 PC 服务端（P0）
基于 websockets + pynput：局域网 WebSocket 控制 + 静态网页托管
消息协议（JSON）：
  move        {dx, dy}            相对移动鼠标
  click       {btn: left|right}   鼠标点击
  scroll      {dy}                垂直滚动
  key         {key}               单键点按（enter/esc/tab/up/...）
  combo       {keys:[...]}        组合快捷键（ctrl+enter / ctrl+shift+p）
  press       {key, state}        按住/松开（push-to-talk / 长按语音预留）
  type        {text}              文本注入（剪贴板 + Ctrl+V）
"""
import asyncio
import json
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets
from pynput import keyboard, mouse

PORT_HTTP = 8081
PORT_WS = 8766
WEB_DIR = Path(__file__).resolve().parent

# 无控制台运行（pythonw/后台）时，stdout 可能为 None，重定向到日志文件
if sys.stdout is None:
    _log = open(WEB_DIR / "server.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log

try:
    import qrcode
    QR_OK = True
except Exception:
    QR_OK = False


# ---------- 输入注入 ----------
class InputEngine:
    def __init__(self):
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self._burst = 0.008  # 组合键按键间隔

    # 鼠标
    def move(self, dx, dy, sens=1.0):
        """dx,dy 为屏幕相对增量(-1~1)，乘以灵敏度 sens 再乘屏幕尺寸"""
        sw, sh = self._screen_size()
        nx = max(-sw, min(sw, int(dx * sens * sw)))
        ny = max(-sh, min(sh, int(dy * sens * sh)))
        self.ms.move(nx, ny)

    @staticmethod
    def _screen_size():
        try:
            import ctypes
            u = ctypes.windll.user32
            return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        except Exception:
            return 1920, 1080

    def click(self, btn="left"):
        b = mouse.Button.left if btn == "left" else mouse.Button.right
        self.ms.click(b)

    def scroll(self, dy):
        self.ms.scroll(0, int(dy))

    # 键盘
    def _resolve(self, key):
        k = key.lower()
        table = {
            "enter": keyboard.Key.enter, "return": keyboard.Key.enter,
            "esc": keyboard.Key.esc, "escape": keyboard.Key.esc,
            "tab": keyboard.Key.tab, "space": keyboard.Key.space,
            "backspace": keyboard.Key.backspace, "delete": keyboard.Key.delete,
            "up": keyboard.Key.up, "down": keyboard.Key.down,
            "left": keyboard.Key.left, "right": keyboard.Key.right,
            "home": keyboard.Key.home, "end": keyboard.Key.end,
            "page_up": keyboard.Key.page_up, "page_down": keyboard.Key.page_down,
            "f1": keyboard.Key.f1, "f2": keyboard.Key.f2, "f3": keyboard.Key.f3,
            "f4": keyboard.Key.f4, "f5": keyboard.Key.f5, "f6": keyboard.Key.f6,
            "f7": keyboard.Key.f7, "f8": keyboard.Key.f8, "f9": keyboard.Key.f9,
            "f10": keyboard.Key.f10, "f11": keyboard.Key.f11, "f12": keyboard.Key.f12,
            "alt_l": keyboard.Key.alt_l,
            "alt": keyboard.Key.alt_l,
            "alt_r": keyboard.Key.alt_r,
            "ctrl": keyboard.Key.ctrl_l, "ctrl_l": keyboard.Key.ctrl_l, "ctrl_r": keyboard.Key.ctrl_r,
            "shift": keyboard.Key.shift, "shift_l": keyboard.Key.shift_l, "shift_r": keyboard.Key.shift_r,
            "cmd": keyboard.Key.cmd, "win": keyboard.Key.cmd,
            "caps": keyboard.Key.caps_lock,
        }
        if k in table:
            return table[k]
        # 单字符键
        if len(k) == 1 and k.isalnum():
            return k
        return k  # 兜底作为字符串

    def tap(self, key):
        self.kb.tap(self._resolve(key))

    def combos(self, keys):
        ks = [self._resolve(k) for k in keys]
        for k in ks:
            self.kb.press(k)
        time.sleep(self._burst)
        for k in reversed(ks):
            self.kb.release(k)

    def press_key(self, key, state):
        k = self._resolve(key)
        if state == "down":
            self.kb.press(k)
        else:
            self.kb.release(k)

    # 文本注入：剪贴板(ctypes Win32) + Ctrl+V
    def type_text(self, text):
        prev = self._get_clipboard()
        ok = self._set_clipboard(text)
        if not ok:
            print("[!] 剪贴板写入失败，跳过注入")
            return
        time.sleep(0.08)
        self.kb.press(keyboard.Key.ctrl)
        self.kb.tap("v")
        self.kb.release(keyboard.Key.ctrl)
        time.sleep(0.08)
        if prev:
            self._set_clipboard(prev)

    @staticmethod
    def _get_clipboard():
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        # 64 位下句柄/指针按 void* 处理，避免截断
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.OpenClipboard.restype = wintypes.BOOL
        u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        u32.GetClipboardData.argtypes = [wintypes.UINT]
        u32.GetClipboardData.restype = wintypes.HANDLE
        u32.CloseClipboard.restype = wintypes.BOOL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        if not u32.OpenClipboard(0):
            return ""
        try:
            if not u32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
                return ""
            h = u32.GetClipboardData(13)
            if not h:
                return ""
            p = k32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p).replace("\x00", "")
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()

    @staticmethod
    def _set_clipboard(text):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.OpenClipboard.restype = wintypes.BOOL
        u32.EmptyClipboard.restype = wintypes.BOOL
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.CloseClipboard.restype = wintypes.BOOL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        if not u32.OpenClipboard(0):
            return False
        try:
            u32.EmptyClipboard()
            data = (text.rstrip("\x00") + "\x00").encode("utf-16-le")
            h = k32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE|GMEM_ZEROINIT
            if not h:
                return False
            p = k32.GlobalLock(h)
            if not p:
                return False
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            # 所有权转交给系统：SetClipboardData 后不要再手动释放
            if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT
                return False
            return True
        except Exception as e:
            print(f"[!] 剪贴板写入异常: {e}")
            return False
        finally:
            u32.CloseClipboard()


engine = InputEngine()


# ---------- WebSocket 处理 ----------
async def ws_handler(ws):
    peer = ws.remote_address
    print(f"[+] 手机已连接: {peer}")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            handle_msg(msg)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print(f"[-] 手机断开: {peer}")


def handle_msg(msg):
    t = msg.get("type")
    try:
        if t == "move":
            engine.move(msg.get("dx", 0), msg.get("dy", 0), msg.get("sens", 1))
        elif t == "click":
            engine.click(msg.get("btn", "left"))
        elif t == "scroll":
            engine.scroll(msg.get("dy", 0))
        elif t == "key":
            engine.tap(msg.get("key"))
        elif t == "combo":
            engine.combos(msg.get("keys") or [])
        elif t == "press":
            engine.press_key(msg.get("key"), msg.get("state"))
        elif t == "type":
            engine.type_text(msg.get("text", ""))
    except Exception as e:
        print(f"[!] 执行指令失败 {t}: {e}")


# ---------- 静态网页托管 ----------
class WebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            f = WEB_DIR / "index.html"
            if f.exists():
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                # 强制不缓存，开发阶段保证手机每次都拿到最新页面
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)
        elif self.path == "/qrcode.svg":
            self.send_qr()
        else:
            self.send_error(404)

    def send_qr(self):
        if not QR_OK:
            self.send_error(404)
            return
        from qrcode.image.svg import SvgPathImage
        url = f"http://{get_ip()}:{PORT_HTTP}/"
        img = qrcode.make(url, image_factory=SvgPathImage)
        body = img.to_string().decode()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass  # 静默


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    ip = get_ip()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT_HTTP), WebServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print("=" * 56)
    print("  VibeRemote PC 遥控服务 (P0)")
    print(f"  手机访问:  http://{ip}:{PORT_HTTP}/")
    print(f"  WebSocket: ws://{ip}:{PORT_WS}/")
    if QR_OK:
        print(f"  电脑端二维码: http://{ip}:{PORT_HTTP}/qrcode.svg (浏览器打开扫码)")
    print("=" * 56)

    # 尝试自动打开二维码到默认浏览器
    try:
        webbrowser.open(f"http://{ip}:{PORT_HTTP}/qrcode.svg")
    except Exception:
        pass

    async def serve():
        async with websockets.serve(ws_handler, "0.0.0.0", PORT_WS):
            print("WebSocket 服务已启动，等待手机连接...")
            await asyncio.Future()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()