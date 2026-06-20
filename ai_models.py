import os, tarfile, shutil, urllib.request, tempfile
import numpy as np

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _resolve_path(p):
    if os.path.isabs(p): return p
    return os.path.join(_SCRIPT_DIR, p)

# ── STT ──────────────────────────────────────────────────────────
SENSE_VOICE_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
                   "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2")
SENSE_VOICE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"

class STTHandler:
    def __init__(self, config, on_status=None):
        self.config = config
        self.on_status = on_status
        self.recognizer = None
        self.reload(config)

    def _log(self, msg):
        if self.on_status: self.on_status(msg)

    def reload(self, config):
        self.config = config
        self.model_dir = _resolve_path(config.get("model_dir", "models/sense-voice-zh-en"))
        self.model_type = config.get("model_type", "sense_voice")
        self.sample_rate = config.get("sample_rate", 16000)
        self._ensure_model()

    def _ensure_model(self):
        mp = os.path.join(self.model_dir, "model.int8.onnx")
        tp = os.path.join(self.model_dir, "tokens.txt")
        if os.path.isfile(mp) and os.path.isfile(tp):
            self._log(f"STT 模型: {self.model_dir}")
            self._init_recognizer(mp, tp); return
        os.makedirs(self.model_dir, exist_ok=True)
        self._download_model()
        for fn in ("model.int8.onnx", "model.onnx"):
            p = os.path.join(self.model_dir, fn)
            if os.path.isfile(p):
                self._init_recognizer(p, os.path.join(self.model_dir, "tokens.txt"))
                return
        raise FileNotFoundError(f"模型文件未找到: {self.model_dir}")

    def _download_model(self):
        self._log("正在下载 STT 模型 (可能较大)...")
        with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False) as tmp:
            tmp_path = tmp.name
            try:
                req = urllib.request.Request(SENSE_VOICE_URL, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    total = int(resp.headers.get("Content-Length", 0)); dl = 0
                    while True:
                        c = resp.read(65536)
                        if not c: break
                        tmp.write(c); dl += len(c)
                        if total > 0:
                            self._log(f"STT 下载: {dl*100//total}% ({dl//1048576}MB/{total//1048576}MB)")
                tmp.flush()
                self._log("STT 下载完成，解压中...")
                with tarfile.open(tmp_path, "r:bz2") as tar:
                    if os.path.exists(self.model_dir): shutil.rmtree(self.model_dir)
                    tar.extractall(path=self.model_dir)
                ep = os.path.join(self.model_dir, SENSE_VOICE_DIR)
                if os.path.isdir(ep):
                    for f in os.listdir(ep):
                        shutil.move(os.path.join(ep, f), os.path.join(self.model_dir, f))
                    os.rmdir(ep)
                self._log("STT 模型就绪")
            finally:
                try: os.unlink(tmp_path)
                except: pass

    def _init_recognizer(self, model_path, tokens_path):
        import sherpa_onnx
        self._log("初始化 STT 引擎...")
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model_path, tokens=tokens_path, num_threads=2,
            sample_rate=16000, decoding_method="greedy_search",
            debug=False, provider="cpu", language="auto", use_itn=True)
        self._log("STT 引擎就绪")

    def recognize(self, samples_f32):
        if self.recognizer is None or len(samples_f32) < 160: return ""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(self.sample_rate, samples_f32)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()

# ── TTS ──────────────────────────────────────────────────────────
VITS_ZH_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
               "sherpa-onnx-vits-zh-ll.tar.bz2")
VITS_ZH_DIR = "sherpa-onnx-vits-zh-ll"

TTS_PRESETS = {
    "vits-zh-ll": {
        "url": VITS_ZH_URL, "dir": VITS_ZH_DIR,
        "model_file": "model.onnx", "label": "VITS 中文(女声)" },
}

class TTSHandler:
    def __init__(self, config, on_status=None):
        self.config = config
        self.on_status = on_status
        self.tts = None
        self.reload(config)

    def _log(self, msg):
        if self.on_status: self.on_status(msg)

    def reload(self, config):
        self.config = config
        self.enabled = config.get("enabled", False)
        self.model_dir = _resolve_path(config.get("model_dir", "models/vits-zh-ll"))
        self.model_type = config.get("model_type", "vits")
        self.speaker_id = config.get("speaker_id", 0)
        self.speed = config.get("speed", 1.0)
        if self.enabled:
            self._ensure_model()
        else:
            self.tts = None

    def _ensure_model(self):
        mp = os.path.join(self.model_dir, "model.onnx")
        tp = os.path.join(self.model_dir, "tokens.txt")
        if os.path.isfile(mp) and os.path.isfile(tp):
            self._log(f"TTS 模型: {self.model_dir}")
            self._init_tts(mp, tp); return
        os.makedirs(self.model_dir, exist_ok=True)
        self._download_model()
        if os.path.isfile(mp) and os.path.isfile(tp):
            self._init_tts(mp, tp); return
        raise FileNotFoundError(f"TTS 模型文件未找到: {self.model_dir}")

    def _download_model(self):
        self._log("正在下载 TTS 模型...")
        with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False) as tmp:
            tmp_path = tmp.name
            try:
                req = urllib.request.Request(VITS_ZH_URL, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    total = int(resp.headers.get("Content-Length", 0)); dl = 0
                    while True:
                        c = resp.read(65536)
                        if not c: break
                        tmp.write(c); dl += len(c)
                        if total > 0:
                            self._log(f"TTS 下载: {dl*100//total}% ({dl//1048576}MB/{total//1048576}MB)")
                tmp.flush()
                self._log("TTS 下载完成，解压中...")
                with tarfile.open(tmp_path, "r:bz2") as tar:
                    if os.path.exists(self.model_dir): shutil.rmtree(self.model_dir)
                    tar.extractall(path=self.model_dir)
                ep = os.path.join(self.model_dir, "sherpa-onnx-vits-zh-ll")
                if os.path.isdir(ep):
                    for f in os.listdir(ep):
                        shutil.move(os.path.join(ep, f), os.path.join(self.model_dir, f))
                    os.rmdir(ep)
                self._log("TTS 模型就绪")
            finally:
                try: os.unlink(tmp_path)
                except: pass

    def _init_tts(self, model_path, tokens_path):
        import sherpa_onnx
        self._log("初始化 TTS 引擎...")
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=model_path, tokens=tokens_path, lexicon=os.path.join(os.path.dirname(model_path), 'lexicon.txt')),
                num_threads=2, provider="cpu", debug=False),
            rule_fsts='', max_num_sentences=1)
        self.tts = sherpa_onnx.OfflineTts(cfg)
        self._log("TTS 引擎就绪")

    def synthesize_to_pcm16(self, text):
        if self.tts is None: return b'', 0
        audio = self.tts.generate(text, sid=self.speaker_id, speed=self.speed)
        if audio is None or audio.samples is None: return b'', 0
        samples = np.array(audio.samples, dtype=np.float32)
        pcm16 = (samples * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
        return pcm16, audio.sample_rate

# ── LLM ──────────────────────────────────────────────────────────
class LLMHandler:
    def __init__(self, config, on_status=None):
        self.config = config
        self.on_status = on_status
        self.local_model = None
        self.local_tokenizer = None
        self.reload(config)

    def _log(self, msg):
        if self.on_status: self.on_status(msg)

    def reload(self, config):
        self.config = config
        self.enabled = config.get("enabled", False)
        self.model_type = config.get("model_type", "openai")
        self.api_base = config.get("api_base", "https://api.openai.com/v1")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", config.get("model_name", "gpt-4o-mini"))
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens", 2048)
        self.system_prompt = config.get("system_prompt", "你是一个智能语音助手，请用中文回答。")
        self.history_len = config.get("history_length", 10)
        self.messages = [{"role": "system", "content": self.system_prompt}]
        if self.model_type == "local" and self.enabled:
            self._load_local_model()

    def _load_local_model(self):
        try:
            import os
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            from transformers import AutoModelForCausalLM, AutoTokenizer
            self._log(f"加载本地模型: {self.model}...")
            self.local_tokenizer = AutoTokenizer.from_pretrained(self.model, trust_remote_code=True)
            self.local_model = AutoModelForCausalLM.from_pretrained(
                self.model, device_map="cpu", torch_dtype="auto", trust_remote_code=True)
            self._log("本地模型加载完成")
        except Exception as e:
            self._log(f"本地模型加载失败: {e}")
            self.local_model = None

    def chat(self, user_message):
        if not self.enabled: return ""
        self.messages.append({"role": "user", "content": user_message})
        if self.model_type == "local":
            reply = self._chat_local(user_message)
        else:
            reply = self._chat_api(user_message)
        if reply:
            self.messages.append({"role": "assistant", "content": reply})
            if len(self.messages) > self.history_len * 2 + 1:
                self.messages = [self.messages[0]] + self.messages[-(self.history_len * 2):]
        return reply

    def _chat_local(self, user_message):
        if not self.local_model or not self.local_tokenizer:
            return "本地模型未加载"
        try:
            import torch
            text = self.local_tokenizer.apply_chat_template(self.messages, tokenize=False, add_generation_prompt=True)
            inputs = self.local_tokenizer([text], return_tensors="pt")
            with torch.no_grad():
                outputs = self.local_model.generate(
                    inputs.input_ids, max_new_tokens=self.max_tokens,
                    temperature=self.temperature, do_sample=True)
            reply = self.local_tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
            return reply.strip()
        except Exception as e:
            return f"本地模型错误: {e}"

    def _chat_api(self, user_message):
        headers = {"Content-Type": "application/json"}
        if self.api_key: headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "messages": self.messages,
                   "temperature": self.temperature, "max_tokens": self.max_tokens}
        try:
            import requests
            r = requests.post(f"{self.api_base.rstrip('/')}/chat/completions",
                              headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            reply = r.json()["choices"][0]["message"]["content"].strip()
            return reply
        except Exception as e:
            return f"LLM 请求失败: {e}"

    def clear(self):
        self.messages = [self.messages[0]]
        return "对话已清空"
