#!/usr/bin/env python3
"""
Meeting Notes Generator / 会议纪要生成工具
Run: python3 server.py  →  open http://localhost:5001
"""

import os
import sys
import json
import tempfile
import threading
import webbrowser
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import cgi


def check_deps():
    missing = []
    try:
        import whisper
    except ImportError:
        missing.append("openai-whisper")
    try:
        import openai
    except ImportError:
        missing.append("openai")
    if missing:
        print("❌ Missing dependencies. Please run:")
        print(f"   pip3 install {' '.join(missing)}")
        sys.exit(1)

check_deps()

import whisper
from openai import OpenAI

# ── Config ────────────────────────────────────────────────────────────────────

PORT = 5001
DEFAULT_OUTPUT_DIR = str(Path.home() / "Desktop" / "MeetingNotes")
CONFIG_FILE = Path(__file__).parent / "config.json"

PROVIDERS = {
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_field": "groq_api_key",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "key_field": "openai_api_key",
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "key_field": "deepseek_api_key",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "qwen": {
        "name": "通义千问 (Qwen)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_field": "qwen_api_key",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
    },
    "anthropic": {
        "name": "Claude (Anthropic)",
        "base_url": None,
        "key_field": "anthropic_api_key",
        "models": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    },
}

NOTES_PROMPT = {
    "zh": """你是一位专业的音频内容整理员。
请根据用户提供的音频转录文字，生成一份结构清晰的中文音频纪要。

输出格式（Markdown）：

## 音频纪要

### 📋 基本信息
- 时长：
- 主要内容方向：（从内容推断）

### 🗣️ 主要讨论议题
（列出 3-6 个核心议题）

### 💡 讨论方案与思路
（列出音频中提出的方案、考虑的角度和不同观点，无论是否已有定论）

### ⏳ 待跟进事项（Action Items）
（格式：- [ ] 事项描述 → 负责方/时限（如有提及））

### 🔑 关键决议
（列出重要决定）

### 📝 其他备注
（补充说明，如无则省略此节）

注意：如果转录内容较短或信息不足，如实说明，不要编造。保持客观，使用简洁要点。""",

    "en": """You are a professional audio content summariser.
Based on the transcript provided, produce a structured meeting notes document in English.

Output format (Markdown):

## Meeting Notes

### 📋 Overview
- Duration:
- Main topic: (inferred from content)

### 🗣️ Key Discussion Topics
(List 3–6 core topics)

### 💡 Ideas & Approaches Discussed
(List proposals, angles considered, and differing views — regardless of whether they were resolved)

### ⏳ Action Items
(Format: - [ ] Description → Owner / Deadline (if mentioned))

### 🔑 Key Decisions
(List important decisions made)

### 📝 Additional Notes
(Supplementary remarks; omit this section if none)

Note: if the transcript is short or lacks detail, say so honestly — do not fabricate. Stay objective and use concise bullet points.""",
}

TRANSCRIPT_PROMPT = {
    "zh": """你是一位专业的转录整理员。
根据以下音频转录文字，将内容整理为对话格式，尝试识别不同的发言人。

规则：
- 根据语气、话题切换、问答关系等推断发言人更换
- 用「说话者 A:」「说话者 B:」等标注（最多标注 6 位发言人）
- 如无法区分则用「说话者 ?:」
- 保留原始语言和内容，不修改、不省略、不总结
- 每段发言另起一行，段落之间空一行

注意：本工具不具备声纹识别能力，发言人标注为基于内容的 AI 推断，仅供参考。""",

    "en": """You are a professional transcript formatter.
Based on the raw transcript below, reformat the content as a dialogue and attempt to identify different speakers.

Rules:
- Infer speaker changes from tone, topic shifts, and question-answer patterns
- Label speakers as "Speaker A:", "Speaker B:", etc. (up to 6 speakers)
- Use "Speaker ?:" when a speaker cannot be determined
- Preserve the original language and content — do not edit, omit, or summarise
- Start a new line for each turn; leave a blank line between turns

Note: this tool has no voice-recognition capability. Speaker labels are AI inferences based on content only and should be treated as approximate.""",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(data: dict):
    existing = load_config()
    existing.update(data)
    CONFIG_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Core ──────────────────────────────────────────────────────────────────────

whisper_model = None


def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        print("📥 Loading Whisper model (first run downloads ~140MB)...")
        whisper_model = whisper.load_model("base")
        print("✅ Whisper ready")
    return whisper_model


def transcribe_audio(audio_path: str, lang: str = "zh") -> dict:
    model = get_whisper_model()
    print(f"🎙️  Transcribing: {audio_path}")
    result = model.transcribe(audio_path, language=None if lang == "auto" else lang, verbose=False)
    segments = result.get("segments", [])
    duration_sec = segments[-1]["end"] if segments else 0
    return {
        "text": result["text"].strip(),
        "duration": f"{int(duration_sec // 60)}m {int(duration_sec % 60)}s",
    }


def _call_llm(system_prompt: str, user_content: str, provider_id: str, model: str,
              api_key: str, max_tokens: int = 4096) -> str:
    provider = PROVIDERS[provider_id]
    if provider_id == "anthropic":
        try:
            import anthropic as anthropic_sdk
        except ImportError:
            raise RuntimeError("Missing dependency. Please run: pip3 install anthropic")
        client = anthropic_sdk.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model, max_tokens=max_tokens, system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return msg.content[0].text.strip()
    client = OpenAI(api_key=api_key, base_url=provider["base_url"])
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        temperature=0.3, max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


def summarize(transcript: str, duration: str, provider_id: str, model: str, api_key: str,
              ui_lang: str = "zh") -> str:
    prompt = NOTES_PROMPT.get(ui_lang, NOTES_PROMPT["en"])
    label = "音频时长" if ui_lang == "zh" else "Duration"
    content_label = "转录内容" if ui_lang == "zh" else "Transcript"
    return _call_llm(prompt, f"{label}：{duration}\n\n{content_label}：\n\n{transcript}",
                     provider_id, model, api_key)


def generate_transcript(transcript: str, provider_id: str, model: str, api_key: str,
                        ui_lang: str = "zh") -> str:
    prompt = TRANSCRIPT_PROMPT.get(ui_lang, TRANSCRIPT_PROMPT["en"])
    content_label = "转录内容" if ui_lang == "zh" else "Transcript"
    return _call_llm(prompt, f"{content_label}：\n\n{transcript}",
                     provider_id, model, api_key, max_tokens=8192)


def process_audio(audio_path: str, lang: str, provider_id: str, model: str, api_key: str,
                  filename: str, output_dir: str, fmt: str, ui_lang: str = "zh") -> dict:
    try:
        result = transcribe_audio(audio_path, lang)
        formatted_transcript = generate_transcript(result["text"], provider_id, model, api_key, ui_lang)
        notes = summarize(result["text"], result["duration"], provider_id, model, api_key, ui_lang)

        out_dir = Path(output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        stem = Path(filename).stem
        ext = ".txt" if fmt == "txt" else ".md"
        footer = f"\n\n---\n*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} · Model: {provider_id}/{model}*\n"

        transcript_path = out_dir / f"{stem}_transcript_{timestamp}{ext}"
        notes_path = out_dir / f"{stem}_notes_{timestamp}{ext}"

        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(formatted_transcript + footer)
        with open(notes_path, "w", encoding="utf-8") as f:
            f.write(notes + footer)

        return {
            "success": True,
            "transcript": formatted_transcript,
            "summary": notes,
            "duration": result["duration"],
            "transcript_saved_to": str(transcript_path),
            "notes_saved_to": str(notes_path),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── HTTP ──────────────────────────────────────────────────────────────────────

HTML_FILE = Path(__file__).parent / "index.html"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            content = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/process":
            content_type = self.headers.get("Content-Type", "")
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
            )

            model_val = form.getvalue("model", "").strip()
            if "||" not in model_val:
                self._json({"success": False, "error": "Please select a model"})
                return
            provider_id, model = model_val.split("||", 1)
            if provider_id not in PROVIDERS:
                self._json({"success": False, "error": f"Unknown provider: {provider_id}"})
                return

            api_key = form.getvalue(f"api_key_{provider_id}", "").strip()
            save_keys = form.getvalue("save_keys", "false") == "true"
            lang = form.getvalue("lang", "zh")
            ui_lang = form.getvalue("ui_lang", "zh") if form.getvalue("ui_lang", "zh") in ("zh", "en") else "zh"
            fmt = form.getvalue("format", "md").strip()
            output_dir = form.getvalue("output_dir", DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR
            audio_field = form["audio"] if "audio" in form else None

            if not api_key:
                api_key = load_config().get(PROVIDERS[provider_id]["key_field"], "")

            if save_keys:
                keys_to_save = {}
                for pid, pinfo in PROVIDERS.items():
                    k = form.getvalue(f"api_key_{pid}", "").strip()
                    if k:
                        keys_to_save[pinfo["key_field"]] = k
                if keys_to_save:
                    save_config(keys_to_save)

            if not api_key:
                self._json({"success": False, "error": f"Please enter your {PROVIDERS[provider_id]['name']} API Key"})
                return
            if audio_field is None or not audio_field.filename:
                self._json({"success": False, "error": "Please upload an audio file"})
                return

            suffix = Path(audio_field.filename).suffix
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio_field.file.read())
                tmp_path = tmp.name

            try:
                result = process_audio(tmp_path, lang, provider_id, model, api_key, audio_field.filename, output_dir, fmt, ui_lang)
            finally:
                os.unlink(tmp_path)

            self._json(result)

        elif self.path == "/check_keys":
            cfg = load_config()
            saved = {}
            for pid, pinfo in PROVIDERS.items():
                k = cfg.get(pinfo["key_field"], "")
                saved[pid] = {
                    "has_key": bool(k),
                    "preview": k[:8] + "..." if k else "",
                    "full_key": k,
                }
            self._json(saved)

        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("  🎙️  Meeting Notes Generator")
    print("=" * 50)
    print(f"📁 Default output folder: {DEFAULT_OUTPUT_DIR}")
    print(f"🌐 Starting server...")

    server = HTTPServer(("localhost", PORT), Handler)

    def open_browser():
        import time
        time.sleep(1)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()
    print(f"✅ Running at http://localhost:{PORT}")
    print(f"\n   Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
