"""
ESP32C3 语音交互系统 - 上位机软件
功能：
1. 按键录音控制
2. 语音识别（百度ASR + 本地模型）
3. AI对话（智谱AI/阿里通义/百度文心）
4. TTS播放（系统TTS + 本地模型）
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import serial
import serial.tools.list_ports
import struct
import threading
import time
import wave
import tempfile
import os
import json
import subprocess
import queue

# 命令常量 (与固件一致)
CMD_REC_START   = 0x01
CMD_REC_STOP    = 0x02
CMD_PLAY_AUDIO  = 0x04
CMD_PLAY_TONE   = 0x06
CMD_PLAY_MELODY = 0x07
CMD_READ_REGS   = 0x08
CMD_DIAG        = 0x09

SAMPLE_RATE = 16000  # 与固件一致

# 尝试导入语音识别库
try:
    import speech_recognition as sr
    HAS_SR = True
except ImportError:
    HAS_SR = False
    print("Warning: speech_recognition not installed")

# 尝试导入百度ASR
try:
    from aip import AipSpeech
    HAS_BAIDU_ASR = True
except ImportError:
    HAS_BAIDU_ASR = False
    print("Warning: Baidu AipSpeech not installed")

# 尝试导入TTS
try:
    import pyttsx3
    HAS_PYTTSX3 = True
except ImportError:
    HAS_PYTTSX3 = False
    print("Warning: pyttsx3 not installed")

class VoiceAssistant:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ESP32C3 语音交互系统 v1.0")
        self.root.geometry("800x600")
        
        # 配置
        self.config = self.load_config()
        
        # 串口
        self.serial_port = None
        self.serial_buffer = bytearray()
        self.audio_buffer = bytearray()
        self.is_recording = False
        self.recording_start_time = 0
        
        # 音频处理队列
        self.audio_queue = queue.Queue()
        
        # 创建TTS引擎
        self.tts_engine = None
        if HAS_PYTTSX3:
            try:
                self.tts_engine = pyttsx3.init()
                self.tts_engine.setProperty('rate', 150)
                self.tts_engine.setProperty('volume', 0.9)
            except Exception as e:
                print(f"TTS init failed: {e}")
                self.tts_engine = None
        
        # 创建UI
        self.create_ui()
        
        # 启动串口检测
        self.check_serial_ports()
        
        # 启动音频处理线程
        self.running = True
        self.process_thread = threading.Thread(target=self.process_audio_thread, daemon=True)
        self.process_thread.start()
        
        # 定期更新UI
        self.update_ui()
    
    def load_config(self):
        """加载配置"""
        config_file = 'config.json'
        default_config = {
            'serial_port': 'COM4',
            'serial_baud': 921600,
            'baidu_app_id': '',
            'baidu_api_key': '',
            'baidu_secret_key': '',
            'zhipu_api_key': '',
            'tongyi_api_key': '',
            'wenxin_api_key': '',
            'wenxin_secret_key': '',
            'default_model': 'local'
        }
        
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    return {**default_config, **config}
            except:
                pass
        return default_config
    
    def save_config(self):
        """保存配置"""
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)
    
    def create_ui(self):
        """创建UI"""
        # 顶部 - 串口配置
        frame_top = ttk.Frame(self.root)
        frame_top.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(frame_top, text="串口:").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(frame_top, width=10, state='readonly')
        self.port_combo.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(frame_top, text="波特率:").pack(side=tk.LEFT, padx=(20, 5))
        self.baud_combo = ttk.Combobox(frame_top, width=10, values=['115200', '9600'], state='readonly')
        self.baud_combo.pack(side=tk.LEFT)
        self.baud_combo.current(0)
        
        self.connect_btn = ttk.Button(frame_top, text="连接", command=self.toggle_connection)
        self.connect_btn.pack(side=tk.LEFT, padx=20)
        
        self.status_label = ttk.Label(frame_top, text="未连接", foreground='red')
        self.status_label.pack(side=tk.LEFT)
        
        # 状态区域
        frame_status = ttk.LabelFrame(self.root, text="状态", padding=10)
        frame_status.pack(fill=tk.X, padx=10, pady=5)
        
        self.recording_label = ttk.Label(frame_status, text="● 等待录音", foreground='gray')
        self.recording_label.pack(side=tk.LEFT)
        
        self.audio_level = ttk.Label(frame_status, text="音频: 0")
        self.audio_level.pack(side=tk.LEFT, padx=20)
        
        # 录音测试按钮
        ttk.Button(frame_status, text="测试录音(5秒)", 
                  command=self.test_recording).pack(side=tk.RIGHT)
        
        ttk.Button(frame_status, text="读寄存器",
                  command=self.read_regs).pack(side=tk.RIGHT, padx=5)
        
        ttk.Button(frame_status, text="音频诊断",
                  command=self.diag_audio).pack(side=tk.RIGHT, padx=5)
        
        # 日志区域
        frame_log = ttk.LabelFrame(self.root, text="日志", padding=10)
        frame_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.log_text = scrolledtext.ScrolledText(frame_log, height=15, font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 底部 - API配置
        frame_bottom = ttk.LabelFrame(self.root, text="API配置", padding=10)
        frame_bottom.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(frame_bottom, text="模型:").pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value=self.config.get('default_model', 'local'))
        models = ['local', 'zhipu', 'tongyi', 'wenxin', 'baidu_asr']
        self.model_combo = ttk.Combobox(frame_bottom, textvariable=self.model_var, 
                                       values=models, width=15, state='readonly')
        self.model_combo.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(frame_bottom, text="配置API", command=self.show_config_dialog).pack(side=tk.LEFT, padx=20)
        ttk.Button(frame_bottom, text="保存配置", command=self.save_settings).pack(side=tk.LEFT)
    
    def check_serial_ports(self):
        """检测串口"""
        ports = list(serial.tools.list_ports.comports())
        port_list = [p.device for p in ports]
        self.port_combo['values'] = port_list
        if port_list:
            if self.config['serial_port'] in port_list:
                self.port_combo.set(self.config['serial_port'])
            else:
                self.port_combo.current(0)
    
    def toggle_connection(self):
        """切换连接状态"""
        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        else:
            self.connect()
    
    def connect(self):
        """连接串口"""
        port = self.port_combo.get()
        baud = int(self.baud_combo.get())
        
        try:
            self.serial_port = serial.Serial(port, baud, timeout=0.1)
            self.status_label.config(text="已连接", foreground='green')
            self.connect_btn.config(text="断开")
            self.log(f"✓ 串口 {port} 已连接 ({baud} bps)")
            
            # 启动接收线程
            self.receive_thread = threading.Thread(target=self.receive_serial, daemon=True)
            self.receive_thread.start()
            
        except Exception as e:
            messagebox.showerror("错误", f"连接失败: {e}")
    
    def disconnect(self):
        """断开连接"""
        if self.serial_port:
            self.serial_port.close()
            self.serial_port = None
        self.status_label.config(text="未连接", foreground='red')
        self.connect_btn.config(text="连接")
        self.log("✗ 串口已断开")
    
    def receive_serial(self):
        """接收串口数据"""
        while self.serial_port and self.serial_port.is_open:
            try:
                if self.serial_port.in_waiting > 0:
                    data = self.serial_port.read(self.serial_port.in_waiting)
                    self.process_serial_data(data)
                time.sleep(0.01)
            except Exception as e:
                self.log(f"接收错误: {e}")
                break
    
    def process_serial_data(self, data):
        """处理串口数据"""
        self.serial_buffer.extend(data)
        
        # 解析帧
        while len(self.serial_buffer) >= 6:
            # 找帧头
            header_pos = -1
            for i in range(len(self.serial_buffer) - 1):
                if self.serial_buffer[i] == 0xAA and self.serial_buffer[i+1] == 0x55:
                    header_pos = i
                    break
            
            if header_pos == -1:
                if len(self.serial_buffer) > 100:
                    self.serial_buffer = self.serial_buffer[-100:]
                break
            
            if header_pos > 0:
                self.serial_buffer = self.serial_buffer[header_pos:]
            
            if len(self.serial_buffer) < 6:
                break
            
            length_data = bytes(self.serial_buffer[2:6])
            data_length = struct.unpack('<I', length_data)[0]
            total_length = 6 + data_length
            
            if len(self.serial_buffer) < total_length:
                break
            
            frame_data = bytes(self.serial_buffer[6:total_length])
            self.serial_buffer = self.serial_buffer[total_length:]
            
            if len(frame_data) > 0:
                cmd = frame_data[0]
                
                if cmd == 0x01:  # 音频数据
                    audio = frame_data[1:]
                    self.audio_buffer.extend(audio)
                    self.update_audio_level(len(audio))
                    
                    if not self.is_recording:
                        self.is_recording = True
                        self.recording_start_time = time.time()
                        self.root.after(0, lambda: self.recording_label.config(
                            text="● 录音中...", foreground='red'))
                        self.log("🎤 开始录音...")
                
                elif cmd == 0x02:  # 录音停止
                    if self.is_recording:
                        self.is_recording = False
                        duration = time.time() - self.recording_start_time
                        self.root.after(0, lambda: self.recording_label.config(
                            text="● 等待录音", foreground='gray'))
                        self.log(f"✓ 录音结束 ({duration:.1f}秒, {len(self.audio_buffer)}字节)")
                        
                        # 将音频数据放入队列处理
                        if len(self.audio_buffer) > 0:
                            self.audio_queue.put(bytes(self.audio_buffer))
                            self.audio_buffer = bytearray()
                
                elif cmd == 0x08 and len(frame_data) >= 3:  # 寄存器数据
                    reg = frame_data[1]
                    val = frame_data[2]
                    self.log(f"  R{reg:02X} = 0x{val:02X} ({val:3d})")
                
                elif cmd == 0x09 and len(frame_data) >= 13:  # 诊断数据
                    import struct
                    total_s, non_zero, ff_count = struct.unpack_from('<III', frame_data, 1)
                    self.log(f"📊 音频诊断: 总样本={total_s}, 非零={non_zero}, 0xFF={ff_count}")
                    if total_s > 0:
                        quality = non_zero * 100 // total_s
                        self.log(f"   质量: {quality}% 有效样本")
                
                elif len(frame_data) > 1:  # 文本消息
                    try:
                        msg = frame_data[1:].decode('utf-8', errors='ignore').strip()
                        if msg:
                            self.log(f"设备: {msg}")
                    except:
                        pass
    
    def update_audio_level(self, size):
        """更新音频电平显示"""
        level = min(100, size // 20)
        self.root.after(0, lambda: self.audio_level.config(text=f"音频: {level}"))
    
    def process_audio_thread(self):
        """音频处理线程"""
        while self.running:
            try:
                audio_data = self.audio_queue.get(timeout=1)
                self.process_audio(audio_data)
            except queue.Empty:
                continue
            except Exception as e:
                self.log(f"处理音频错误: {e}")
    
    def process_audio(self, audio_data):
        """处理音频数据"""
        self.log("=" * 50)
        self.log("📥 进入 process_audio 函数")
        self.log(f"📊 音频数据大小: {len(audio_data)} 字节")
        
        # 检查音频数据有效性
        if len(audio_data) < 100:
            self.log("❌ 音频数据太小，可能无效")
            return
        
        non_zero_count = 0
        all_ff_count = 0
        for i in range(min(100, len(audio_data))):
            if audio_data[i] not in (0, 0xFF):
                non_zero_count += 1
            if audio_data[i] == 0xFF:
                all_ff_count += 1
        
        self.log(f"📊 前100字节: 有效={non_zero_count}, 0xFF={all_ff_count}")
        
        if non_zero_count < 10:
            self.log("⚠️ 警告：音频数据可能无效")
            return
        
        # 保存WAV文件
        temp_file = tempfile.mktemp(suffix='.wav')
        self.save_wav(temp_file, audio_data)
        self.log(f"💾 已保存音频文件: {temp_file}")
        
        # 语音识别
        text = self.recognize_speech(temp_file)
        
        if text:
            self.log(f"✅ 识别结果: {text}")
            self.show_recognized_text(text)
            
            # 处理AI对话
            model = self.model_var.get()
            if model != 'local':
                threading.Thread(target=lambda t=text: self.process_ai_chat(t), daemon=True).start()
            else:
                self.log("📝 本地模式：使用模拟回复")
                self.speak_text("本地模式，暂不支持对话功能")
        else:
            self.log("❌ 语音识别失败，请重试")
            self.speak_text("语音识别失败，请重试")
        
        # 清理临时文件
        try:
            os.remove(temp_file)
        except:
            pass
        
        self.log("=" * 50)
    
    def save_wav(self, file_path, audio_data):
        """保存为WAV文件"""
        with wave.open(file_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data)
    
    def recognize_speech(self, audio_file):
        """语音识别"""
        model = self.model_var.get()
        
        # 百度ASR
        if model == 'baidu_asr' and HAS_BAIDU_ASR:
            return self.recognize_baidu(audio_file)
        
        # 本地识别（使用speech_recognition库）
        if HAS_SR:
            return self.recognize_local(audio_file)
        
        # 模拟识别
        return self.simulate_recognition()
    
    def recognize_baidu(self, audio_file):
        """百度ASR识别"""
        app_id = self.config.get('baidu_app_id', '')
        api_key = self.config.get('baidu_api_key', '')
        secret_key = self.config.get('baidu_secret_key', '')
        
        if not all([app_id, api_key, secret_key]):
            self.log("⚠️ 百度API未配置，使用本地识别")
            return self.recognize_local(audio_file)
        
        try:
            client = AipSpeech(app_id, api_key, secret_key)
            with open(audio_file, 'rb') as f:
                result = client.asr(f.read(), 'wav', 16000, {'dev_pid': 1537})
            
            if result and result.get('err_no') == 0:
                return ''.join(result.get('result', []))
            else:
                self.log(f"百度识别失败: {result}")
        except Exception as e:
            self.log(f"百度识别异常: {e}")
        
        return self.recognize_local(audio_file)
    
    def recognize_local(self, audio_file):
        """本地语音识别"""
        try:
            r = sr.Recognizer()
            with sr.AudioFile(audio_file) as source:
                audio = r.record(source)
            
            # 使用Google语音识别（需要网络）
            text = r.recognize_google(audio, language='zh-CN')
            return text
        except sr.UnknownValueError:
            self.log("⚠️ 无法识别音频内容")
        except sr.RequestError as e:
            self.log(f"⚠️ 识别服务错误: {e}")
        except Exception as e:
            self.log(f"⚠️ 本地识别异常: {e}")
        
        return None
    
    def simulate_recognition(self):
        """模拟识别（用于测试）"""
        import random
        phrases = [
            "你好",
            "今天天气怎么样",
            "现在几点",
            "播放音乐",
            "打开浏览器"
        ]
        result = random.choice(phrases)
        self.log(f"📝 模拟识别: {result}")
        return result
    
    def process_ai_chat(self, text):
        """处理AI对话"""
        model = self.model_var.get()
        
        if model == 'zhipu':
            reply = self.chat_zhipu(text)
        elif model == 'tongyi':
            reply = self.chat_tongyi(text)
        elif model == 'wenxin':
            reply = self.chat_wenxin(text)
        else:
            reply = "本地模式暂不支持对话"
        
        if reply:
            self.log(f"🤖 AI回复: {reply}")
            self.speak_text(reply)
    
    def chat_zhipu(self, text):
        """智谱AI对话"""
        api_key = self.config.get('zhipu_api_key', '')
        if not api_key:
            return "智谱API未配置"
        
        try:
            import requests
            response = requests.post(
                'https://open.bigmodel.cn/api/paas/v4/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'},
                json={
                    'model': 'glm-4-flash',
                    'messages': [{'role': 'user', 'content': text}]
                },
                timeout=30
            )
            result = response.json()
            return result.get('choices', [{}])[0].get('message', {}).get('content', '')
        except Exception as e:
            self.log(f"智谱API错误: {e}")
            return None
    
    def chat_tongyi(self, text):
        """阿里通义对话"""
        api_key = self.config.get('tongyi_api_key', '')
        if not api_key:
            return "通义API未配置"
        
        try:
            import requests
            response = requests.post(
                'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={
                    'model': 'qwen-turbo',
                    'messages': [{'role': 'user', 'content': text}]
                },
                timeout=30
            )
            result = response.json()
            return result.get('choices', [{}])[0].get('message', {}).get('content', '')
        except Exception as e:
            self.log(f"通义API错误: {e}")
            return None
    
    def chat_wenxin(self, text):
        """百度文心对话"""
        api_key = self.config.get('wenxin_api_key', '')
        secret_key = self.config.get('wenxin_secret_key', '')
        if not all([api_key, secret_key]):
            return "文心API未配置"
        
        try:
            # 获取access_token
            token_url = f'https://aip.baidubce.com/oauth/2.0/token?grant_type=client_credentials&client_id={api_key}&client_secret={secret_key}'
            token_resp = requests.get(token_url)
            access_token = token_resp.json().get('access_token')
            
            if not access_token:
                return "获取token失败"
            
            # 对话
            chat_url = f'https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/completions?access_token={access_token}'
            response = requests.post(chat_url, json={
                'messages': [{'role': 'user', 'content': text}]
            })
            result = response.json()
            return result.get('result', '')
        except Exception as e:
            self.log(f"文心API错误: {e}")
            return None
    
    def speak_text(self, text):
        """TTS播放"""
        self.log(f"🔊 播放: {text}")
        
        if self.tts_engine:
            try:
                self.tts_engine.say(text)
                self.tts_engine.runAndWait()
            except Exception as e:
                self.log(f"TTS播放错误: {e}")
        else:
            # 使用系统命令
            self.speak_system(text)
    
    def speak_system(self, text):
        """使用系统TTS"""
        try:
            if os.name == 'nt':  # Windows
                import pythoncom
                import win32com.client
                pythoncom.CoInitialize()
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                speaker.Speak(text)
                pythoncom.CoUninitialize()
        except Exception as e:
            self.log(f"系统TTS错误: {e}")
    
    def show_recognized_text(self, text):
        """显示识别文本"""
        self.log(f"📝 识别文本: {text}")
    
    def send_cmd(self, cmd, data=None):
        """发送命令帧到设备"""
        if not self.serial_port or not self.serial_port.is_open:
            self.log("未连接串口")
            return False
        payload = data or b''
        total = 1 + len(payload)
        frame = bytes([0xAA, 0x55]) + struct.pack('<I', total) + bytes([cmd]) + payload
        try:
            self.serial_port.write(frame)
            return True
        except Exception as e:
            self.log(f"发送失败: {e}")
            return False
    
    def read_regs(self):
        """读取ES8311所有寄存器"""
        self.log("📖 读取ES8311寄存器...")
        self.send_cmd(CMD_READ_REGS)
    
    def diag_audio(self):
        """诊断音频数据"""
        self.log("🔍 诊断音频数据...")
        self.send_cmd(CMD_DIAG)
    
    def test_recording(self):
        """测试录音5秒"""
        if not self.serial_port or not self.serial_port.is_open:
            messagebox.showwarning("警告", "请先连接串口")
            return
        
        self.audio_buffer = bytearray()
        self.is_recording = True
        self.recording_start_time = time.time()
        
        self.recording_label.config(text="● 测试录音中...", foreground='red')
        self.log("🎤 开始测试录音 (5秒)")
        
        # 5秒后自动停止
        self.root.after(5000, self.stop_test_recording)
    
    def stop_test_recording(self):
        """停止测试录音"""
        self.is_recording = False
        duration = time.time() - self.recording_start_time
        self.recording_label.config(text="● 等待录音", foreground='gray')
        self.log(f"✓ 测试录音结束 ({duration:.1f}秒, {len(self.audio_buffer)}字节)")
        
        if len(self.audio_buffer) > 0:
            self.audio_queue.put(bytes(self.audio_buffer))
            self.audio_buffer = bytearray()
    
    def show_config_dialog(self):
        """显示配置对话框"""
        dialog = tk.Toplevel(self.root)
        dialog.title("API配置")
        dialog.geometry("500x400")
        
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # 百度ASR
        ttk.Label(frame, text="百度ASR:", font=('', 10, 'bold')).grid(row=0, column=0, sticky='w', pady=5)
        ttk.Label(frame, text="App ID:").grid(row=1, column=0, sticky='w')
        self.baidu_app_id = ttk.Entry(frame, width=40)
        self.baidu_app_id.grid(row=1, column=1, padx=5, pady=2)
        self.baidu_app_id.insert(0, self.config.get('baidu_app_id', ''))
        
        ttk.Label(frame, text="API Key:").grid(row=2, column=0, sticky='w')
        self.baidu_api_key = ttk.Entry(frame, width=40)
        self.baidu_api_key.grid(row=2, column=1, padx=5, pady=2)
        self.baidu_api_key.insert(0, self.config.get('baidu_api_key', ''))
        
        ttk.Label(frame, text="Secret Key:").grid(row=3, column=0, sticky='w')
        self.baidu_secret_key = ttk.Entry(frame, width=40)
        self.baidu_secret_key.grid(row=3, column=1, padx=5, pady=2)
        self.baidu_secret_key.insert(0, self.config.get('baidu_secret_key', ''))
        
        # 智谱AI
        ttk.Label(frame, text="智谱AI:", font=('', 10, 'bold')).grid(row=4, column=0, sticky='w', pady=(15,5))
        ttk.Label(frame, text="API Key:").grid(row=5, column=0, sticky='w')
        self.zhipu_api_key = ttk.Entry(frame, width=40)
        self.zhipu_api_key.grid(row=5, column=1, padx=5, pady=2)
        self.zhipu_api_key.insert(0, self.config.get('zhipu_api_key', ''))
        
        # 阿里通义
        ttk.Label(frame, text="阿里通义:", font=('', 10, 'bold')).grid(row=6, column=0, sticky='w', pady=(15,5))
        ttk.Label(frame, text="API Key:").grid(row=7, column=0, sticky='w')
        self.tongyi_api_key = ttk.Entry(frame, width=40)
        self.tongyi_api_key.grid(row=7, column=1, padx=5, pady=2)
        self.tongyi_api_key.insert(0, self.config.get('tongyi_api_key', ''))
        
        # 百度文心
        ttk.Label(frame, text="百度文心:", font=('', 10, 'bold')).grid(row=8, column=0, sticky='w', pady=(15,5))
        ttk.Label(frame, text="API Key:").grid(row=9, column=0, sticky='w')
        self.wenxin_api_key = ttk.Entry(frame, width=40)
        self.wenxin_api_key.grid(row=9, column=1, padx=5, pady=2)
        self.wenxin_api_key.insert(0, self.config.get('wenxin_api_key', ''))
        
        ttk.Label(frame, text="Secret Key:").grid(row=10, column=0, sticky='w')
        self.wenxin_secret_key = ttk.Entry(frame, width=40)
        self.wenxin_secret_key.grid(row=10, column=1, padx=5, pady=2)
        self.wenxin_secret_key.insert(0, self.config.get('wenxin_secret_key', ''))
        
        # 保存按钮
        ttk.Button(frame, text="保存", command=lambda: self.save_api_config(dialog)).grid(
            row=11, column=1, pady=20, sticky='e')
    
    def save_api_config(self, dialog):
        """保存API配置"""
        self.config['baidu_app_id'] = self.baidu_app_id.get()
        self.config['baidu_api_key'] = self.baidu_api_key.get()
        self.config['baidu_secret_key'] = self.baidu_secret_key.get()
        self.config['zhipu_api_key'] = self.zhipu_api_key.get()
        self.config['tongyi_api_key'] = self.tongyi_api_key.get()
        self.config['wenxin_api_key'] = self.wenxin_api_key.get()
        self.config['wenxin_secret_key'] = self.wenxin_secret_key.get()
        
        self.save_config()
        dialog.destroy()
        messagebox.showinfo("提示", "配置已保存")
    
    def save_settings(self):
        """保存设置"""
        self.config['serial_port'] = self.port_combo.get()
        self.config['default_model'] = self.model_var.get()
        self.save_config()
        self.log("✓ 设置已保存")
    
    def log(self, message):
        """写日志"""
        def update():
            self.log_text.insert(tk.END, f"{message}\n")
            self.log_text.see(tk.END)
        self.root.after(0, update)
    
    def update_ui(self):
        """更新UI"""
        self.root.after(100, self.update_ui)
    
    def run(self):
        """运行"""
        self.root.mainloop()


if __name__ == "__main__":
    app = VoiceAssistant()
    app.run()
