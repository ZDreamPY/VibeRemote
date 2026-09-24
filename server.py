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

# 路径：打包(exe)与源码运行两种形态
#  - BASE：持久化数据目录（pin.txt/server.log）。exe 放 exe 所在目录，保证 PIN 重启不变
#  - INDEX_DIR：静态网页目录。exe 时 index.html 在 PyInstaller 解压目录 _MEIPASS
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
    INDEX_DIR = Path(getattr(sys, "_MEIPASS", BASE))
else:
    BASE = Path(__file__).resolve().parent
    INDEX_DIR = BASE

# 配对 PIN：首次启动生成 4 位数字，存 pin.txt，之后保持不变
PIN_FILE = BASE / "pin.txt"

def _load_or_create_pin():
    if PIN_FILE.exists():
        v = PIN_FILE.read_text(encoding="utf-8").strip()
        if v and v.isdigit() and len(v) == 4:
            return v
    import secrets
    pin = f"{secrets.randbelow(10000):04d}"
    try:
        PIN_FILE.write_text(pin, encoding="utf-8")
    except Exception:
        pass
    return pin

PIN = _load_or_create_pin()

# 无控制台运行（pythonw/后台）时，stdout 可能为 None，重定向到日志文件
if sys.stdout is None:
    _log = open(BASE / "server.log", "a", encoding="utf-8", buffering=1)
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


# ---------- WebSocket 处理（配对 PIN 鉴权） ----------
async def ws_handler(ws):
    peer = ws.remote_address
    try:
        # 鉴权握手：8 秒内首条消息必须是 {"type":"auth","pin":xxx}
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
        except asyncio.TimeoutError:
            print(f"[-] 鉴权超时，断开: {peer}")
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            msg = {}
        if msg.get("type") == "auth" and msg.get("pin") == PIN:
            await ws.send(json.dumps({"type": "auth", "ok": True}))
            print(f"[+] 手机配对成功: {peer}")
        else:
            await ws.send(json.dumps({"type": "auth", "ok": False}))
            print(f"[-] 配对 PIN 错误，拒绝连接: {peer}")
            return
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


# ---------- 配对页（大号 PIN + 二维码） ----------
PAIR_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VibeRemote 配对</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#0a0d1f;color:#eef1ff;font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
  .box{max-width:420px;width:92vw;background:rgba(12,16,34,.92);border:1px solid rgba(150,170,255,.22);
    border-radius:24px;padding:32px 28px;text-align:center;box-shadow:0 0 60px rgba(0,229,255,.15)}
  h1{font-size:20px;margin:0 0 6px;background:linear-gradient(90deg,#00e5ff,#ff3df0);
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  p{color:rgba(200,210,255,.6);font-size:13px;margin:0 0 20px}
  .pin{font-size:64px;font-weight:800;letter-spacing:14px;color:#00e5ff;text-shadow:0 0 24px rgba(0,229,255,.55);
    margin:0 0 20px;font-variant-numeric:tabular-nums}
  .qr{width:210px;height:210px;margin:0 auto 18px;background:#fff;border-radius:16px;padding:10px}
  .qr svg{width:100%;height:100%;display:block}
  .url{font-size:14px;color:#b8e6ff;word-break:break-all;background:rgba(255,255,255,.05);
    border:1px solid rgba(150,170,255,.22);border-radius:12px;padding:10px 12px}
  .tip{font-size:12px;color:rgba(200,210,255,.45);margin-top:16px;line-height:1.7}
</style>
</head>
<body>
  <div class="box">
    <h1>VibeRemote 配对</h1>
    <p>用手机浏览器扫码打开，输入配对码即可遥控</p>
    <div class="pin">{pin}</div>
    <div class="qr">{qr}</div>
    <div class="url">{url}</div>
    <div class="tip">配对码保存于 pin.txt，重启不变。<br>如担心泄露，删除 pin.txt 重启服务即可重新生成。</div>
  </div>
</body>
</html>"""

# ---------- 静态网页托管 ----------
class WebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            f = INDEX_DIR / "index.html"
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
            self.send_pair_page()
        else:
            self.send_error(404)

    def send_pair_page(self):
        url = f"http://{get_ip()}:{PORT_HTTP}/"
        qr = ""
        if QR_OK:
            from qrcode.image.svg import SvgPathImage
            img = qrcode.make(url, image_factory=SvgPathImage)
            qr = img.to_string().decode()
        body = PAIR_PAGE.replace("{pin}", PIN).replace("{qr}", qr).replace("{url}", url)
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
    print(f"  配对 PIN:  {PIN}  (保存于 pin.txt)")
    if QR_OK:
        print(f"  电脑端配对页: http://{ip}:{PORT_HTTP}/qrcode.svg (浏览器打开扫码)")
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