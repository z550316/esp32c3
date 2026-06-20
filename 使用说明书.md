# 语音助手 - 使用说明书

---

## 一、软件功能概述

本系统由两部分组成：

### 1.1 ESP32-C3 固件（硬件端）
- **语音采集**: 通过 I2S 接口连接 ES8311 音频编码器，16kHz 立体声采样
- **音频播放**: 通过 I2S 输出音频到喇叭
- **按键录音**: GPIO9 按键 — 按下开始录音，松开停止并发送
- **TCP 服务**: 端口 11348，接收 PC 端命令和音频数据
- **WiFi 连接**: 支持 STA 模式（连接路由器）和 AP 模式（配置页面）
- **AP 配置页**: 连接 ESP 发出的 `Xiaozhi-xxxx` 热点后访问 192.168.4.1 配置 WiFi
- **串口回退**: 在没有 WiFi 时自动使用串口通信
- **WiFi 扫描**: 支持扫描周围 WiFi 热点

### 1.2 PC 端助手（软件端）
- **语音识别 (STT)**: 使用 SenseVoice 中英文模型，离线运行
- **语音合成 (TTS)**: 使用 VITS 中文模型，5 种角色可选
- **大语言模型 (LLM)**: 支持本地模型 (Qwen2.5-0.5B) 和 OpenAI API
- **热键录音**: Ctrl+Alt+R — 按住录音，松开识别
- **文字模式（默认）**: 语音识别结果直接打字到当前窗口，不触发语音回答
- **对话模式**: 说"小助手"、问时间/天气等才进入语音对话模式（TTS 语音回复）
- **命令系统**: 
  - `/ask 问题` — 向 LLM 提问
  - `/search 关键词` — 搜索网络
  - `/clear` — 清空对话历史
- **应用/文件/目录控制**: 语音打开或关闭软件、文件、文件夹（可在 `config.json` 自定义路径）
- **音乐播放**: 语音搜索并播放歌曲
- **音量控制**: 语音调节音量
- **打字输出**: 将识别结果自动键入到当前焦点窗口
- **Web 配置页**: http://127.0.0.1:18099 可视化配置

### 1.3 通信协议
帧格式: `AA 55 <len:4LE> <cmd:1> [data]`
- `0x01` — 录音开始/音频数据
- `0x02` — 录音停止
- `0x03` — 录音已开始（ACK）
- `0x04` — 播放音频
- `0x05` — 播放完成
- `0x06` — 播放提示音
- `0x07` — 播放旋律
- `0x0A` — 设置音量
- `0x0B` — 设置 WiFi
- `0x0C` — 扫描 WiFi
- `0x0D` — WiFi 列表

---

## 二、WiFi 配置方法

### 2.1 AP 模式配置（推荐）
1. ESP 首次上电或未保存 WiFi 时，会自动进入 AP 模式
2. 用手机/电脑搜索 WiFi 热点 `Xiaozhi-xxxx`，连接（无密码）
3. 浏览器访问 `http://192.168.4.1`
4. 在页面中选择或输入 WiFi SSID 和密码，点击"保存"
5. ESP 自动重启并连接指定 WiFi

### 2.2 串口命令配置
1. 用串口工具连接 ESP（115200 或 921600 波特率）
2. 发送帧: `AA 55 <len:4LE> 0B <ssid_len> <ssid> <password>`
   - `<ssid_len>` — SSID 长度（1 字节）
   - `<ssid>` — WiFi 名称
   - `<password>` — WiFi 密码
3. ESP 会自动重启并连接

### 2.3 PC 助手 Web 页配置
1. PC 助手启动后会显示 `配置页面: http://127.0.0.1:18099`
2. 浏览器打开该地址
3. 切换到"连接"选项卡
4. 选择 WiFi 模式，输入 ESP 的 IP 地址
5. 点击"重连设备"

---

## 三、工具包部署说明

### 3.1 目录结构
```
esp32c3/
├── pc_assistant/                    # PC 端助手
│   ├── assistant.py                 # 主程序
│   ├── ai_models.py                 # STT/TTS/LLM 模型封装
│   ├── config.json                  # 配置文件
│   ├── install_deps.py              # 自动依赖安装脚本
│   ├── requirements.txt             # Python 依赖清单
│   ├── start.bat                    # Windows 启动脚本
│   ├── serial_monitor.py            # 串口调试工具
│   └── models/                      # AI 模型
│       ├── sense-voice-zh-en/       # 语音识别模型
│       └── vits-zh-ll/              # 语音合成模型
├── esp32c3_firmware/                # ESP32-C3 固件
│   ├── esp32c3_voice/esp32c3_voice.ino  # 主程序
│   └── 硬件记忆文档.md                  # 硬件参数记录
└── models/
    └── llm/                         # LLM 模型文件
```

### 3.2 PC 端安装与启动
**Windows:**
1. 双击 `start.bat` — 自动检查并安装依赖，然后启动助手

**手动安装:**
```bash
cd pc_assistant
pip install -r requirements.txt
python assistant.py
```

**首次启动会自动模型:**
- 首次运行会自动下载 STT 模型（SenseVoice，~200MB）
- 首次运行会自动下载 TTS 模型（VITS，~100MB）
- LLM 模型 (Qwen2.5-0.5B) 会在首次使用时从 HuggingFace 下载（~1GB）

### 3.3 ESP32-C3 固件烧录
1. 安装 Arduino IDE 或 arduino-cli
2. 安装 ESP32 开发板支持包:
   ```
   arduino-cli core install esp32:esp32
   ```
3. 编译并烧录:
   ```
   cd esp32c3_firmware/esp32c3_voice
   arduino-cli compile --fqbn esp32:esp32:esp32c3
   arduino-cli upload --fqbn esp32:esp32:esp32c3 -p COM端口
   ```

### 3.4 移植到其他电脑
整个 `esp32c3` 文件夹可以复制到任何 Windows 电脑:
1. 复制整个文件夹到目标电脑
2. 进入 `pc_assistant/` 目录
3. 双击 `start.bat`
4. 脚本会自动安装所有 Python 依赖
5. AI 模型如果不存在会自动下载
6. 安装完成后等待模型加载，即可开始使用

---

## 四、配置文件说明

配置文件 `pc_assistant/config.json`:

```json
{
    "hotkey": "ctrl+alt+r",
    "web": {
        "enabled": true,
        "host": "127.0.0.1",
        "port": 18099
    },
    "connection": {
        "mode": "wifi",
        "serial": {
            "port": "COM4",
            "baudrate": 921600,
            "timeout": 5
        },
        "wifi": {
            "host": "192.168.1.215",
            "port": 11348
        }
    },
    "typing": {
        "enabled": true,
        "speed": 0.005
    },
    "commands": {
        "ask_prefix": "/ask",
        "search_prefix": "/search",
        "clear_prefix": "/clear"
    },
    "behavior": {
        "auto_llm": true,
        "silent_mode": false
    },
    "stt": {
        "model_type": "sense_voice",
        "model_dir": "models/sense-voice-zh-en",
        "sample_rate": 16000
    },
    "tts": {
        "enabled": true,
        "model_type": "vits",
        "model_dir": "models/vits-zh-ll",
        "speed": 1.0,
        "speaker_id": 2,
        "volume_gain": 130
    },
    "llm": {
        "enabled": true,
        "model_type": "local",
        "temperature": 0.7,
        "max_tokens": 2048,
        "system_prompt": "你是一个智能语音助手，请用中文回答。回答简洁准确，不超过3句话。",
        "history_length": 10,
        "model": "Qwen/Qwen2.5-0.5B-Instruct"
    },
    "apps": {}
}
```

### 配置项详解

| 字段 | 说明 | 可选值 |
|------|------|--------|
| `hotkey` | 全局录音热键 | 任何 keyboard 库支持的组合键 |
| `connection.mode` | 通信模式 | `"serial"` 串口 / `"wifi"` WiFi |
| `connection.serial.port` | 串口号 | 如 `"COM3"` |
| `connection.serial.baudrate` | 串口波特率 | `115200` / `921600` |
| `connection.wifi.host` | ESP32 IP 地址 | 如 `"192.168.1.215"` |
| `connection.wifi.port` | TCP 端口 | `11348` |
| `typing.enabled` | 是否启用打字输出 | `true` / `false` |
| `commands.ask_prefix` | LLM 提问前缀 | 如 `"/ask"` |
| `commands.search_prefix` | 搜索前缀 | 如 `"/search"` |
| `behavior.auto_llm` | 无前缀时是否自动 LLM | `true` / `false` |
| `behavior.silent_mode` | 静音模式（关闭喇叭） | `true` / `false` |
| `stt.model_dir` | STT 模型路径 | 相对路径（相对于脚本目录） |
| `stt.sample_rate` | STT 采样率 | `16000` |
| `tts.speaker_id` | TTS 音色 ID | `0` 苏映雪 / `1` 古妮 / `2` 傅诗雨 / `3` 冰娇 / `4` 巴总 |
| `tts.speed` | TTS 语速 | `0.5` ~ `2.0` |
| `tts.volume_gain` | TTS 音量增益 | `0.5` ~ `200` |
| `llm.model_type` | LLM 类型 | `"local"` 本地 / `"openai"` API |
| `llm.model` | 模型标识 | 本地: HuggingFace 模型名 / API: 模型名 |
| `llm.temperature` | LLM 温度参数 | `0` ~ `2` |
| `llm.max_tokens` | 最大生成长度 | `64` ~ `32768` |
| `llm.system_prompt` | 系统提示词 | 自定义 |
| `llm.history_length` | 对话历史轮数 | `1` ~ `100` |

---

## 五、使用说明

### 5.1 基本使用流程
1. 给 ESP32-C3 上电
2. 确保 ESP 和 PC 在同一个 WiFi 网络
3. 启动 PC 助手 (`start.bat`)
4. 等待模型加载完成（显示"语音助手就绪"）
5. 按 ESP 的 GPIO9 按键开始说话
6. 松手后，PC 自动进行语音识别 → LLM 处理 → TTS 合成 → 喇叭播放

### 5.2 按键录音（推荐）
- 按下 GPIO9 按键 → 开始录音
- 松开 → 自动结束录音并发送到 PC 处理
- LED 亮起表示正在录音

### 5.3 热键录音
- 按住 `Ctrl+Alt+R` → 开始录音
- 松开 → 自动结束录音并处理

### 5.4 文字模式 / 对话模式

系统默认处于 **文字模式** — 说的话会直接打字到当前焦点窗口，不触发语音回复。

以下情况会自动进入 **对话模式**（语音合成回复）：
- 以"小助手"开头说话（如"小助手现在几点" → 剥离唤醒词后执行指令）
- 说"语音对话模式"开头
- 询问时间（如"现在几点"、"今天星期几"）
- 询问天气（如"今天天气怎么样"、"温度多少"）

> 示例：说"小助手 帮我搜索今天的新闻" → 先打字输出"小助手 帮我搜索今天的新闻"，然后进入对话模式搜索并语音播报结果。

### 5.5 语音控制应用、文件和目录

说"打开xxx"即可打开软件、文件或文件夹。支持预配置和智能匹配：

**系统默认应用**（可直接说名称）：
- 计算器、记事本、画图、命令提示符、浏览器、Edge、控制面板、任务管理器、设置、截图、录音机、时钟

**自定义路径**（在 `config.json` 的 `"apps"` 字段添加）：
```json
{
    "apps": {
        "我的文档": "C:\\Users\\Administrator\\Documents",
        "微信": "C:\\Program Files\\Tencent\\WeChat\\WeChat.exe",
        "项目": "E:\\github\\esp32c3",
        "百度": "https://www.baidu.com"
    }
}
```

说"打开我的文档"→ 打开文件夹 | "打开微信"→ 启动微信 | "打开项目"→ 打开目录

> 路径可以是 exe 文件、文件夹路径、网址或任意 `start` 命令支持的内容。

说"关闭xxx"可关闭正在运行的软件（如"关闭计算器"）。

### 5.6 音量控制
说"音量调到 50"、"大声一点"、"静音"等。

---

## 六、故障排除

| 问题 | 可能原因 | 解决 |
|------|----------|------|
| 连接失败 | IP 地址不对 | 在配置页重新输入 ESP 的 IP |
| 录音无反应 | 模型未加载完 | 等待"语音助手就绪"提示 |
| 录音太短 | GPIO 按键松太快 | 确保说话时长超过 0.5 秒 |
| 识别不准 | 环境噪音大 | 靠近麦克风说话 |
| LLM 无响应 | 本地模型加载慢 | 等待"思考中..."变为"回答" |
| 播放无声 | 音量太小/静音 | 说"音量调到 80" |
| 程序闪退 | 依赖缺失 | 运行 `install_deps.py` 安装 |

---

*文档版本: 1.0*
