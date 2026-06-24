import os, json, struct, threading, time, queue, sys, re, datetime

SAMPLE_RATE = 16000
import numpy as np
import serial, serial.tools.list_ports
import keyboard, pystray
from PIL import Image, ImageDraw
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socket
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from ai_models import STTHandler, TTSHandler, LLMHandler

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CFG = json.load(open(CONFIG_PATH, encoding="utf-8"))

# ═══════════════════════════════════════════════════════════════
# 默认应用路径映射（用户可在 config.json 的 "apps" 中覆盖）
DEFAULT_APP_MAP = {
    "计算器": "calc.exe",
    "记事本": "notepad.exe",
    "画图": "mspaint.exe",
    "命令提示符": "cmd.exe",
    "powershell": "powershell.exe",
    "文件管理器": "explorer.exe",
    "浏览器": "start chrome",
    "chrome": "start chrome",
    "edge": "start msedge",
    "控制面板": "control",
    "任务管理器": "taskmgr",
    "设置": "start ms-settings:",
    "截图": "snippingtool.exe",
    "录音机": "soundrecorder.exe",
    "时钟": "start ms-clock:",
    "计算器2": "start calculator:",
}

def get_app_map():
    return {**DEFAULT_APP_MAP, **CFG.get("apps", {})}

DIGITS = "零一二三四五六七八九"

def num_to_cn(n):
    if 0 <= n <= 9:
        return DIGITS[n]
    if n <= 99:
        s = ""
        if n >= 10:
            s += (DIGITS[n // 10] if n // 10 > 1 else "") + "十"
        if n % 10:
            s += DIGITS[n % 10]
        return s
    return str(n)

def format_time_cn():
    import datetime
    now = datetime.datetime.now()
    hour = now.hour
    ampm = "上午" if hour < 12 else "下午"
    h12 = hour if hour <= 12 else hour - 12
    weekdays = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"]
    y = "".join(DIGITS[int(c)] for c in str(now.year))
    month = num_to_cn(now.month)
    day = num_to_cn(now.day)
    h = num_to_cn(h12)
    m = num_to_cn(now.minute)
    return f"现在是{y}年{month}月{day}日 {ampm}{h}点{m}分，{weekdays[now.weekday()]}"

def web_search(query, max_results=5):
    try:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get("https://cn.bing.com/search", params={"q": query}, headers=h, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        rs = []
        for item in soup.select(".b_algo")[:max_results]:
            t = item.select_one("h2 a")
            s = item.select_one(".b_caption p")
            if t: rs.append(f"- {t.get_text(strip=True)}\n  {s.get_text(strip=True) if s else ''}")
        return "搜索结果:\n\n" + "\n".join(rs) if rs else f"未找到: {query}"
    except Exception as e: return f"搜索失败: {e}"

# ── 帧协议 (AA 55 [len:4] [cmd] [data]) ────────────────────
FRAME_HEADER = b'\xAA\x55'
CMD_RECORD_DATA = 0x01; CMD_REC_STOP = 0x02; CMD_ACK = 0x03; CMD_PLAY_DONE = 0x05
CMD_SET_VOLUME = 0x0A; CMD_VOLUME_REPLY = 0x0A
CMD_SET_WIFI = 0x0B; CMD_WIFI_SCAN = 0x0C; CMD_WIFI_SSIDS = 0x0D

class FrameParser:
    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._ack_evt = threading.Event()
        self._done_evt = threading.Event()
        self._scan_evt = threading.Event()
        self._scan_ssids = []
        self.on_audio_data = None
        self.on_rec_start = None
        self.on_rec_stop = None

    def build_frame(self, cmd, data=None):
        payload = data or b''
        return FRAME_HEADER + struct.pack('<I', 1 + len(payload)) + bytes([cmd]) + payload

    def feed(self, chunk):
        self._buf.extend(chunk)
        self._parse()

    def _parse(self):
        while True:
            if len(self._buf) < 7: break
            if self._buf[0] != 0xAA: self._buf.pop(0); continue
            if self._buf[1] != 0x55: self._buf.pop(0); continue
            tl = struct.unpack_from('<I', self._buf, 2)[0]
            fs = 6 + tl
            if tl < 1 or tl > 131072: self._buf.pop(0); continue
            if len(self._buf) < fs: break
            cmd = self._buf[6]; dl = tl - 1
            data = bytes(self._buf[7:7+dl]) if dl else b''
            del self._buf[:fs]
            if cmd == CMD_RECORD_DATA and data and self.on_audio_data:
                self.on_audio_data(data)
            elif cmd == CMD_REC_STOP and self.on_rec_stop:
                self.on_rec_stop()
            elif cmd == CMD_PLAY_DONE:
                self._done_evt.set()
            elif cmd == CMD_ACK:
                self._ack_evt.set()
                if self.on_rec_start: self.on_rec_start()
            elif cmd == CMD_WIFI_SSIDS:
                self._scan_ssids = data.decode('utf-8', errors='replace').split('\n') if data else []
                self._scan_evt.set()

# ── 串口传输 ────────────────────────────────────────────────
class SerialTransport(FrameParser):
    def __init__(self, port=None, baud=115200, timeout=5):
        super().__init__()
        self.port = port; self.baud = baud; self.timeout = timeout
        self.ser = None; self.rx_thread = None; self.running = False

    @property
    def connected(self): return self.ser and self.ser.is_open

    def connect(self):
        if not self.port:
            ports = [p.device for p in serial.tools.list_ports.comports()]
            if not ports: raise RuntimeError("未检测到串口")
            self.port = ports[0]
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        self.ser.dtr = True
        self.ser.rts = True
        time.sleep(2)
        self.ser.reset_input_buffer()
        self._buf.clear(); self.running = True
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self.rx_thread.start()

    def disconnect(self):
        self.running = False
        if self.rx_thread: self.rx_thread.join(timeout=2)
        if self.ser and self.ser.is_open: self.ser.close()

    def send(self, cmd, data=None):
        if not self.connected: return False
        frame = self.build_frame(cmd, data)
        try:
            with self._lock: self.ser.write(frame)
            return True
        except: return False

    def _rx_loop(self):
        while self.running:
            try:
                if not self.connected: time.sleep(0.1); continue
                if self.ser.in_waiting == 0: time.sleep(0.001); continue
                data = self.ser.read(self.ser.in_waiting)
                # detect ESP's WiFi IP from serial output
                txt = data.decode('utf-8', 'replace')
                m = __import__('re').search(r'connected! IP:\s*([\d.]+)', txt)
                if m:
                    global CFG
                    CFG['_detected_wifi_host'] = m.group(1)
                    print(f"[AUTO] Detected ESP IP: {m.group(1)}")
                self.feed(data)
            except: time.sleep(0.1)

# ── WiFi 传输 ────────────────────────────────────────────────
class WiFiTransport(FrameParser):
    def __init__(self, host="192.168.4.1", port=11348):
        super().__init__()
        self.host = host; self.port = port
        self.sock = None; self.rx_thread = None; self.running = False

    @property
    def connected(self): return self.sock is not None

    def connect(self):
        hosts = [self.host]
        # try common subnets if first fails
        if self.host.startswith("192.168."):
            parts = self.host.split(".")
            for i in range(1, 255):
                if i != int(parts[3]):
                    hosts.append(f"{parts[0]}.{parts[1]}.{parts[2]}.{i}")
                    if len(hosts) > 5: break
        # fallback: detected IP if set
        detected = CFG.get("_detected_wifi_host")
        if detected and detected not in hosts:
            hosts.insert(0, detected)
        last_err = None
        for h in hosts[:6]:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(2)
                self.sock.connect((h, self.port))
                self.sock.settimeout(1)
                self.host = h  # remember successful host
                self._buf.clear(); self.running = True
                self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
                self.rx_thread.start()
                return
            except Exception as e:
                last_err = e
                self.sock = None
                continue
        raise last_err or ConnectionError("连接超时")

    def disconnect(self):
        self.running = False
        if self.rx_thread: self.rx_thread.join(timeout=2)
        if self.sock:
            try: self.sock.close()
            except: pass
            self.sock = None

    def send(self, cmd, data=None):
        if not self.connected: return False
        frame = self.build_frame(cmd, data)
        try:
            with self._lock: self.sock.sendall(frame)
            return True
        except: return False

    def _rx_loop(self):
        while self.running:
            try:
                if not self.connected: time.sleep(0.1); continue
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.sock.close()
                    self.sock = None
                    time.sleep(0.1)
                    continue
                self.feed(chunk)
            except socket.timeout: continue
            except:
                if self.sock:
                    try: self.sock.close()
                    except: pass
                    self.sock = None
                time.sleep(0.1)

# ── 传输工厂 ────────────────────────────────────────────────
def create_transport(cfg_conn):
    mode = cfg_conn.get("mode", "serial")
    if mode == "wifi":
        w = cfg_conn.get("wifi", {})
        host = w.get("host") or CFG.get("_detected_wifi_host", "192.168.4.1")
        return WiFiTransport(host, w.get("port", 11348))
    s = cfg_conn.get("serial", {})
    return SerialTransport(s.get("port"), s.get("baudrate", 921600), s.get("timeout", 5))

# ── 通用传输接口方法 ────────────────────────────────────────
def transport_send_rec_start(t): t._ack_evt.clear(); return t.send(CMD_RECORD_DATA)
def transport_send_rec_stop(t): return t.send(0x02)
def transport_send_audio(t, data):
    MAX_PAYLOAD = 130000
    for i in range(0, len(data), MAX_PAYLOAD):
        chunk = data[i:i+MAX_PAYLOAD]
        t.send(0x04, chunk)
def transport_send_volume(t, vol):
    vol = max(0, min(100, vol))
    t.send(CMD_SET_VOLUME, bytes([vol]))

def transport_wait_done(t, timeout=30):
    r = t._done_evt.wait(timeout)
    t._done_evt.clear()
    return r

# ── 状态队列 ─────────────────────────────────────────────────
class StatusQueue:
    def __init__(self):
        self.q = queue.Queue(); self.listeners = []
    def add(self, fn): self.listeners.append(fn)
    def put(self, msg):
        self.q.put(msg)
        for fn in self.listeners:
            try: fn(msg)
            except: pass
    def get(self, t=0.1):
        try: return self.q.get(timeout=t)
        except: return None

# ── 热键处理 ─────────────────────────────────────────────────
class HotkeyHandler:
    def __init__(self, combo="ctrl+alt+r", on_press=None, on_release=None):
        self.combo = combo; self._pressed = False
        self._main = None
        for k in combo.lower().replace(" ","").split("+"):
            if k not in ("ctrl","alt","shift","win"): self._main = k; break
        if not self._main: self._main = self.combo.split("+")[-1]
        self.on_press = on_press; self.on_release = on_release
    def start(self):
        keyboard.on_press(self._press); keyboard.on_release(self._release)
    def _is_down(self):
        parts = self.combo.lower().replace(" ","").split("+")
        for k in parts:
            if not keyboard.is_pressed(k): return False
        return True
    def _press(self, e):
        if e.name != self._main: return
        if self._is_down() and not self._pressed:
            self._pressed = True
            if self.on_press: self.on_press()
    def _release(self, e):
        if e.name == self._main and self._pressed:
            self._pressed = False
            if self.on_release: self.on_release()
    def stop(self): keyboard.unhook_all()

# ── Web 配置页面 ────────────────────────────────────────────
CONFIG_HTML = r"""<!DOCTYPE html>
<html lang=zh>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>语音助手配置</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
min-height:100vh;background:linear-gradient(135deg,#0f0c29 0%,#302b63 50%,#24243e 100%);
color:#e0e0e0;padding:24px}
.container{max-width:760px;margin:0 auto}
.header{display:flex;align-items:center;gap:14px;margin-bottom:24px}
.header svg{width:32px;height:32px;flex-shrink:0}
.header h1{font-size:24px;font-weight:700;background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.header .badge{font-size:11px;background:rgba(255,255,255,0.08);padding:3px 10px;border-radius:20px;color:rgba(255,255,255,0.45);margin-left:auto;white-space:nowrap}
.tabs{display:flex;gap:4px;margin-bottom:16px;flex-wrap:wrap}
.tab{padding:9px 20px;border:1px solid rgba(255,255,255,0.08);border-radius:10px;cursor:pointer;background:rgba(255,255,255,0.04);color:rgba(255,255,255,0.5);font-size:13px;font-weight:500;transition:all .25s}
.tab:hover{background:rgba(255,255,255,0.08);color:#fff;border-color:rgba(255,255,255,0.15)}
.tab.active{background:linear-gradient(135deg,rgba(99,102,241,0.25),rgba(139,92,246,0.25));color:#fff;border-color:rgba(139,92,246,0.3)}
.panel{display:none;background:rgba(255,255,255,0.05);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-radius:16px;padding:24px;border:1px solid rgba(255,255,255,0.07);box-shadow:0 8px 32px rgba(0,0,0,0.2)}
.panel.active{display:block;animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.row{display:flex;margin-bottom:14px;align-items:center;gap:12px}
.row label{width:120px;flex-shrink:0;font-size:13px;font-weight:500;color:rgba(255,255,255,0.6)}
.row input,.row select,.row textarea{flex:1;padding:9px 12px;border:1px solid rgba(255,255,255,0.10);border-radius:10px;background:rgba(255,255,255,0.06);color:#e0e0e0;font-size:13px;outline:none;transition:border-color .25s,box-shadow .25s;font-family:inherit}
.row input:focus,.row select:focus,.row textarea:focus{border-color:rgba(99,102,241,0.4);box-shadow:0 0 0 3px rgba(99,102,241,0.12)}
.row textarea{min-height:64px;resize:vertical}
.row .hint{font-size:11px;color:rgba(255,255,255,0.3);margin-left:6px}
.row input[type=range]{padding:0;border:none;background:none;accent-color:#8b5cf6;height:6px}
.row input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);cursor:pointer;border:2px solid rgba(255,255,255,0.15)}
.btn{padding:9px 24px;border:none;border-radius:10px;cursor:pointer;font-size:13px;font-weight:600;transition:all .25s;font-family:inherit}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-primary:hover{box-shadow:0 4px 16px rgba(99,102,241,0.35);transform:translateY(-1px)}
.btn-success{background:linear-gradient(135deg,#10b981,#059669);color:#fff}
.btn-success:hover{box-shadow:0 4px 16px rgba(16,185,129,0.35);transform:translateY(-1px)}
.btn-danger{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
.btn-danger:hover{box-shadow:0 4px 16px rgba(239,68,68,0.35);transform:translateY(-1px)}
.btn-sm{padding:6px 16px;font-size:12px}
.actions{display:flex;gap:10px;margin-top:20px;align-items:center;flex-wrap:wrap}
#status{font-size:12px;padding:7px 14px;border-radius:10px;margin-left:auto;font-weight:500}
#status.ok{background:rgba(16,185,129,0.15);color:#6ee7b7}
#status.err{background:rgba(239,68,68,0.15);color:#fca5a5}
#status.info{background:rgba(99,102,241,0.15);color:#a5b4fc}
.conn_stat{font-size:12px;padding:8px 12px;border-radius:10px;background:rgba(255,255,255,0.04);margin-top:6px;border:1px solid rgba(255,255,255,0.05);color:rgba(255,255,255,0.5)}
.rec_dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.toggle{position:relative;width:42px;height:22px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle .slider{position:absolute;inset:0;background:rgba(255,255,255,0.12);border-radius:11px;cursor:pointer;transition:.25s}
.toggle .slider::before{content:"";position:absolute;width:16px;height:16px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}
.toggle input:checked+.slider{background:linear-gradient(135deg,#6366f1,#8b5cf6)}
.toggle input:checked+.slider::before{transform:translateX(20px)}
select option{background:#1a1a3e}
.panel-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:rgba(255,255,255,0.25);margin-bottom:16px}
input[type=range]::-moz-range-thumb{width:18px;height:18px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);cursor:pointer;border:2px solid rgba(255,255,255,0.15)}
</style>
</head>
<body>
<div class=container>
<div class=header>
<svg viewBox="0 0 24 24" fill="none" stroke="url(#hg)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><defs><linearGradient id="hg" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#60a5fa"/><stop offset="100%" stop-color="#a78bfa"/></linearGradient></defs><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
<h1>语音助手配置</h1>
<span class=badge>v1.0</span>
</div>
<div class=tabs id=tabs>
<div class=tab active data-tab=tab_conn>连接</div>
<div class=tab data-tab=tab_general>通用</div>
<div class=tab data-tab=tab_stt>STT</div>
<div class=tab data-tab=tab_tts>TTS</div>
<div class=tab data-tab=tab_llm>LLM</div>
</div>

<form id=cfg_form>
<div class=panel id=tab_conn active>
<div class=panel-title>设备连接</div>
<div class=row><label>连接方式</label><select name=conn_mode><option value=serial>串口</option><option value=wifi>WiFi</option></select></div>
<div class=row id=serial_row><label>串口号</label><input name=serial_port id=serial_port placeholder=COM3><button type=button class="btn btn-sm btn-primary" onclick=scanPorts()>扫描</button></div>
<div id=ports_list></div>
<div class=row id=serial_baud_row><label>波特率</label><select name=serial_baudrate><option value=115200>115200</option><option value=921600>921600</option></select></div>
<div class=row id=wifi_host_row style=display:none><label>ESP32 IP</label><input name=wifi_host value=192.168.4.1></div>
<div class=row id=wifi_port_row style=display:none><label>端口</label><input name=wifi_port value=11348></div>
<div style="margin:16px 0;border-top:1px solid rgba(255,255,255,0.06);padding-top:14px">
<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:rgba(255,255,255,0.25);margin-bottom:12px">Wi-Fi 配置 (通过串口发送)</div>
<div class=row><label>SSID</label><select id=wifi_ssid style="flex:1"><option value="">-- 扫描或手动输入 --</option></select><input id=wifi_ssid_manual placeholder="或手动输入" style="flex:1;margin-left:4px"></div>
<div class=row><label>密码</label><input id=wifi_pass type=password placeholder="密码 (可选)"></div>
<div class=actions style="display:flex;gap:8px"><button type=button class="btn btn-sm btn-primary" onclick=scanWiFi()>扫描 WiFi</button><button type=button class="btn btn-sm btn-primary" onclick=sendWiFi()>发送到设备</button></div>
</div>
<div class=actions><button type=button class="btn btn-sm btn-success" onclick=reconnectDev()>重连设备</button></div>
<div class=conn_stat id=conn_status>未连接</div>
<div class=conn_stat id=rec_status style=margin-top:4px><span class=rec_dot style=background:#4caf50></span>空闲中</div>
</div>

<div class=panel id=tab_general>
<div class=panel-title>通用设置</div>
<div class=row><label>全局热键</label><input name=hotkey value=ctrl+alt+r></div>
<div class=row><label>打字速度(s)</label><input name=typing_speed type=number step=0.001 min=0 value=0.005></div>
<div class=row><label>启用打字</label><label class=toggle><input name=typing_enabled type=checkbox checked><span class=slider></span></label></div>
<div class=row><label>/ask 前缀</label><input name=ask_prefix value=/ask></div>
<div class=row><label>/search 前缀</label><input name=search_prefix value=/search></div>
<div class=row><label>自动 LLM</label><label class=toggle><input name=auto_llm type=checkbox><span class=slider></span></label><span class=hint>无前缀时也走 LLM</span></div>
<div class=row><label>静音模式</label><label class=toggle><input name=silent_mode type=checkbox><span class=slider></span></label><span class=hint>关闭喇叭播放</span></div>
</div>

<div class=panel id=tab_stt>
<div class=panel-title>语音识别 (STT)</div>
<div class=row><label>模型类型</label><select name=stt_model_type><option value=sense_voice>SenseVoice 中英</option></select></div>
<div class=row><label>模型目录</label><input name=stt_model_dir value=models/sense-voice-zh-en></div>
</div>

<div class=panel id=tab_tts>
<div class=panel-title>语音合成 (TTS)</div>
<div class=row><label>启用 TTS</label><label class=toggle><input name=tts_enabled type=checkbox checked><span class=slider></span></label></div>
<div class=row><label>模型目录</label><input name=tts_model_dir value="models/vits-zh-ll"></div>
<div class=row><label>语速</label><input name=tts_speed type=range min=0.5 max=2.0 step=0.1 value=1.0 style=flex:1><span id=speed_val style=min-width:36px;text-align:center;font-weight:600;color:#a78bfa>1.0</span></div>
<div class=row><label>音量增益</label><input name=tts_volume_gain type=range min=0.5 max=200 step=0.5 value=80 style=flex:1><span id=gain_val style=min-width:48px;text-align:center;font-weight:600;color:#a78bfa>80</span></div>
<div class=row><label>音色/角色</label><select name=tts_speaker_id>
<option value=0>苏映雪 (女声 温柔)</option>
<option value=1>古妮 (女声 活泼)</option>
<option value=2>傅诗雨 (女声 知性)</option>
<option value=3>冰娇 (女声 清冷)</option>
<option value=4>巴总 (男声 浑厚)</option>
</select></div>
<div class=hint style=padding-left:132px;color:rgba(255,255,255,0.3);font-size:11px>共 5 个角色</div>
</div>

<div class=panel id=tab_llm>
<div class=panel-title>大语言模型 (LLM)</div>
<div class=row><label>启用 LLM</label><label class=toggle><input name=llm_enabled type=checkbox checked><span class=slider></span></label></div>
<div class=row><label>模型类型</label><select name=llm_model_type onchange=toggleLLMType()><option value=local>本地模型</option><option value=openai>OpenAI API</option></select></div>
<div id=llm_local_fields>
<div class=row><label>模型</label><input name=llm_model_local value="Qwen/Qwen2.5-0.5B-Instruct"><span class=hint>HuggingFace 模型名</span></div>
</div>
<div id=llm_api_fields style=display:none>
<div class=row><label>API 地址</label><input name=llm_api_base value="https://api.openai.com/v1"></div>
<div class=row><label>API Key</label><input name=llm_api_key type=password></div>
<div class=row><label>模型</label><input name=llm_api_model value="gpt-4o-mini"></div>
</div>
<div class=row><label>温度</label><input name=llm_temperature type=number step=0.1 min=0 max=2 value=0.7></div>
<div class=row><label>最大 Tokens</label><input name=llm_max_tokens type=number min=64 max=32768 value=2048></div>
<div class=row><label>System Prompt</label><textarea name=llm_system_prompt>你是一个智能语音助手，请用中文回答。回答简洁准确，不超过3句话。</textarea></div>
<div class=row><label>历史轮数</label><input name=llm_history_length type=number min=1 max=100 value=10></div>
</div>

</form>

<div class=actions>
<button class="btn btn-primary" onclick=saveConfig()>保存配置</button>
<button class="btn btn-sm btn-primary" onclick=webRefresh()>刷新状态</button>
<div id=status></div>
</div>
</div>

<script>
let CFG={};
async function api(m,p){let r=await fetch('/api/'+m,{method:p||'GET',headers:{'Content-Type':'application/json'},body:p=='POST'?JSON.stringify(CFG):null});return await r.json()}
async function loadConfig(){CFG=await api('config');applyConfig()}
function applyConfig(){
	let m=v=>document.querySelector(`[name=${v}]`);
	m('conn_mode').value=CFG.connection?.mode||'serial';
	m('serial_port').value=CFG.connection?.serial?.port||'';
	m('serial_baudrate').value=CFG.connection?.serial?.baudrate||115200;
	m('wifi_host').value=CFG.connection?.wifi?.host||'192.168.4.1';
	m('wifi_port').value=CFG.connection?.wifi?.port||8080;
	m('hotkey').value=CFG.hotkey||'ctrl+alt+r';
	m('typing_enabled').checked=CFG.typing?.enabled!==false;
	m('typing_speed').value=CFG.typing?.speed||0.005;
	m('ask_prefix').value=CFG.commands?.ask_prefix||'/ask';
	m('search_prefix').value=CFG.commands?.search_prefix||'/search';
	m('auto_llm').checked=CFG.behavior?.auto_llm||false;
	m('silent_mode').checked=CFG.behavior?.silent_mode||false;
	m('stt_model_dir').value=CFG.stt?.model_dir||'models/sense-voice-zh-en';
	m('stt_model_type').value=CFG.stt?.model_type||'sense_voice';
	m('tts_enabled').checked=CFG.tts?.enabled!==false;
	m('tts_model_dir').value=CFG.tts?.model_dir||'models/vits-zh-ll';
	m('tts_speed').value=CFG.tts?.speed||1.0;
	document.getElementById('speed_val').textContent=CFG.tts?.speed||'1.0';
	m('tts_volume_gain').value=CFG.tts?.volume_gain||80;
	document.getElementById('gain_val').textContent=CFG.tts?.volume_gain||'80';
	m('tts_speaker_id').value=String(CFG.tts?.speaker_id||0);
	m('llm_enabled').checked=CFG.llm?.enabled!==false;
	m('llm_model_type').value=CFG.llm?.model_type||'local';
	if(CFG.llm?.model_type==='local'){
		m('llm_model_local').value=CFG.llm?.model||'Qwen/Qwen2.5-0.5B-Instruct';
	}else{
		m('llm_api_base').value=CFG.llm?.api_base||'https://api.openai.com/v1';
		m('llm_api_key').value=CFG.llm?.api_key||'';
		m('llm_api_model').value=CFG.llm?.model||'gpt-4o-mini';
	}
	m('llm_temperature').value=CFG.llm?.temperature||0.7;
	m('llm_max_tokens').value=CFG.llm?.max_tokens||2048;
	m('llm_system_prompt').value=CFG.llm?.system_prompt||'';
	m('llm_history_length').value=CFG.llm?.history_length||10;
	toggleLLMType();
	toggleConnMode(); scanPorts(); refreshConnStatus(); refreshRecStatus();
}
function toggleConnMode(){
	let mode=document.querySelector('[name=conn_mode]').value;
	document.getElementById('serial_row').style.display=mode=='serial'?'':'none';
	document.getElementById('serial_baud_row').style.display=mode=='serial'?'':'none';
	document.getElementById('wifi_host_row').style.display=mode=='wifi'?'':'none';
	document.getElementById('wifi_port_row').style.display=mode=='wifi'?'':'none';
}
function toggleLLMType(){
	let t=document.querySelector('[name=llm_model_type]').value;
	document.getElementById('llm_local_fields').style.display=t=='local'?'':'none';
	document.getElementById('llm_api_fields').style.display=t=='openai'?'':'none';
}
function collectConfig(){
	let m=v=>document.querySelector(`[name=${v}]`),cb=v=>m(v).checked,val=v=>m(v).value;
	let mode=val('conn_mode');
	let llm_type=val('llm_model_type');
	let llm_cfg={enabled:cb('llm_enabled'),model_type:llm_type,
		temperature:parseFloat(val('llm_temperature')),max_tokens:parseInt(val('llm_max_tokens')),
		system_prompt:val('llm_system_prompt'),history_length:parseInt(val('llm_history_length'))};
	if(llm_type==='local'){
		llm_cfg.model=val('llm_model_local');
	}else{
		llm_cfg.api_base=val('llm_api_base');llm_cfg.api_key=val('llm_api_key');llm_cfg.model=val('llm_api_model');
	}
	return {
		hotkey:val('hotkey'),web:{enabled:true,host:'127.0.0.1',port:18099},
		connection:{mode:mode,
			serial:{port:val('serial_port'),baudrate:parseInt(val('serial_baudrate')),timeout:5},
			wifi:{host:val('wifi_host'),port:parseInt(val('wifi_port'))}},
		typing:{enabled:cb('typing_enabled'),speed:parseFloat(val('typing_speed'))},
		commands:{ask_prefix:val('ask_prefix'),search_prefix:val('search_prefix'),clear_prefix:'/clear'},
		behavior:{auto_llm:cb('auto_llm'),silent_mode:cb('silent_mode')},
		stt:{model_type:val('stt_model_type'),model_dir:val('stt_model_dir'),sample_rate:16000},
		tts:{enabled:cb('tts_enabled'),model_type:'vits',model_dir:val('tts_model_dir'),speed:parseFloat(val('tts_speed')),speaker_id:parseInt(val('tts_speaker_id')),volume_gain:parseFloat(val('tts_volume_gain'))},
		llm:llm_cfg
	};
}
function setStatus(msg,type){let s=document.getElementById('status');s.textContent=msg;s.className=type||'info'}
async function saveConfig(){CFG=collectConfig();let r=await api('config','POST');setStatus(r.msg||'已保存','ok')}
async function scanPorts(){let r=await api('ports');let p=document.getElementById('ports_list');p.innerHTML=r.ports?.map(x=>'<span style="display:inline-block;background:rgba(255,255,255,0.06);padding:3px 10px;border-radius:6px;margin:2px 4px;font-size:12px">'+x+'</span>').join('')||'<span style=color:rgba(255,255,255,0.3);font-size:12px>无串口</span>'}
async function refreshConnStatus(){let r=await fetch('/api/conn_status').then(r=>r.json());document.getElementById('conn_status').textContent=r.status||'未知'}
async function refreshRecStatus(){let r=await fetch('/api/rec_status').then(r=>r.json());let e=document.getElementById('rec_status');let dot=r.recording?'<span class=rec_dot style=background:#ef4444></span>':r.processing?'<span class=rec_dot style=background:#f59e0b></span>':'<span class=rec_dot style=background:#10b981></span>';e.innerHTML=dot+(r.text||'空闲中')}
async function webRefresh(){refreshConnStatus();refreshRecStatus()}
setInterval(refreshRecStatus,500)
async function reconnectDev(){let r=await fetch('/api/reconnect',{method:'POST'}).then(r=>r.json());setStatus(r.msg||r.error||'重连完成','ok');refreshConnStatus()}
async function sendWiFi(){let ssid=document.getElementById('wifi_ssid').value;if(!ssid||ssid=='-- 扫描或手动输入 --')ssid=document.getElementById('wifi_ssid_manual').value;if(!ssid){setStatus('请输入或选择 SSID','err');return};let pass=document.getElementById('wifi_pass').value;let r=await fetch('/api/set_wifi',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid:ssid,password:pass})}).then(r=>r.json());setStatus(r.msg||r.error||'发送成功','ok')}
async function scanWiFi(){setStatus('扫描中...','');let r=await fetch('/api/scan_wifi').then(r=>r.json());if(r.ssids){let sel=document.getElementById('wifi_ssid');sel.innerHTML='<option value="">-- 选择 Wi-Fi --</option>'+r.ssids.map(s=>'<option value="'+s.replace(/"/g,'&quot;')+'">'+s.replace(/</g,'&lt;')+'</option>').join('');setStatus('扫描到 '+r.ssids.length+' 个网络','ok')}else{setStatus(r.error||'扫描失败','err')}}
document.querySelector('#tabs').addEventListener('click',e=>{
	let t=e.target.closest('.tab');if(!t)return;
	document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');
	document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
	document.getElementById(t.dataset.tab).classList.add('active');
});
document.querySelector('[name=tts_speed]').addEventListener('input',function(){document.getElementById('speed_val').textContent=this.value});
document.querySelector('[name=tts_volume_gain]').addEventListener('input',function(){document.getElementById('gain_val').textContent=this.value});
document.querySelector('[name=conn_mode]').addEventListener('change',toggleConnMode);
loadConfig();
</script>
</body>
</html>"""

# ── Web 服务器 ──────────────────────────────────────────────
class ConfigHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def _app(self):
        return getattr(threading, '_app', None)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            self._html(CONFIG_HTML)
        elif u.path == '/api/config':
            with open(CONFIG_PATH, encoding='utf-8') as f:
                self._json(json.load(f))
        elif u.path == '/api/ports':
            self._json({"ports": [p.device for p in serial.tools.list_ports.comports()]})
        elif u.path == '/api/conn_status':
            app = self._app()
            if app and app.transport:
                s = f"{type(app.transport).__name__}: {'已连接' if app.transport.connected else '未连接'}"
                self._json({"status": s})
            else:
                self._json({"status": "未初始化"})
        elif u.path == '/api/rec_status':
            app = self._app()
            if app:
                t = app._last_status_msg or ("录音中..." if app.recording else ("处理中..." if app.processing else "空闲中"))
                self._json({"recording": app.recording, "processing": app.processing, "text": t})
            else:
                self._json({"recording": False, "processing": False, "text": "未初始化"})
        elif u.path == '/api/scan_wifi':
            app = self._app()
            if not app or not app.transport or not app.transport.connected:
                self._json({"error": "设备未连接"}, 400)
                return
            t = app.transport
            t._scan_evt.clear()
            t.send(CMD_WIFI_SCAN, b'')
            if t._scan_evt.wait(timeout=10):
                self._json({"ssids": t._scan_ssids})
            else:
                self._json({"ssids": [], "error": "扫描超时"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b'{}'
        app = self._app()

        if u.path == '/api/config':
            try:
                new_cfg = json.loads(body)
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(new_cfg, f, ensure_ascii=False, indent=4)
                global CFG; CFG = new_cfg
                if app: threading.Thread(target=app.reload_config, daemon=True).start()
                self._json({"msg": "配置已保存，正在应用..."})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif u.path == '/api/reconnect':
            if app:
                try:
                    if app.transport and app.transport.connected:
                        app.transport.disconnect()
                    app._init_transport()
                    nm = type(app.transport).__name__
                    self._json({"msg": f"已重连: {nm}"})
                except Exception as e:
                    self._json({"error": str(e)}, 400)
            else:
                self._json({"error": "app not ready"}, 400)
        elif u.path == '/api/set_wifi':
            try:
                data = json.loads(body)
                ssid = data.get("ssid", "").strip()
                password = data.get("password", "").strip()
                if not ssid:
                    self._json({"error": "SSID required"}, 400)
                    return
                payload = bytes([len(ssid)]) + ssid.encode('utf-8') + password.encode('utf-8')
                app.transport.send(CMD_SET_WIFI, payload)
                self._json({"msg": f"WiFi 配置已发送: {ssid}，等待设备重启..."})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

# ── 主应用 ───────────────────────────────────────────────────
class VoiceAssistant:
    def __init__(self):
        self.status = StatusQueue()
        self._last_status_msg = "语音助手就绪"
        self._buf = bytearray()
        self.recording = False; self.processing = False
        self.silent = CFG.get("behavior",{}).get("silent_mode", False)
        self.auto_llm = CFG.get("behavior",{}).get("auto_llm", False)
        self.current_volume = 50
        self.transport = None
        self._voice_reply = True

        self.status.add(lambda m: setattr(self, '_last_status_msg', m))
        self.recording_start_time = 0
        self._last_audio_time = 0

        self.stt = None; self.tts = None; self.llm = None
        threading.Thread(target=self._init_stt, daemon=True).start()
        threading.Thread(target=self._init_tts, daemon=True).start()
        threading.Thread(target=self._init_llm, daemon=True).start()

        self.hk = HotkeyHandler(CFG.get("hotkey","ctrl+alt+r"), self._on_down, self._on_up)
        self._init_transport()
        threading._app = self
        self._start_web()

    def _init_stt(self):
        try:
            self.stt = STTHandler(CFG.get("stt",{}), lambda m: self.status.put(f"STT: {m}"))
        except Exception as e:
            self.status.put(f"STT 错误: {e}")

    def _init_tts(self):
        try:
            self.tts = TTSHandler(CFG.get("tts",{}), lambda m: self.status.put(f"TTS: {m}"))
        except Exception as e:
            self.status.put(f"TTS 错误: {e}")

    def _init_llm(self):
        try:
            self.llm = LLMHandler(CFG.get("llm",{}), lambda m: self.status.put(f"LLM: {m}"))
        except Exception as e:
            self.status.put(f"LLM 错误: {e}")

    def _init_transport(self):
        try:
            if self.transport:
                self.transport.disconnect()
            self.transport = create_transport(CFG.get("connection", {}))
            self.transport.on_audio_data = self._on_audio
            self.transport.on_rec_start = self._on_rec_start
            self.transport.on_rec_stop = self._on_rec_stop
            self.transport.connect()
            self.status.put(f"设备已连接 ({type(self.transport).__name__})")
        except Exception as e:
            self.status.put(f"连接失败: {e}")

    def _start_web(self):
        wc = CFG.get("web", {})
        if not wc.get("enabled", True): return
        host = wc.get("host", "127.0.0.1")
        port = wc.get("port", 18099)
        try:
            srv = ThreadingHTTPServer((host, port), ConfigHTTPHandler)
            srv.allow_reuse_address = True
            srv.daemon_threads = True
            t = threading.Thread(target=srv.serve_forever, daemon=True, name="web-config")
            t.start()
            msg = f"配置页面: http://{host}:{port}"
            self.status.put(msg); print(f"[WEB] {msg}", flush=True)
        except Exception as e:
            err = f"Web 服务启动失败: {e}"
            self.status.put(err); print(f"[WEB ERROR] {err}", flush=True)

    def reload_config(self):
        self.silent = CFG.get("behavior",{}).get("silent_mode", False)
        self.auto_llm = CFG.get("behavior",{}).get("auto_llm", False)
        mode = CFG.get("connection", {}).get("mode")
        cur = type(self.transport).__name__.replace("Transport","").lower() if self.transport else None
        if mode != cur:
            try:
                self._init_transport()
            except Exception as e:
                self.status.put(f"连接失败: {e}")
        threading.Thread(target=self._reload_models, daemon=True).start()
        self.status.put("配置已应用(模型加载中...)")

    def _reload_models(self):
        try:
            if self.stt: self.stt.reload(CFG.get("stt",{}))
            if self.tts: self.tts.reload(CFG.get("tts",{}))
            if self.llm: self.llm.reload(CFG.get("llm",{}))
            self.status.put("模型已就绪")
        except Exception as e:
            self.status.put(f"模型加载失败: {e}")

    def _on_audio(self, data):
        if self.recording:
            self._buf.extend(data)
            self._last_audio_time = time.time()

    def _on_rec_start(self):
        if self.processing or self.recording: return
        self._buf.clear(); self.recording = True
        self.recording_start_time = time.time()
        self._last_audio_time = time.time()
        self.status.put("开始录音...")

    def _on_rec_stop(self):
        if not self.recording: return
        self.recording = False; self.processing = True
        self.recording_start_time = 0
        self.status.put("录音结束，识别中...")
        threading.Thread(target=self._process_audio, daemon=True).start()

    def _on_down(self):
        if self.processing: return
        self._on_rec_start()
        if self.transport and self.transport.connected:
            transport_send_rec_start(self.transport)

    def _on_up(self):
        if not self.recording: return
        self.recording = False; self.processing = True
        self.recording_start_time = 0
        self.status.put("录音结束，识别中...")
        if self.transport and self.transport.connected:
            transport_send_rec_stop(self.transport)
        threading.Thread(target=self._process_audio, daemon=True).start()

    def _handle_volume(self, text):
        m = re.search(r'(\d+)', text)
        vol = None
        if re.search(r'(最大|最高|最大声|开到最大)', text):
            vol = 100
        elif re.search(r'(最小|最低|静音|最?小声|开到最小|零)', text):
            vol = 0
        elif re.search(r'(加大|增大|提高|调高|开大|大声|大一点|大些|大点)', text):
            vol = min(100, self.current_volume + 15)
        elif re.search(r'(减小|减少|降低|调低|开小|小声|小一点|小些|小点|关小)', text):
            vol = max(0, self.current_volume - 15)
        elif re.search(r'音量', text):
            if m:
                vol = int(m.group(1))
        if vol is not None and self.transport and self.transport.connected:
            self.current_volume = vol
            transport_send_volume(self.transport, vol)
            self.status.put(f"音量: {vol}%")
            self._play_tts(f"音量已调到{vol}%")
            return True
        return False

    def _handle_time_query(self, text):
        m = re.search(r'(?:几点了|现在几点|现在.*时间|几月几号|今天星期|今天几号|什么日期|今天日期|现在.*日期)', text)
        if m:
            t = format_time_cn()
            self.status.put(t)
            self._play_tts(t)
            return True
        return False

    def _handle_app_launch(self, text):
        m = re.search(r'(?:打开|启动|运行)\s*(.+)', text)
        if m:
            name = re.sub(r'[，。！？、；：""\'\'【】《》（）.,!?;:\'\"\[\]()\s]+$', '', m.group(1).strip())
            if not name: return False
            app_map = get_app_map()
            exe = app_map.get(name)
            if not exe:
                for k, v in app_map.items():
                    if k in name:
                        exe = v; break
            if not exe:
                import subprocess, shlex
                try:
                    r = subprocess.run(["where", name], capture_output=True, text=True, timeout=3)
                    if r.returncode == 0 and r.stdout.strip():
                        exe = r.stdout.strip().split('\n')[0]
                except: pass
            try:
                import subprocess
                if exe:
                    subprocess.Popen(exe, shell=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.run(["cmd", "/c", "start", "", name],
                        capture_output=True, timeout=10)
                msg = f"已打开{name}"
                self.status.put(msg)
                self._play_tts(msg)
                return True
            except Exception as e:
                self.status.put(f"打开失败: {e}")
                self._play_tts(f"打开{name}失败")
                return True
        return False

    def _handle_app_close(self, text):
        m = re.search(r'(?:关闭|退出|结束)\s*(.+)', text)
        if m:
            name = re.sub(r'[，。！？、；：""\'\'【】《》（）.,!?;:\'\"\[\]()\s]+$', '', m.group(1).strip())
            if not name: return False
            app_map = get_app_map()
            exe = app_map.get(name)
            if not exe:
                for k, v in app_map.items():
                    if k in name:
                        exe = v; break
            import subprocess, os
            proc_names = []
            if exe:
                base = os.path.basename(exe)
                if base.startswith("start "):
                    base = base[6:]
                proc_names.append(base)
                stem, ext = os.path.splitext(base)
                stem = re.sub(r'^(new_|old_|main_)', '', stem)
                alt = stem + ext
                if alt != base:
                    proc_names.append(alt)
            else:
                proc_names.append(name + ".exe")
            for pn in proc_names:
                try:
                    r = subprocess.run(["taskkill", "/f", "/im", pn],
                        capture_output=True, timeout=5)
                    if r.returncode == 0:
                        self.status.put(f"已关闭{name}")
                        self._play_tts(f"已关闭{name}")
                        return True
                except: pass
            self._play_tts(f"关闭{name}失败")
            return True
        return False

    def _handle_music_play(self, text):
        song = None
        is_explicit = False
        m = re.search(r'(?:播放|唱一首|来一首|放一首|搜索播放|搜索并播放)\s*(.+)', text)
        if m: song = m.group(1).strip(); is_explicit = True
        if not song:
            m = re.search(r'(?:搜索|查一下|搜一下|帮我搜|查找)\s*(.+)', text)
            if m: song = m.group(1).strip()
        if not song: return False
        song = re.sub(r'[，,]\s*(?:并且|然后|再|帮我|请).*$', '', song).strip()
        song = re.sub(r'(?:并且|然后|再)\s*.*$', '', song).strip()
        for pfx in ['歌曲', '音乐', '歌']:
            if song.startswith(pfx):
                song = song[len(pfx):]; break
        for sfx in ['这首歌', '这首歌曲', '歌曲', '的歌', '音乐', '的歌曲', '歌']:
            if song.endswith(sfx):
                song = song[:-len(sfx)]; break
        for pfx in ['一首', '个', '一下']:
            if song.startswith(pfx):
                song = song[len(pfx):]; break
        song = song.strip()
        if not song:
            if is_explicit: self._play_tts("请告诉我歌曲名称")
            return is_explicit
        self.status.put(f"搜索歌曲: {song}")
        try:
            h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://music.163.com/"}
            r = requests.get("https://music.163.com/api/search/get/web", params={"csrf_token": "","hlpretag": "","hlposttag": "","s": song,"type": 1,"offset": 0,"total": True,"limit": 5}, headers=h, timeout=15)
            data = r.json()
            songs = data.get("result",{}).get("songs",[])
            if songs:
                sid = songs[0]["id"]
                name = songs[0]["name"]
                artist = songs[0]["artists"][0]["name"]
                url = f"https://music.163.com/song/media/outer/url?id={sid}.mp3"
                self.status.put(f"正在播放: {name} - {artist}")
                import subprocess
                subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._play_tts(f"正在播放{name}")
                return True
        except Exception as e:
            self.status.put(f"音乐搜索异常: {e}")
        if is_explicit:
            self._play_tts(f"未找到{song}")
            return True
        return False

    def _handle_weather_query(self, text):
        if re.search(r'(天气|气温|温度|下雨|下雪|刮风|降温|升温|天冷|天热)', text):
            self.status.put("查询天气中...")
            r = web_search("今天天气")
            short = r[:200] if len(r) > 200 else r
            self.status.put(short)
            self._play_tts(short)
            return True
        return False

    def _get_dialog_text(self, text):
        for wake in ["小助手", "语音对话模式"]:
            if text.startswith(wake):
                return text[len(wake):].strip()
        if re.search(r'(几点了|现在几点|现在.*时间|几月几号|今天星期|今天几号|什么日期|现在.*日期)', text):
            return text
        if re.search(r'(天气|气温|温度|下雨|下雪|刮风|降温|升温|天冷|天热)', text):
            return text
        return None

    def _handle_natural_search(self, text):
        m = re.search(r'(?:搜索|查一下|查找|搜一下|帮我查|帮我搜)\s*(.+)', text)
        if m:
            q = m.group(1).strip()
            q = re.sub(r'[，,]\s*(?:并且|然后|再|帮我|请).*$', '', q).strip()
            q = re.sub(r'(?:并且|然后|再)\s*.*$', '', q).strip()
            q = re.sub(r'播放\s*$', '', q).strip()
            if q:
                self.status.put(f"搜索: {q}")
                r = web_search(q)
                self.status.put(r)
                self._play_tts(r)
                return True
        return False

    def _process_audio(self):
        try:
            dur = len(self._buf) / 96000
            if dur < 0.1:
                self.status.put("录音太短"); return
            raw = bytes(self._buf)
            samples = np.frombuffer(raw, dtype=np.int16)
            mono = ((samples[0::2].astype(np.int32)+samples[1::2].astype(np.int32))//2).astype(np.int16)
            n_out = int(len(mono) * 16000 / SAMPLE_RATE)
            x_old = np.linspace(0, 1, len(mono))
            x_new = np.linspace(0, 1, n_out)
            mono = np.interp(x_new, x_old, mono.astype(np.float32)).astype(np.int16)
            f32 = mono.astype(np.float32) / 32768.0
            text = self.stt.recognize(f32) if self.stt else ""
            if not text: self.status.put("未识别到语音"); return
            self.status.put(f"识别: {text}")
            if CFG.get("typing",{}).get("enabled",True):
                spd = CFG.get("typing",{}).get("speed",0.005)
                for ch in text: keyboard.write(ch); time.sleep(spd)
            dialog_text = self._get_dialog_text(text)
            is_dialog = dialog_text is not None
            self._voice_reply = is_dialog
            if not is_dialog:
                dialog_text = text
            elif not dialog_text:
                self._play_tts("我在呢")
                return
            if self._handle_volume(dialog_text): return
            cmd = CFG.get("commands",{})
            if dialog_text.startswith(cmd.get("clear_prefix","/clear")):
                if self.llm: self.status.put(self.llm.clear())
            elif dialog_text.startswith(cmd.get("search_prefix","/search")):
                q = dialog_text[len(cmd["search_prefix"]):].strip()
                if q:
                    self.status.put(f"搜索: {q}")
                    r = web_search(q); self.status.put(r); self._play_tts(r)
            elif dialog_text.startswith(cmd.get("ask_prefix","/ask")):
                q = dialog_text[len(cmd["ask_prefix"]):].strip()
                if q and self.llm:
                    self.status.put("思考中...")
                    r = self.llm.chat(q); self.status.put(f"回答: {r}"); self._play_tts(r)
            elif self._handle_time_query(dialog_text):
                pass
            elif self._handle_weather_query(dialog_text):
                pass
            elif self._handle_app_launch(dialog_text):
                pass
            elif self._handle_app_close(dialog_text):
                pass
            elif self._handle_music_play(dialog_text):
                pass
            elif self._handle_natural_search(dialog_text):
                pass
            elif self.auto_llm and is_dialog and self.llm and self.llm.enabled:
                now = datetime.datetime.now()
                t_str = now.strftime("%Y-%m-%d %H:%M")
                self.llm.messages[0]["content"] = f"当前时间: {t_str}\n{CFG.get('llm',{}).get('system_prompt','你是一个智能语音助手，请用中文回答。')}"
                self.status.put("思考中...")
                r = self.llm.chat(dialog_text); self.status.put(f"回答: {r}"); self._play_tts(r)
        except Exception as e:
            self.status.put(f"处理失败: {e}")
        finally:
            self.processing = False

    def _play_tts(self, text):
        if not self.tts or not self.tts.enabled or self.silent or not self._voice_reply: return
        self.status.put("合成语音...")
        try:
            if not self.transport or not self.transport.connected: return
            raw = re.split(r'(。|！|？|；|\n)', text)
            parts, buf = [], ""
            for p in raw:
                if p in '。！？；\n':
                    s = (buf + p).strip()
                    if s: parts.append(s)
                    buf = ""
                else:
                    buf = p
            if buf.strip(): parts.append(buf.strip())
            if not parts: parts = [text]
            for idx, chunk in enumerate(parts):
                if idx > 0:
                    self.status.put(f"继续播放({idx+1}/{len(parts)})...")
                pcm, sr = self.tts.synthesize_to_pcm16(chunk)
                if not pcm or len(pcm) <= 44: continue
                mono16 = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                smooth = np.array([0.25, 0.5, 0.25])
                mono16 = np.convolve(mono16, smooth, mode='same')
                gain = CFG.get("tts", {}).get("volume_gain", 1.0)
                if gain != 1.0:
                    peak = np.max(np.abs(mono16))
                    if peak > 0:
                        target = min(32767, peak * gain)
                        mono16 = mono16 * (target / peak)
                        overs = np.abs(mono16) > 30000
                        if np.any(overs):
                            mono16[overs] = np.sign(mono16[overs]) * (
                                30000 + (np.abs(mono16[overs]) - 30000) * 0.2
                            )
                        mono16 = mono16.clip(-32767, 32767)
                if sr != SAMPLE_RATE:
                    from scipy import signal
                    up = SAMPLE_RATE; down = sr
                    mono16 = signal.resample_poly(mono16, up, down).astype(np.float32)
                fade_len = min(int(0.05 * SAMPLE_RATE), len(mono16) // 4)
                if fade_len > 0:
                    fade = np.linspace(0, 1, fade_len)
                    mono16[:fade_len] *= fade
                stereo = np.column_stack((mono16, mono16)).flatten().astype(np.int16)
                pcm = stereo.tobytes()
                self.status.put(f"播放...({idx+1}/{len(parts)})")
                transport_send_audio(self.transport, pcm)
                transport_wait_done(self.transport, 30)
        except Exception as e: self.status.put(f"播放失败: {e}")

    def run(self):
        self.status.put("语音助手就绪")
        self.hk.start()
        try:
            while True:
                time.sleep(0.1)
                if self.recording and self.recording_start_time > 0:
                    now = time.time()
                    if not self.transport or not self.transport.connected:
                        self.status.put("连接断开，自动停止录音")
                        self.recording = False; self.processing = True
                        self.recording_start_time = 0
                        threading.Thread(target=self._process_audio, daemon=True).start()
                    elif now - self.recording_start_time > 30:
                        self.status.put("录音超时，自动停止")
                        self.recording = False; self.processing = True
                        self.recording_start_time = 0
                        if self.transport and self.transport.connected:
                            transport_send_rec_stop(self.transport)
                        threading.Thread(target=self._process_audio, daemon=True).start()
                    elif now - self._last_audio_time > 1.5 and now - self.recording_start_time > 1:
                        self.status.put("静音检测，自动停止")
                        self.recording = False; self.processing = True
                        self.recording_start_time = 0
                        if self.transport and self.transport.connected:
                            transport_send_rec_stop(self.transport)
                        threading.Thread(target=self._process_audio, daemon=True).start()
        except KeyboardInterrupt: self.shutdown()

    def shutdown(self):
        self.hk.stop()
        if self.transport and self.transport.connected: self.transport.disconnect()
        self.status.put("已退出")

# ── 托盘图标 ─────────────────────────────────────────────────
def make_icon(app):
    img = Image.new("RGBA", (64,64), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.ellipse([4,4,60,60], fill="#2196F3")
    d.rounded_rectangle([20,14,44,34], radius=4, fill="white")
    d.rectangle([26,34,38,44], fill="white")
    d.ellipse([22,38,42,50], fill="white")
    wc = CFG.get("web", {})
    url = f"http://{wc.get('host','127.0.0.1')}:{wc.get('port',18099)}"
    menu = pystray.Menu(
        pystray.MenuItem("语音助手", None, enabled=False),
        pystray.MenuItem(f"配置 ({url})", lambda: os.startfile(url)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("重连设备", lambda: (app.transport.disconnect() if app.transport and app.transport.connected else None, app._init_transport())),
        pystray.MenuItem("清空对话", lambda: app.llm.clear() if app.llm else None),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda i: (i.stop(), app.shutdown(), os._exit(0))),
    )
    return pystray.Icon("va", img, "语音助手", menu)

def status_loop(app):
    msgs = []
    while True:
        m = app.status.get(0.5)
        if m:
            msgs.append(m)
            if len(msgs) > 8: msgs.pop(0)
            os.system("cls" if os.name=="nt" else "clear")
            wc = CFG.get("web", {})
            print("="*52)
            print("  语音助手")
            print(f"  配置: http://{wc.get('host','127.0.0.1')}:{wc.get('port',18099)}")
            print("  热键录音 | /ask /search /clear")
            print("="*52)
            for m2 in msgs: print(f"  [{time.strftime('%H:%M:%S')}] {m2}")
            print()

def main():
    app = VoiceAssistant()
    threading.Thread(target=status_loop, args=(app,), daemon=True).start()
    app.run()

if __name__ == "__main__":
    main()
