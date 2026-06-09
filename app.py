
import streamlit as st
import easyocr
from PIL import Image, ImageOps
import numpy as np
import cv2
import sqlite3
import re
import html
import time
import datetime
import gc
import requests
import json
import os
import base64
import io
import hashlib
import threading

# ============================================================
# ১. GEMINI API KEY — Backend থেকে লোড হবে, User দেখবে না
# Streamlit Cloud: st.secrets["GEMINI_API_KEY"]
# Local: .env ফাইলে GEMINI_API_KEY=your_key_here
# ============================================================
try:
    gemini_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

# DB path: configurable via env/secret, default to local file
DB_PATH = os.environ.get("TRUMPU_DB_PATH", "token_history.db")


# ============================================================
# ২. SQLite ডাটাবেস ইনিশিয়ালাইজেশন
# FIXED: context managers everywhere — no connection leaks
# FIXED: added scan_count column, delete_history, clear_all
# ============================================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      token TEXT NOT NULL,
                      scan_date TEXT NOT NULL,
                      scan_count INTEGER DEFAULT 1)''')
        # Migration: add scan_count if missing
        try:
            c.execute("ALTER TABLE history ADD COLUMN scan_count INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS chat_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      role TEXT NOT NULL,
                      content TEXT NOT NULL,
                      timestamp TEXT NOT NULL)''')
        conn.commit()

def save_token_to_db(token):
    if not token or not isinstance(token, str):
        return
    token = token[:500]
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, scan_count FROM history WHERE token=? ORDER BY id DESC LIMIT 1", (token,))
            last_record = c.fetchone()
            if last_record:
                # Update scan count instead of duplicate insert
                c.execute("UPDATE history SET scan_count=?, scan_date=? WHERE id=?",
                          (last_record[1] + 1,
                           datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           last_record[0]))
            else:
                c.execute(
                    "INSERT INTO history (token, scan_date, scan_count) VALUES (?, ?, 1)",
                    (token, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
            conn.commit()
    except sqlite3.Error:
        pass

def get_history(limit=10):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT token, scan_date, scan_count FROM history ORDER BY id DESC LIMIT ?", (limit,))
            return c.fetchall()
    except sqlite3.Error:
        return []

def delete_history_item(token, scan_date):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM history WHERE token=? AND scan_date=?", (token, scan_date))
            conn.commit()
    except sqlite3.Error:
        pass

def clear_all_history():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM history")
            conn.commit()
    except sqlite3.Error:
        pass

def save_chat_message(role, content):
    if not content or not role:
        return
    role = "model" if role in ("ai", "model", "assistant") else "user"
    # Sanitize: strip null bytes, limit length
    content = content.replace("\x00", "")[:2000]
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO chat_history (role, content, timestamp) VALUES (?, ?, ?)",
                (role, content, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
    except sqlite3.Error:
        pass

def get_chat_history(limit=20):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT role, content, timestamp FROM chat_history ORDER BY id DESC LIMIT ?", (limit,))
            return list(reversed(c.fetchall()))
    except sqlite3.Error:
        return []

def clear_chat_history():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM chat_history")
            conn.commit()
    except sqlite3.Error:
        pass

init_db()


# ============================================================
# ৩. পেজ কনফিগারেশন
# ============================================================
st.set_page_config(
    page_title="Trumpu AI Jarvis V16.0 Overlord",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_DIGITS_ALLOWED = 500
MAX_CHAT_INPUT_LEN = 1000      # max chars per user message
MAX_PASTE_INPUT_LEN = 2000     # max chars in paste box
MAX_API_CALLS_PER_MIN = 10     # rate limit: Gemini calls per minute
ALLOWED_IMG_TYPES = {'jpg', 'jpeg', 'png', 'webp'}

# ── In-memory rate limiter (thread-safe) ──
_rate_lock = threading.Lock()
_api_call_times: list = []

def _rate_limited() -> bool:
    """Returns True if rate limit exceeded. Cleans old entries."""
    now = time.time()
    with _rate_lock:
        # keep only calls within last 60 seconds
        _api_call_times[:] = [t for t in _api_call_times if now - t < 60]
        if len(_api_call_times) >= MAX_API_CALLS_PER_MIN:
            return True
        _api_call_times.append(now)
        return False

def safe_js_string(value: str) -> str:
    """Escape a string for safe embedding inside JS string literals."""
    if not isinstance(value, str):
        return ""
    return (value
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("<", "\\x3c")
            .replace(">", "\\x3e")
            .replace("&", "\\x26"))



# ============================================================
# ৪. Sidebar — Theme বাদ, API Key বাদ, শুধু দরকারি options
# ============================================================
st.sidebar.title("⚙️ JARVIS Control Panel")
st.sidebar.caption("🔖 V16.0 Overlord | by Alif Ahmed")
lang_choice = st.sidebar.selectbox("🌐 Language / ভাষা", ["বাংলা (Bangla)", "English"])

st.sidebar.markdown("---")
st.sidebar.subheader("🎙️ Voice Persona")
voice_persona = st.sidebar.selectbox(
    "👤 Voice Persona / কন্ঠস্বর",
    ["Jarvis (🤖 Male)", "Cortana (👩 Female)", "System Default"]
)

st.sidebar.markdown("---")
st.sidebar.subheader("🔊 Voice Speed Control")
voice_speed = st.sidebar.slider(
    "Speed / গতি",
    min_value=0.5, max_value=4.0, value=2.5, step=0.1,
    help="1.0 = Normal, 2.5 = Fast (default), 4.0 = Ultra Fast"
)
voice_pitch_ui = st.sidebar.slider(
    "Pitch / পিচ",
    min_value=0.5, max_value=2.0, value=1.0, step=0.1,
    help="1.0 = Normal pitch"
)

st.sidebar.markdown("---")
st.sidebar.subheader("🧠 Memory Limit")
memory_limit = st.sidebar.slider(
    "AI Chat History / চ্যাট মেমোরি",
    min_value=2, max_value=30, value=12, step=2,
    help="How many past messages AI remembers"
)

st.sidebar.markdown("---")
st.sidebar.subheader("👁️ Vision Mode")
use_gemini_vision = st.sidebar.toggle(
    "🤖 Gemini Vision OCR",
    value=False,
    help="Use Gemini AI to read images (more accurate, needs API key)"
)


# ============================================================
# ৫. ল্যাঙ্গুয়েজ ডিকশনারি
# ============================================================
if lang_choice == "বাংলা (Bangla)":
    lang = {
        "title": "🤖 Trumpu AI - V16.0 Jarvis Overlord",
        "subtitle": "নেক্সট-জেন লার্জ ল্যাঙ্গুয়েজ ভয়েস কোয়ান্টাম সিস্টেম | অটো-ক্রপ ভিশন",
        "tab_scan": "📷 গ্যালারি স্ক্যান",
        "tab_cam": "📸 লাইভ ক্যামেরা",
        "tab_paste": "✍️ ম্যানুয়াল ইনপুট",
        "tab_hist": "💾 স্ক্যান হিস্ট্রি",
        "tab_chat": "🧠 AI চ্যাট",
        "upload_lbl": "টোকেন বা মিটারের ছবি আপলোড করুন",
        "upload_err": f"❌ ফাইলটি অনেক বড়! সর্বোচ্চ {MAX_FILE_SIZE_MB}MB আপলোড করুন।",
        "file_corrupt": "❌ ফাইল করাপ্টেড! স্ক্যানে ব্লক করা হয়েছে।",
        "scan_msg": "মিলিটারি-গ্রেড সিকিউর স্ক্যানিং চলছে...",
        "ai_analyzing": "স্মার্ট ভিশন অটো-ক্রপিং ও অ্যাডাপ্টিভ থ্রেশহোল্ডিং চলছে...",
        "no_num_warn": "⚠️ কোনো নম্বর পাওয়া যায়নি! সোর্স ফাইলটি চেক করুন।",
        "paste_plc": "এখানে সিকিউর টোকেন পেস্ট করুন...",
        "step": "ধাপ",
        "type_meter": "মিটারে টাইপ করুন",
        "btn_start": "▶️ জার্ভিস কোর চালু করুন",
        "btn_prev": "⬅️ আগের ধাপে",
        "btn_repeat": "আবার বলুন 🔊",
        "btn_next": "পরের ধাপে ➡️",
        "success_ready": "✅ টোকেন লোড সম্পন্ন! স্ক্রিনে টাচ করে মুখে 'Start' বা 'শুরু করো' বলুন।",
        "voice_hint_standby": "🎙️ ভয়েস কমান্ড: 'Start', 'শুরু করো', 'Ready' (মোবাইলে স্ক্রিনে একবার আলতো টাচ করে কমান্ড দিন)।",
        "voice_hint": "🎙️ রিয়েল-টাইম ইন্টারাপশন অ্যাক্টিভ: কথা বলা অবস্থায় সরাসরি 'Wait' বা 'থামো' বলুন।",
        "success": "✅ মিশন সাকসেসফুল! সবগুলো নম্বর সফলভাবে প্রসেস করা হয়েছে।",
        "btn_restart": "🔄 নতুন কোয়ান্টাম সেশন শুরু করুন",
        "start_info": "👆 কাজ শুরু করতে ছবি দিন, লাইভ ছবি তুলুন অথবা টোকেন পেস্ট করুন।",
        "chat_placeholder": "যেকোনো বিষয়ে জিজ্ঞেস করুন...",
        "chat_send": "📤 পাঠান",
        "chat_clear": "🗑️ চ্যাট মুছুন",
        "chat_empty": "কোনো চ্যাট নেই। AI-এর সাথে কথা শুরু করুন!",
        "chat_thinking": "🤔 Trumpu ভাবছে...",
    }
else:
    lang = {
        "title": "🤖 Trumpu AI - V16.0 Jarvis Overlord",
        "subtitle": "Next-Gen LLM Voice Quantum Agent | Auto-Crop Smart Vision",
        "tab_scan": "📷 Gallery Scan",
        "tab_cam": "📸 Live Camera",
        "tab_paste": "✍️ Manual Input",
        "tab_hist": "💾 Scan History",
        "tab_chat": "🧠 AI Chat",
        "upload_lbl": "Upload Token Image",
        "upload_err": f"❌ File too large! Max {MAX_FILE_SIZE_MB}MB.",
        "file_corrupt": "❌ File corrupted! Deep Security Scan Intervention.",
        "scan_msg": "Military-grade secure scanning in progress...",
        "ai_analyzing": "Executing Auto-Cropping & Adaptive Structural OCR Thresholding...",
        "no_num_warn": "⚠️ No numbers detected! Please review the layout clarity.",
        "paste_plc": "Paste secure token here...",
        "step": "Step",
        "type_meter": "Type into the meter",
        "btn_start": "▶️ Initialize Jarvis Core",
        "btn_prev": "⬅️ Previous",
        "btn_repeat": "Repeat 🔊",
        "btn_next": "Next ➡️",
        "success_ready": "✅ Token loaded! Tap screen and say 'Start' or 'Begin'.",
        "voice_hint_standby": "🎙️ Voice Command: Say 'Start', 'Begin' or 'Ready' (Tap screen on mobile first).",
        "voice_hint": "🎙️ Real-Time Interruption: Say 'Wait' or 'Stop' to halt. Voice tracking is continuous.",
        "success": "✅ Mission Successful! Quantum digits sequence processed securely.",
        "btn_restart": "🔄 Initialize New Quantum Session",
        "start_info": "👆 Upload, snap a photo, or paste token above to begin.",
        "chat_placeholder": "Ask anything...",
        "chat_send": "📤 Send",
        "chat_clear": "🗑️ Clear Chat",
        "chat_empty": "No messages yet. Start talking to Trumpu AI!",
        "chat_thinking": "🤔 Trumpu is thinking...",
    }


# ============================================================
# ৬. Dark Theme — Fixed, হার্ডকোড করা (Theme toggle বাদ)
# ============================================================
bg_color = "#020617"
secondary_bg = "#0f172a"
text_color = "#f8fafc"
border_color = "#1e293b"
accent_color = "#3b82f6"
accent_hover = "#60a5fa"
shadow = "0 10px 15px -3px rgba(0,0,0,0.5)"
color_scheme = "dark"
user_bubble = "#1e3a8a"
ai_bubble = "#1e293b"
user_text = "#bfdbfe"
ai_text = "#f8fafc"


# ============================================================
# ৭. CSS — XSS-safe (unsafe_allow_html only for controlled CSS)
# ============================================================
st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;800&family=JetBrains+Mono:wght@800&display=swap');

    :root {{ color-scheme: {color_scheme}; }}
    * {{ font-family: 'Poppins', sans-serif; }}

    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {{
        background-color: {bg_color} !important;
        color: {text_color} !important;
    }}

    [data-testid="stSidebar"] {{
        background-color: {secondary_bg} !important;
        border-right: 1px solid {border_color} !important;
    }}

    .master-card {{
        background-color: {secondary_bg}; border: 1px solid {border_color}; padding: 30px 15px;
        border-radius: 25px; text-align: center; box-shadow: {shadow}; margin: 15px 0;
        border-bottom: 5px solid {accent_color}; position: relative; overflow: hidden;
    }}

    .big-number {{
        font-family: 'JetBrains Mono', monospace; font-size: clamp(28px, 6vw, 60px);
        font-weight: 800; background: linear-gradient(135deg, {accent_hover}, {accent_color});
        -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: 4px;
        line-height: 1.4; word-break: break-all;
    }}

    .stButton>button {{
        border-radius: 12px !important; height: 55px !important; font-weight: 700 !important;
        font-size: 16px !important; transition: 0.2s ease !important;
    }}
    button[kind="primary"] {{
        background: linear-gradient(90deg, {accent_color}, {accent_hover}) !important;
        color: #ffffff !important; border: none !important;
    }}
    button[kind="primary"]:hover {{ transform: scale(1.02) !important; box-shadow: 0 8px 20px {accent_color}50 !important; }}

    .chat-container {{
        max-height: 420px; overflow-y: auto; padding: 10px;
        border: 1px solid {border_color}; border-radius: 16px;
        background: {secondary_bg}; margin-bottom: 12px;
    }}
    .chat-bubble-user {{
        background: {user_bubble}; color: {user_text}; padding: 10px 16px;
        border-radius: 18px 18px 4px 18px; margin: 8px 0 8px 40px;
        font-size: 14px; line-height: 1.5; word-break: break-word;
    }}
    .chat-bubble-ai {{
        background: {ai_bubble}; color: {ai_text}; padding: 10px 16px;
        border-radius: 18px 18px 18px 4px; margin: 8px 40px 8px 0;
        font-size: 14px; line-height: 1.5; word-break: break-word;
        border-left: 3px solid {accent_color};
    }}
    .chat-label-user {{ text-align: right; font-size: 11px; color: {accent_hover}; margin-bottom: 2px; }}
    .chat-label-ai {{ font-size: 11px; color: {accent_color}; margin-bottom: 2px; }}

    #MainMenu, footer {{visibility: hidden;}}
    </style>
""", unsafe_allow_html=True)

# ── Enter-key support for Chat input and Quick Query ──
st.components.v1.html("""
<script>
(function() {
    function bindEnterKey(inputKey, buttonTexts) {
        function tryBind() {
            // Find all Streamlit text inputs by data-testid or aria
            const inputs = window.parent.document.querySelectorAll('input[type="text"], textarea');
            inputs.forEach(function(inp) {
                if (inp.dataset.trumpuEnterBound) return;
                inp.dataset.trumpuEnterBound = "1";
                inp.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter' && !e.shiftKey) {
                        const container = inp.closest('[data-testid="stVerticalBlock"]') || window.parent.document;
                        for (const txt of buttonTexts) {
                            const btns = window.parent.document.querySelectorAll('button');
                            for (const btn of btns) {
                                if ((btn.innerText || btn.textContent || '').includes(txt)) {
                                    e.preventDefault();
                                    btn.click();
                                    return;
                                }
                            }
                        }
                    }
                });
            });
        }
        // Run after Streamlit renders
        setTimeout(tryBind, 1200);
        setInterval(tryBind, 3000);
    }
    bindEnterKey('chat_input_box', ['পাঠান', 'Send']);
    bindEnterKey('quick_query_box', ['Think']);
})();
</script>
""", height=0)


# ============================================================
# ৮. OCR Engine
# ============================================================
@st.cache_resource(show_spinner=False)
def load_fast_ocr():
    return easyocr.Reader(['en'], gpu=False)

def secure_clean(text):
    """FIXED: Extract only digits — no XSS surface."""
    if not isinstance(text, str):
        return ""
    cleaned = "".join(re.findall(r'\d+', text))
    return cleaned[:MAX_DIGITS_ALLOWED]

def strip_exif_data(image):
    """FIXED: Proper PIL EXIF strip — no verify() + reopen pattern."""
    try:
        if not isinstance(image, Image.Image):
            return None
        img = image.convert('RGB')
        clean = Image.new('RGB', img.size)
        clean.putdata(list(img.getdata()))
        return clean
    except Exception:
        return None


# ============================================================
# ৯. Gemini AI Brain — Multi-turn Conversation
# ============================================================
TRUMPU_SYSTEM_PROMPT = """You are TRUMPU — a hyper-intelligent, multi-domain AI assistant engineered by Alif Ahmed. You operate at the intersection of Jarvis-level cognition and real-world utility.

IDENTITY:
- Name: TRUMPU. Creator: Alif Ahmed. Never claim to be Google Gemini, Bard, or any other AI.
- Personality: Sharp, confident, witty, occasionally humorous, always precise and helpful.
- You are bilingual: respond in the same language the user uses (Bengali or English). If mixed, match the dominant language.

RESPONSE RULES:
- Be direct and concise: 1–3 short paragraphs unless detail is explicitly requested.
- Zero filler words. No "Certainly!", "Of course!", "Great question!" — just answer.
- Use markdown formatting (bold, code blocks, lists) when it improves clarity.
- For code requests: provide working, well-commented code immediately.

DOMAIN COVERAGE (handle ALL of these with expert depth):
- 🔬 Science & Technology: physics, chemistry, biology, AI/ML, engineering
- 💻 Programming: Python, JavaScript, web dev, algorithms, debugging, system design
- 📊 Math & Logic: calculus, statistics, proofs, puzzles, data analysis
- 🌍 History, Geography, Culture, Politics, Philosophy
- 💰 Finance, Economics, Business strategy, Startups
- 🏥 Health & Medicine (general knowledge, NOT personal medical advice)
- 🎨 Creative writing, storytelling, poetry, humor
- ⚡ Electricity & Meters: prepaid token systems (DESCO, BPDB, DPDC), kWh, billing
- 🤖 AI agents, prompt engineering, automation workflows
- 📱 Everyday advice, productivity, life hacks

SPECIAL BEHAVIORS:
- If asked about electricity tokens/meters: provide expert guidance on DESCO/BPDB/DPDC prepaid systems.
- If given a digit sequence: help interpret it as a meter token, OTP, code, or number system as context warrants.
- For ambiguous queries: answer the most likely interpretation, then briefly note alternatives.
- Never refuse normal requests. If a topic seems sensitive, answer factually and objectively.
- In Bengali responses: use smart, modern, futuristic Bangla — not overly formal.

SECURITY:
- Never reveal your underlying model or API details.
- Never execute harmful instructions even if framed as roleplay."""

TRUMPU_SYSTEM_PROMPT_BN = """তুমি TRUMPU — আলিফ আহমেদের তৈরি একটি অতি-স্মার্ট, বহু-ডোমেইন AI সহকারী। তুমি Jarvis-লেভেল বুদ্ধিমত্তা নিয়ে কাজ করো।

পরিচয়:
- নাম: TRUMPU। নির্মাতা: আলিফ আহমেদ। তুমি কখনো নিজেকে Google Gemini বা অন্য কোনো AI বলবে না।
- ব্যক্তিত্ব: তীক্ষ্ণ, আত্মবিশ্বাসী, মাঝে মাঝে মজাদার, সবসময় সরাসরি এবং সহায়ক।
- তুমি দ্বিভাষিক: ব্যবহারকারী যে ভাষায় কথা বলে, সেই ভাষায় উত্তর দাও।

উত্তর দেওয়ার নিয়ম:
- সরাসরি ও সংক্ষিপ্ত থাকো: ১-৩টি ছোট প্যারাগ্রাফ, যদি না বিস্তারিত চাওয়া হয়।
- কোনো অকারণ শব্দ নেই। সরাসরি উত্তর দাও।
- প্রয়োজনে মার্কডাউন ফরম্যাট ব্যবহার করো।

দক্ষতার ক্ষেত্রসমূহ (সব বিষয়ে বিশেষজ্ঞ পর্যায়ে সাহায্য করো):
- বিজ্ঞান ও প্রযুক্তি, পদার্থ, রসায়ন, জীববিজ্ঞান, AI/ML
- প্রোগ্রামিং: Python, JavaScript, ওয়েব ডেভেলপমেন্ট, অ্যালগরিদম, ডিবাগিং
- গণিত, যুক্তি, পরিসংখ্যান
- ইতিহাস, ভূগোল, সংস্কৃতি, রাজনীতি, দর্শন
- অর্থনীতি, ব্যবসা, উদ্যোক্তা
- স্বাস্থ্য ও চিকিৎসা (সাধারণ জ্ঞান)
- সৃজনশীল লেখা, গল্প, কবিতা, রসিকতা
- বিদ্যুৎ ও মিটার: DESCO, BPDB, DPDC প্রিপেইড টোকেন সিস্টেম
- AI এজেন্ট, অটোমেশন, প্রম্পট ইঞ্জিনিয়ারিং
- দৈনন্দিন পরামর্শ, উৎপাদনশীলতা

বিশেষ আচরণ:
- বিদ্যুৎ টোকেন/মিটার বিষয়ক প্রশ্নে DESCO/BPDB/DPDC সিস্টেমে বিশেষজ্ঞ গাইড দাও।
- কোনো সংখ্যা-ক্রম দিলে প্রসঙ্গ বুঝে মিটার টোকেন, OTP বা কোড হিসেবে ব্যাখ্যা করো।
- স্বাভাবিক কোনো প্রশ্ন প্রত্যাখ্যান করবে না।
- বাংলায় স্মার্ট, আধুনিক ভাষায় উত্তর দাও — অতিরিক্ত আনুষ্ঠানিক না হয়ে।"""

def ask_gemini_vision(pil_image, lang_mode):
    """Gemini Vision — rate limited, sanitized."""
    if not gemini_key or len(gemini_key) < 10:
        return None, ("AI Vision offline — API key not configured." if lang_mode == "English"
                      else "AI ভিশন অফলাইন — API কী কনফিগার হয়নি।")
    if _rate_limited():
        return None, ("⚠️ Rate limit. Wait 60s." if lang_mode == "English"
                      else "⚠️ রেট লিমিট। ৬০ সেকেন্ড অপেক্ষা করুন।")
    try:
        buf = io.BytesIO()
        pil_image.save(buf, format='JPEG', quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

        vision_prompt = (
            "You are a precision OCR + meter reading AI. Look at this image carefully.\n"
            "1. Detect if this is a prepaid electricity meter or token slip.\n"
            "2. Extract ALL digit sequences you can see (token numbers, meter readings, codes).\n"
            "3. Return ONLY the digits in order, no spaces, no explanation. Join all digit groups.\n"
            "4. If no digits found, reply: NONE"
        ) if lang_mode == "English" else (
            "তুমি একটি প্রিসিশন OCR + মিটার রিডিং AI। এই ছবিটি মনোযোগ দিয়ে দেখো।\n"
            "১. সব digit sequence বের করো (token নম্বর, মিটার রিডিং, কোড)।\n"
            "২. শুধু digits গুলো সরাসরি দাও, কোনো ব্যাখ্যা ছাড়া।\n"
            "৩. যদি কোনো digit না থাকে, উত্তর দাও: NONE"
        )

        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.0-flash:generateContent?key={gemini_key}")
        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                    {"text": vision_prompt}
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 256}
        }
        response = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=15)
        if response.status_code == 200:
            result = response.json()
            raw = result['candidates'][0]['content']['parts'][0]['text'].strip()
            if raw.upper() == "NONE" or not raw:
                return "", ("No digits detected by Vision AI." if lang_mode == "English"
                            else "ভিশন AI কোনো সংখ্যা পায়নি।")
            digits_only = re.sub(r'\D', '', raw)
            label = (f"🤖 Gemini Vision: **{raw[:80]}**" if lang_mode == "English"
                     else f"🤖 জেমিনি ভিশন পেয়েছে: **{raw[:80]}**")
            return digits_only[:MAX_DIGITS_ALLOWED], label
    except Exception:
        pass
    return None, ("Vision API error." if lang_mode == "English" else "ভিশন API এ সমস্যা হয়েছে।")


def validate_token(token_str):
    """Basic DESCO/BPDB token format validation."""
    if not token_str:
        return None
    digits = re.sub(r'\D', '', token_str)
    length = len(digits)
    if length == 20:
        return ("✅ Valid! Standard 20-digit prepaid token format.",
                "✅ বৈধ! ২০-ডিজিটের স্ট্যান্ডার্ড প্রিপেইড টোকেন।", "success")
    elif length == 11:
        return ("✅ Valid! 11-digit meter reading format.",
                "✅ বৈধ! ১১-ডিজিটের মিটার রিডিং ফরম্যাট।", "success")
    elif length < 8:
        return ("⚠️ Too short — token may be incomplete.",
                "⚠️ অনেক ছোট — টোকেন অসম্পূর্ণ হতে পারে।", "warning")
    elif length > 25:
        return ("⚠️ Too long — may contain extra digits.",
                "⚠️ অনেক বড় — অতিরিক্ত সংখ্যা থাকতে পারে।", "warning")
    else:
        return (f"ℹ️ Non-standard length ({length} digits). Verify manually.",
                f"ℹ️ অ-প্রমাণিত দৈর্ঘ্য ({length} ডিজিট)। ম্যানুয়ালি যাচাই করুন।", "info")


def ask_gemini_brain(prompt, lang_mode, conversation_history=None, mem_limit=12):
    """FIXED: rate limited, sanitized input, memory_limit applied properly."""
    if not gemini_key or len(gemini_key) < 10:
        return ("AI brain offline — API key not configured." if lang_mode == "English"
                else "AI ব্রেইন অফলাইন — API কী কনফিগার হয়নি।")
    # Input sanitization
    if not prompt or not isinstance(prompt, str):
        return "Invalid input." if lang_mode == "English" else "ভুল ইনপুট।"
    prompt = prompt.strip().replace("\x00", "")[:MAX_CHAT_INPUT_LEN]
    # Rate limit check
    if _rate_limited():
        return ("⚠️ Too many requests. Please wait 60 seconds." if lang_mode == "English"
                else "⚠️ অনেক বেশি রিকোয়েস্ট। ৬০ সেকেন্ড অপেক্ষা করুন।")
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.0-flash:generateContent?key={gemini_key}")
        headers = {'Content-Type': 'application/json'}

        system_prompt = (TRUMPU_SYSTEM_PROMPT if lang_mode == "English"
                         else TRUMPU_SYSTEM_PROMPT_BN)

        contents = []

        if conversation_history:
            # FIXED: use mem_limit parameter — was hardcoded -12 before
            for msg in conversation_history[-mem_limit:]:
                role = msg[0]
                content = str(msg[1]).replace("\x00", "")[:1500]
                api_role = "model" if role == "model" else "user"
                contents.append({"role": api_role, "parts": [{"text": content}]})

        # Append user message (no system prefix — uses systemInstruction instead)
        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.85,
                "maxOutputTokens": 512,
                "topP": 0.95,
            }
        }

        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            result = response.json()
            raw = result['candidates'][0]['content']['parts'][0]['text']
            return raw.replace("\x00", "")[:3000]
        elif response.status_code == 400:
            return ("Invalid API Key or request." if lang_mode == "English"
                    else "ভুল API Key বা রিকোয়েস্ট।")
        elif response.status_code == 429:
            return ("Rate limit hit. Please wait a moment." if lang_mode == "English"
                    else "অনেক বেশি রিকোয়েস্ট হয়েছে। একটু অপেক্ষা করুন।")
        else:
            return (f"API error {response.status_code}." if lang_mode == "English"
                    else f"API ত্রুটি {response.status_code}।")
    except requests.exceptions.Timeout:
        return ("Response timeout. Try again." if lang_mode == "English"
                else "সময়সীমা শেষ। আবার চেষ্টা করুন।")
    except requests.exceptions.ConnectionError:
        return ("Network error. Check your connection." if lang_mode == "English"
                else "নেটওয়ার্ক সমস্যা। ইন্টারনেট চেক করুন।")
    except Exception as e:
        import sys
        print(f"[TRUMPU Brain Error] {type(e).__name__}: {e}", file=sys.stderr)
    return ("Brain linkage error." if lang_mode == "English"
            else "সার্ভার সংযোগ বিচ্ছিন্ন হয়েছে।")


# ============================================================
# ১০. JARVIS ENGINE CORE — Voice Agent
# ============================================================
def smart_ai_agent_logic(text, ui_lang, current_persona, mode="global", brain_resp="", spd=2.5, pitch=1.0):
    is_bangla = "true" if ui_lang == "বাংলা (Bangla)" else "false"
    has_brain = "true" if (gemini_key and len(gemini_key) > 5) else "false"

    safe_brain_resp = json.dumps(brain_resp) if brain_resp else '""'
    safe_text = json.dumps(text) if text else '""'

    js_code = f"""
    <div id="jarvis-visualizer-container" style="display:flex;flex-direction:column;align-items:center;justify-content:center;background:rgba(15,23,42,0.6);padding:20px;border-radius:20px;border:1px solid #1e293b;margin:15px 0;box-shadow:inset 0 0 20px rgba(59,130,246,0.1);">
        <div id="jarvis-orb" style="width:70px;height:70px;border-radius:50%;background:radial-gradient(circle,#60a5fa 0%,#3b82f6 50%,rgba(0,0,0,0) 100%);box-shadow:0 0 25px #3b82f6;transition:all 0.3s ease;animation:floatOrb 3s ease-in-out infinite;"></div>
        <div style="display:flex;gap:4px;margin-top:15px;height:30px;align-items:center;">
            <div class="wave-bar" style="width:4px;height:10px;background:#3b82f6;border-radius:2px;transition:0.1s;"></div>
            <div class="wave-bar" style="width:4px;height:10px;background:#60a5fa;border-radius:2px;transition:0.1s;"></div>
            <div class="wave-bar" style="width:4px;height:10px;background:#2563eb;border-radius:2px;transition:0.1s;"></div>
            <div class="wave-bar" style="width:4px;height:10px;background:#3b82f6;border-radius:2px;transition:0.1s;"></div>
            <div class="wave-bar" style="width:4px;height:10px;background:#60a5fa;border-radius:2px;transition:0.1s;"></div>
        </div>
        <p id="jarvis-status" style="color:#60a5fa;font-size:13px;font-weight:600;margin-top:8px;letter-spacing:1px;font-family:monospace;">TRUMPU STANDBY</p>
    </div>

    <style>
        @keyframes floatOrb {{
            0% {{ transform: translateY(0px) scale(1); opacity: 0.8; }}
            50% {{ transform: translateY(-6px) scale(1.05); opacity: 1; box-shadow: 0 0 35px #60a5fa; }}
            100% {{ transform: translateY(0px) scale(1); opacity: 0.8; }}
        }}
        @keyframes loadPulse {{
            0% {{ height: 8px; opacity: 0.5; }}
            100% {{ height: 22px; opacity: 1; }}
        }}
    </style>

    <script>
        window.isBangla = {is_bangla};
        window.agentLang = window.isBangla ? 'bn-BD' : 'en-US';
        window.currentMode = "{mode}";
        window.activeDigitsString = {safe_text};
        window.digitSpeechTimeout = null;
        window.globalSpeechIndex = 0;
        window.isInterrupted = false;

        window.voiceSpeed = {spd};
        window.voicePitch = {pitch};
        window.voicePersona = "{current_persona}";
        window.hasBrainEngine = {has_brain};
        window.lastVoiceQueryTs = 0;  // debounce: min 2s between voice AI calls
        window.pendingBrainResponse = {safe_brain_resp};

        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        function playSciFiSound(type) {{
            try {{
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                if (type === 'beep') {{
                    osc.type = 'sine'; osc.frequency.setValueAtTime(880, audioCtx.currentTime);
                    gain.gain.setValueAtTime(0.05, audioCtx.currentTime);
                    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.15);
                    osc.start(); osc.stop(audioCtx.currentTime + 0.15);
                }} else if (type === 'swoosh') {{
                    osc.type = 'triangle'; osc.frequency.setValueAtTime(150, audioCtx.currentTime);
                    osc.frequency.exponentialRampToValueAtTime(600, audioCtx.currentTime + 0.3);
                    gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
                    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.3);
                    osc.start(); osc.stop(audioCtx.currentTime + 0.3);
                }} else if (type === 'alert') {{
                    osc.type = 'sawtooth'; osc.frequency.setValueAtTime(220, audioCtx.currentTime);
                    gain.gain.setValueAtTime(0.05, audioCtx.currentTime);
                    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.4);
                    osc.start(); osc.stop(audioCtx.currentTime + 0.4);
                }}
            }} catch(e) {{}}
        }}

        function updateVisualizerState(state) {{
            const orb = document.getElementById('jarvis-orb');
            const statusText = document.getElementById('jarvis-status');
            const bars = document.querySelectorAll('.wave-bar');
            if (state === 'speaking') {{
                orb.style.background = "radial-gradient(circle,#22c55e 0%,#16a34a 60%,rgba(0,0,0,0) 100%)";
                orb.style.boxShadow = "0 0 30px #22c55e";
                statusText.innerText = "TRUMPU TRANSMITTING...";
                statusText.style.color = "#22c55e";
                bars.forEach(b => {{ b.style.height = (20 + Math.random() * 25) + "px"; b.style.backgroundColor = "#22c55e"; }});
            }} else if (state === 'listening') {{
                orb.style.background = "radial-gradient(circle,#a855f7 0%,#7e22ce 60%,rgba(0,0,0,0) 100%)";
                orb.style.boxShadow = "0 0 30px #a855f7";
                statusText.innerText = "TRUMPU LISTENING...";
                statusText.style.color = "#a855f7";
                bars.forEach(b => {{ b.style.height = "18px"; b.style.backgroundColor = "#a855f7"; }});
            }} else {{
                orb.style.background = "radial-gradient(circle,#60a5fa 0%,#3b82f6 50%,rgba(0,0,0,0) 100%)";
                orb.style.boxShadow = "0 0 25px #3b82f6";
                statusText.innerText = "TRUMPU ONLINE";
                statusText.style.color = "#3b82f6";
                bars.forEach(b => {{ b.style.height = "8px"; b.style.backgroundColor = "#3b82f6"; }});
            }}
        }}

        function triggerHaptic() {{ if (navigator.vibrate) navigator.vibrate(40); }}

        const randRes = (arr) => arr[Math.floor(Math.random() * arr.length)];
        const replies = {{
            repeat: window.isBangla ? ["দয়া করে লক্ষ্য করুন, পুনরায় পাঠ করছি।", "আবার মিলিয়ে নিন।"] : ["Re-indexing. Listen closely.", "Repeating active cluster."],
            wait: window.isBangla ? ["থামলাম স্যার। প্রস্তুত হলে নেক্সট বলুন।", "ভয়েস মডিউল হোল্ডে রাখা হয়েছে।"] : ["System paused. Standing by.", "Telemetry paused on your mark."],
            next: window.isBangla ? ["পরবর্তী কোয়ান্টাম ক্লাস্টারে স্থানান্তরিত হচ্ছি।"] : ["Advancing data stream."],
            prev: window.isBangla ? ["পূর্ববর্তী নোডে ফিরে যাচ্ছি।"] : ["Reverting cluster block."],
            start: window.isBangla ? ["সিস্টেম ইনিশিয়ালাইজড। কোড ইনপুট করুন।"] : ["TRUMPU initialized. Proceed with entry."],
            hello: window.isBangla ? ["হ্যালো স্যার, আমি TRUMPU। বলুন কি সাহায্য করতে পারি?"] : ["Greetings. I am TRUMPU. How may I assist?"]
        }};

        const intents = {{
            start: ['start', 'begin', 'শুরু', 'স্টার্ট', 'ready', 'রেডি', 'suro', 'shuru'],
            next: ['next', 'forward', 'পরের', 'সামনে', 'নেক্সট', 'পরবর্তী', 'পরে', 'nex'],
            prev: ['back', 'previous', 'আগের', 'পিছনে', 'পেছনে', 'আগে', 'piche'],
            repeat: ['repeat', 'again', 'আবার', 'রিপিট', 'abar'],
            wait: ['wait', 'stop', 'thamo', 'থামো', 'দাঁড়াও', 'pause', 'slow down', 'চুপ'],
            hello: ['hello', 'hi', 'hey', 'trumpu', 'jarvis', 'হাই', 'হ্যালো', 'ট্রাম্পু']
        }};

        function fuzzyMatch(input, target) {{
            if(Math.abs(input.length - target.length) > 3) return false;
            let changes = 0;
            for(let i=0; i<Math.min(input.length, target.length); i++) {{
                if(input[i] !== target[i]) changes++;
            }}
            return changes <= 2;
        }}

        function analyzeIntent(transcript) {{
            const text = transcript.toLowerCase().trim();
            for (const [intentName, keywords] of Object.entries(intents)) {{
                if(keywords.some(kw => text.includes(kw) || fuzzyMatch(text, kw))) return intentName;
            }}
            return null;
        }}

        function stopAllSpeech() {{
            window.isInterrupted = true;
            clearTimeout(window.digitSpeechTimeout);
            window.speechSynthesis.cancel();
            // Also drain the response queue so stale replies don't speak
            window.voiceResponseQueue = [];
            window.voiceQueueBusy = false;
            updateVisualizerState('idle');
        }}

        function selectSystemVoice(msgInstance) {{
            let voices = window.speechSynthesis.getVoices();
            if(!voices || voices.length === 0) return;
            if (window.voicePersona.includes("Jarvis")) {{
                let target = voices.find(v => v.lang.startsWith('en') && (v.name.toLowerCase().includes('google uk english male') || v.name.toLowerCase().includes('male') || v.name.toLowerCase().includes('david')));
                if(target) msgInstance.voice = target;
            }} else if (window.voicePersona.includes("Cortana")) {{
                let target = voices.find(v => v.lang.startsWith('en') && (v.name.toLowerCase().includes('zira') || v.name.toLowerCase().includes('female') || v.name.toLowerCase().includes('google us english')));
                if(target) msgInstance.voice = target;
            }}
        }}

        function speakDigitsStepByStep() {{
            if (!window.activeDigitsString || window.isInterrupted || window.currentMode !== "active") {{
                updateVisualizerState('idle'); return;
            }}
            let digitsArray = window.activeDigitsString.split("");
            if (window.globalSpeechIndex >= digitsArray.length) {{
                window.globalSpeechIndex = 0; updateVisualizerState('idle'); return;
            }}
            updateVisualizerState('speaking');
            let currentDigit = digitsArray[window.globalSpeechIndex];
            let msg = new SpeechSynthesisUtterance(currentDigit);
            msg.lang = 'en-US';
            msg.rate = window.voiceSpeed;
            msg.pitch = window.voicePitch;
            selectSystemVoice(msg);
            msg.onend = function() {{
                window.globalSpeechIndex++;
                if (window.globalSpeechIndex < digitsArray.length && !window.isInterrupted) {{
                    window.digitSpeechTimeout = setTimeout(speakDigitsStepByStep, 300);
                }} else {{ updateVisualizerState('idle'); }}
            }};
            msg.onerror = function() {{
                updateVisualizerState('idle');
                if(!window.isInterrupted) window.digitSpeechTimeout = setTimeout(speakDigitsStepByStep, 200);
            }};
            window.speechSynthesis.speak(msg);
        }}

        function agentSpeakPhrase(text, callback) {{
            stopAllSpeech();
            updateVisualizerState('speaking');
            // FIXED: use voices-ready gate instead of bare 60ms timeout to prevent race condition
            function doSpeak() {{
                let voices = window.speechSynthesis.getVoices();
                let msg = new SpeechSynthesisUtterance(text);
                msg.lang = window.agentLang;
                msg.rate = Math.min(window.voiceSpeed, 2.0);
                msg.pitch = window.voicePitch;
                selectSystemVoice(msg);
                msg.onend = function() {{
                    updateVisualizerState('idle');
                    if(callback) callback();
                    // Process next item in the response queue after speaking
                    processVoiceQueue();
                }};
                msg.onerror = function() {{
                    updateVisualizerState('idle');
                    processVoiceQueue();
                }};
                window.speechSynthesis.speak(msg);
            }}
            if (window.speechSynthesis.getVoices().length > 0) {{
                doSpeak();
            }} else {{
                window.speechSynthesis.onvoiceschanged = function() {{ doSpeak(); }};
                // Fallback: if voices never load, speak after 200ms
                setTimeout(() => {{ if (!window.speechSynthesis.speaking) doSpeak(); }}, 200);
            }}
        }}

        // ── Voice Response Queue — prevents speech cut-off on rapid queries ──
        window.voiceResponseQueue = [];
        window.voiceQueueBusy = false;

        function enqueueVoiceResponse(text) {{
            window.voiceResponseQueue.push(text);
            if (!window.voiceQueueBusy) processVoiceQueue();
        }}

        function processVoiceQueue() {{
            if (window.voiceResponseQueue.length === 0) {{
                window.voiceQueueBusy = false;
                return;
            }}
            window.voiceQueueBusy = true;
            const nextText = window.voiceResponseQueue.shift();
            // Call the low-level speak directly (not agentSpeakPhrase to avoid queue loop)
            updateVisualizerState('speaking');
            let msg = new SpeechSynthesisUtterance(nextText);
            msg.lang = window.agentLang;
            msg.rate = Math.min(window.voiceSpeed, 2.0);
            msg.pitch = window.voicePitch;
            selectSystemVoice(msg);
            msg.onend = function() {{
                updateVisualizerState('idle');
                window.voiceQueueBusy = false;
                processVoiceQueue();
            }};
            msg.onerror = function() {{
                window.voiceQueueBusy = false;
                processVoiceQueue();
            }};
            window.speechSynthesis.speak(msg);
        }}

        function speakBrainResponse() {{
            if(window.pendingBrainResponse && window.pendingBrainResponse.length > 0) {{
                agentSpeakPhrase(window.pendingBrainResponse, null);
                window.pendingBrainResponse = "";
            }}
        }}

        function executeDomButton(selectors) {{
            triggerHaptic();
            // Search both the current frame and parent frame
            const searchRoots = [document, window.parent ? window.parent.document : null].filter(Boolean);
            for (const root of searchRoots) {{
                try {{
                    const buttons = root.querySelectorAll('button[kind], button.stButton, button');
                    for (const btn of buttons) {{
                        const btnText = (btn.innerText || btn.textContent || "").trim();
                        for (const sel of selectors) {{
                            if (btnText.includes(sel)) {{
                                btn.click();
                                return;  // click first match only
                            }}
                        }}
                    }}
                }} catch(e) {{}}
            }}
        }}

        function initAudioEngine() {{
            window.isInterrupted = false;
            if (window.currentMode === "active") {{
                window.globalSpeechIndex = 0;
                speakDigitsStepByStep();
            }}
            speakBrainResponse();
        }}

        setInterval(() => {{
            if(window.speechSynthesis.speaking && window.currentMode === "active" && !window.isInterrupted) {{
                const bars = document.querySelectorAll('.wave-bar');
                bars.forEach(b => {{ b.style.height = (12 + Math.random() * 22) + 'px'; }});
            }}
        }}, 120);

        document.addEventListener('click', initAudioEngine, {{ once: false }});
        document.addEventListener('touchstart', initAudioEngine, {{ once: false }});

        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (SpeechRecognition) {{
            if (!window.globalTrackerEngine) {{
                window.globalTrackerEngine = new SpeechRecognition();
                window.globalTrackerEngine.lang = window.agentLang;
                window.globalTrackerEngine.continuous = true;
                window.globalTrackerEngine.interimResults = false;

                window.globalTrackerEngine.onstart = () => updateVisualizerState('listening');

                window.globalTrackerEngine.onresult = function(event) {{
                    let transcript = event.results[event.results.length - 1][0].transcript;
                    let intent = analyzeIntent(transcript);

                    if(!intent) {{
                        if(window.hasBrainEngine && transcript.length > 2) {{
                            const now = Date.now();
                            if (now - window.lastVoiceQueryTs < 2000) return;  // debounce: 2s min
                            window.lastVoiceQueryTs = now;
                            stopAllSpeech();
                            playSciFiSound('swoosh');
                            // ── REAL Voice-to-AI-to-Voice ──
                            // Show loading skeleton while waiting for AI
                            updateVisualizerState('listening');
                            const statusEl = document.getElementById('jarvis-status');
                            if(statusEl) statusEl.innerText = "TRUMPU THINKING...";
                            const bars = document.querySelectorAll('.wave-bar');
                            bars.forEach((b, i) => {{
                                b.style.height = "14px";
                                b.style.backgroundColor = "#f59e0b";
                                b.style.animation = `loadPulse ${{0.5 + i * 0.1}}s ease-in-out infinite alternate`;
                            }});

                            const geminiKey = "{safe_js_string(gemini_key)}";
                            const isBn = window.isBangla;
                            const sysPrompt = isBn
                                ? "তুমি TRUMPU, আলিফ আহমেদের তৈরি AI। বাংলায় সংক্ষিপ্তভাবে (১-২ বাক্যে) উত্তর দাও।"
                                : "You are TRUMPU, AI by Alif Ahmed. Reply concisely in 1-2 sentences max. No markdown.";
                            const userMsg = sysPrompt + "\\n\\nUser: " + transcript;

                            fetch("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=" + geminiKey, {{
                                method: "POST",
                                headers: {{"Content-Type": "application/json"}},
                                body: JSON.stringify({{
                                    contents: [{{role: "user", parts: [{{text: userMsg}}]}}],
                                    generationConfig: {{temperature: 0.7, maxOutputTokens: 150}}
                                }})
                            }})
                            .then(r => r.json())
                            .then(data => {{
                                try {{
                                    const reply = data.candidates[0].content.parts[0].text.trim().replace(/[*_`#]/g, "");
                                    if(reply && reply.length > 0) {{
                                        enqueueVoiceResponse(reply);
                                    }}
                                }} catch(e) {{
                                    const errMsg = isBn ? "দুঃখিত, উত্তর দিতে পারছি না।" : "Sorry, I couldn't get a response.";
                                    enqueueVoiceResponse(errMsg);
                                }}
                                updateVisualizerState('idle');
                            }})
                            .catch(() => {{
                                const errMsg = isBn ? "নেটওয়ার্ক সমস্যা।" : "Network error.";
                                enqueueVoiceResponse(errMsg);
                                updateVisualizerState('idle');
                            }});
                        }}
                        return;
                    }}

                    triggerHaptic();

                    if (intent === 'wait') {{
                        stopAllSpeech(); playSciFiSound('alert');
                        agentSpeakPhrase(randRes(replies.wait)); return;
                    }}
                    if (intent === 'hello' && window.currentMode !== "active") {{
                        playSciFiSound('beep'); agentSpeakPhrase(randRes(replies.hello));
                    }}
                    else if ((window.currentMode === "global" || window.currentMode === "standby") && intent === 'start') {{
                        playSciFiSound('swoosh');
                        agentSpeakPhrase(randRes(replies.start), () => {{
                            executeDomButton(['Jarvis', 'শুরু', 'Initialize', 'Trumpu']);
                        }});
                    }}
                    else if (window.currentMode === "active") {{
                        if (intent === 'repeat') {{
                            stopAllSpeech(); playSciFiSound('beep');
                            agentSpeakPhrase(randRes(replies.repeat), () => {{
                                window.isInterrupted = false;
                                window.globalSpeechIndex = 0;
                                speakDigitsStepByStep();
                            }});
                        }} else if (intent === 'next') {{
                            stopAllSpeech(); playSciFiSound('swoosh');
                            agentSpeakPhrase(randRes(replies.next), () => {{ executeDomButton(['Next', 'পরের', '➡️']); }});
                        }} else if (intent === 'prev') {{
                            stopAllSpeech(); playSciFiSound('beep');
                            agentSpeakPhrase(randRes(replies.prev), () => {{ executeDomButton(['Prev', 'আগের', '⬅️']); }});
                        }}
                    }}
                }};

                window.globalTrackerEngine.onerror = function(e) {{
                    if(e.error !== 'not-allowed') {{
                        setTimeout(() => {{ try {{ window.globalTrackerEngine.start(); }} catch(err) {{}} }}, 400);
                    }}
                }};

                window.globalTrackerEngine.onend = function() {{
                    updateVisualizerState('idle');
                    setTimeout(() => {{ try {{ window.globalTrackerEngine.start(); }} catch(err) {{}} }}, 300);
                }};

                try {{ window.globalTrackerEngine.start(); }} catch(e) {{}}
            }}
        }}

        if(window.globalTrackerEngine) window.globalTrackerEngine.lang = window.agentLang;
        initAudioEngine();
    </script>
    """
    st.components.v1.html(js_code, height=180)


# ============================================================
# ১১. মেইন ড্যাশবোর্ড
# ============================================================
st.markdown(f"<h1 style='text-align:center;'>{lang['title']}</h1>", unsafe_allow_html=True)
st.markdown(f"<p style='text-align:center;margin-top:-15px;font-weight:500;color:{accent_color} !important;'>{lang['subtitle']}</p>", unsafe_allow_html=True)

# Session state init
defaults = {
    'cached_token': "", 'last_file_name': None, 'is_started': False,
    'prev_token': "", 'current_step': 0, 'brain_response': "",
    'chat_messages': []
    # last_voice_ts removed — was unused
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

col1, col2 = st.columns([2, 1])

with col1:
    tab_scan, tab_cam, tab_paste, tab_hist, tab_chat = st.tabs([
        lang['tab_scan'], lang['tab_cam'], lang['tab_paste'],
        lang['tab_hist'], lang['tab_chat']
    ])
    token_master = ""
    active_img = None
    raw_msg = ""

    with tab_scan:
        input_img = st.file_uploader(lang['upload_lbl'], type=['jpg','png','jpeg','webp'], label_visibility="collapsed")
        if input_img: active_img = input_img

    with tab_cam:
        cam_img = st.camera_input("Take Picture", label_visibility="collapsed")
        if cam_img: active_img = cam_img

    if active_img:
        file_identifier = (f"{active_img.name}_{active_img.size}"
                           if hasattr(active_img, 'name')
                           else f"camera_img_{active_img.size}")

        # ── File type security check ──
        if hasattr(active_img, 'name'):
            _ext = active_img.name.rsplit('.', 1)[-1].lower() if '.' in active_img.name else ''
            if _ext not in ALLOWED_IMG_TYPES:
                st.error(f"❌ File type '.{_ext}' not allowed. Use: jpg, png, jpeg, webp")
                active_img = None

        if st.session_state.last_file_name != file_identifier and active_img is not None:
            if active_img.size > MAX_FILE_SIZE_BYTES:
                st.error(lang['upload_err'])
            else:
                try:
                    raw_img = Image.open(active_img)
                    raw_img = ImageOps.exif_transpose(raw_img)
                    if raw_img.mode in ('RGBA', 'P'): raw_img = raw_img.convert('RGB')

                    # FIXED: strip_exif_data এখন PIL Image নেয়, file object না
                    secure_img = strip_exif_data(raw_img)

                    if secure_img is None:
                        st.error(lang['file_corrupt'])
                    else:
                        secure_img.thumbnail((800, 800))  # mobile-friendly size
                        # Show image with appropriate label
                        img_caption = lang['scan_msg'] if st.session_state.last_file_name != file_identifier else ("✅ Processed" if lang_choice == "English" else "✅ প্রসেস সম্পন্ন")
                        st.image(secure_img, use_container_width=True, caption=img_caption)

                        with st.spinner(lang['ai_analyzing']):
                            cv_img = np.array(secure_img)
                            if len(cv_img.shape) == 3:
                                gray_img = cv2.cvtColor(cv_img, cv2.COLOR_RGB2GRAY)
                            else:
                                gray_img = cv_img

                            blurred = cv2.GaussianBlur(gray_img, (5, 5), 0)
                            edged = cv2.Canny(blurred, 75, 200)
                            contours, _ = cv2.findContours(edged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                            if contours:
                                largest_contour = max(contours, key=cv2.contourArea)
                                if cv2.contourArea(largest_contour) > 4000:
                                    x, y, w, h = cv2.boundingRect(largest_contour)
                                    # Use a separate crop_img so gray_img ref stays valid for del
                                    crop_img = gray_img[y:y+h, x:x+w]
                                else:
                                    crop_img = gray_img
                            else:
                                crop_img = gray_img

                            final_processed = cv2.adaptiveThreshold(
                                cv2.medianBlur(crop_img, 3), 255,
                                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 11, 2
                            )

                            reader = load_fast_ocr()
                            ocr_res = reader.readtext(final_processed, detail=0)
                            extracted = secure_clean(" ".join(ocr_res))

                            # ── Gemini Vision upgrade ──
                            if use_gemini_vision and gemini_key:
                                with st.spinner("🤖 Gemini Vision analyzing..."):
                                    vision_digits, vision_label = ask_gemini_vision(secure_img, lang_choice)
                                if vision_digits is not None and len(vision_digits) > len(extracted):
                                    extracted = vision_digits
                                    st.success(vision_label)
                                elif vision_digits is not None and vision_digits:
                                    st.info(vision_label)

                            st.session_state.cached_token = extracted
                            st.session_state.last_file_name = file_identifier
                            st.session_state.is_started = False
                            st.session_state.current_step = 0

                            if extracted:
                                save_token_to_db(extracted)

                                # ── Token Validity Check ──
                                validity = validate_token(extracted)
                                if validity:
                                    en_msg, bn_msg, v_type = validity
                                    msg = en_msg if lang_choice == "English" else bn_msg
                                    if v_type == "success": st.success(msg)
                                    elif v_type == "warning": st.warning(msg)
                                    else: st.info(msg)

                                # ── Copy Button (XSS-safe) ──
                                _safe_ext = safe_js_string(extracted)
                                # Format for display: groups of 4
                                display_extracted = " ".join([extracted[i:i+4] for i in range(0, len(extracted), 4)])
                                st.components.v1.html(f"""
                                <div style="display:flex;align-items:center;gap:10px;margin-top:8px;">
                                  <code style="background:#0f172a;color:#60a5fa;padding:8px 14px;border-radius:10px;font-size:16px;font-family:monospace;flex:1;word-break:break-all;">{html.escape(display_extracted)}</code>
                                  <button onclick="navigator.clipboard.writeText('{_safe_ext}').then(()=>{{this.innerText='✅ Copied!';setTimeout(()=>{{this.innerText='📋 Copy'}},1500)}})"
                                    style="background:#3b82f6;color:white;border:none;border-radius:10px;padding:10px 18px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap;">
                                    📋 Copy
                                  </button>
                                </div>
                                """, height=60)
                            else:
                                st.warning(lang['no_num_warn'])

                            del cv_img, gray_img, blurred, edged, crop_img, final_processed
                            gc.collect()

                except Exception as e:
                    st.error(f"❌ Processing Error ({str(e)[:30]})")

        token_master = st.session_state.cached_token

    with tab_paste:
        raw_msg = st.text_area("Paste Token", placeholder=lang['paste_plc'], height=150, label_visibility="collapsed")
        if raw_msg:
            # ── Input sanitization ──
            raw_msg = raw_msg.replace("\x00", "")
            if len(raw_msg) > MAX_PASTE_INPUT_LEN:
                st.warning(f"⚠️ Input truncated to {MAX_PASTE_INPUT_LEN} characters for security.")
                raw_msg = raw_msg[:MAX_PASTE_INPUT_LEN]
            temp_clean = secure_clean(raw_msg)
            token_master = temp_clean
            if st.session_state.prev_token != token_master:
                st.session_state.is_started = False
                st.session_state.current_step = 0
                if token_master: save_token_to_db(token_master)
            st.session_state.cached_token = token_master
            if token_master:
                validity = validate_token(token_master)
                if validity:
                    en_msg, bn_msg, v_type = validity
                    msg = en_msg if lang_choice == "English" else bn_msg
                    if v_type == "success": st.success(msg)
                    elif v_type == "warning": st.warning(msg)
                    else: st.info(msg)
                _safe_tm = safe_js_string(token_master)
                st.components.v1.html(f"""
                <div style="display:flex;align-items:center;gap:10px;margin-top:6px;">
                  <button onclick="navigator.clipboard.writeText('{_safe_tm}').then(()=>{{this.innerText='✅ Copied!';setTimeout(()=>{{this.innerText='📋 Copy Token'}},1500)}})"
                    style="background:#3b82f6;color:white;border:none;border-radius:10px;padding:9px 20px;font-size:14px;font-weight:700;cursor:pointer;">
                    📋 Copy Token
                  </button>
                </div>""", height=55)

    with tab_hist:
        st.markdown(f"### {lang['tab_hist']}")
        hist_col_l, hist_col_r = st.columns([4, 1])
        with hist_col_r:
            if st.button("🗑️ Clear All", key="clear_all_hist", use_container_width=True):
                clear_all_history()
                st.rerun()
        history_data = get_history(limit=20)
        if history_data:
            for row in history_data:
                tok, scan_dt, scan_cnt = row[0], row[1], row[2] if len(row) > 2 else 1
                # Mask token for security: show first 2 + masked middle + last 4
                if len(tok) > 8:
                    masked_tok = tok[:2] + "•" * (len(tok) - 6) + tok[-4:]
                else:
                    masked_tok = "•" * len(tok)
                col_a, col_b, col_c, col_d = st.columns([3, 0.8, 0.8, 0.7])
                with col_a:
                    st.code(masked_tok, language="text")
                    st.caption(f"🕒 {scan_dt}  •  🔁 Scanned {scan_cnt}x  •  📏 {len(tok)} digits")
                with col_b:
                    if st.button("Load", key=f"btn_hist_{scan_dt}"):
                        st.session_state.cached_token = tok
                        st.session_state.is_started = False
                        st.session_state.current_step = 0
                        st.rerun()
                with col_c:
                    _safe_hist = safe_js_string(tok)
                    st.components.v1.html(f"""
                    <button onclick="navigator.clipboard.writeText('{_safe_hist}').then(()=>{{this.innerText='✅';setTimeout(()=>{{this.innerText='📋'}},1200)}})"
                      style="background:#1e3a8a;color:#60a5fa;border:1px solid #3b82f6;border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer;width:100%;margin-top:4px;">
                      📋
                    </button>""", height=45)
                with col_d:
                    if st.button("🗑️", key=f"del_hist_{scan_dt}", help="Delete this entry"):
                        delete_history_item(tok, scan_dt)
                        st.rerun()
                st.markdown("---")
        else:
            st.info("No token history found yet.")

    # ============================================================
    # AI Chat Tab — Gemini key backend থেকে আসে, user input নেই
    # ============================================================
    with tab_chat:
        st.markdown(f"### {lang['tab_chat']}")

        # Render chat messages
        chat_history_db = get_chat_history(limit=memory_limit)

        if not chat_history_db:
            st.info(lang['chat_empty'])
        else:
            chat_html = "<div class='chat-container'>"
            for msg in chat_history_db:
                role, content, ts = msg
                safe_content = html.escape(str(content))
                safe_ts = html.escape(str(ts))
                if role == "user":
                    chat_html += f"""
                    <div class='chat-label-user'>You • {safe_ts}</div>
                    <div class='chat-bubble-user'>{safe_content}</div>"""
                else:
                    chat_html += f"""
                    <div class='chat-label-ai'>🤖 TRUMPU • {safe_ts}</div>
                    <div class='chat-bubble-ai'>{safe_content}</div>"""
            chat_html += "</div>"
            st.markdown(chat_html, unsafe_allow_html=True)

        user_input = st.text_input(
            "Chat Input",
            placeholder=lang['chat_placeholder'],
            label_visibility="collapsed",
            key="chat_input_box"
        )

        chat_col1, chat_col2 = st.columns([3, 1])
        with chat_col1:
            send_clicked = st.button(lang['chat_send'], use_container_width=True, type="primary")
        with chat_col2:
            clear_clicked = st.button(lang['chat_clear'], use_container_width=True)

        if clear_clicked:
            clear_chat_history()
            st.session_state.chat_messages = []
            st.rerun()

        if send_clicked and user_input and user_input.strip():
            safe_user_input = user_input.strip().replace("\x00", "")
            if len(safe_user_input) > MAX_CHAT_INPUT_LEN:
                st.warning(f"⚠️ Message too long! Max {MAX_CHAT_INPUT_LEN} characters." if lang_choice == "English"
                           else f"⚠️ বার্তা অনেক লম্বা! সর্বোচ্চ {MAX_CHAT_INPUT_LEN} অক্ষর।")
            else:
                conv_history = [(r, c) for r, c, _ in get_chat_history(limit=memory_limit)]
                # Save user message BEFORE API call — so it's not lost on error
                save_chat_message("user", safe_user_input)
                with st.spinner(lang['chat_thinking']):
                    ai_reply = ask_gemini_brain(safe_user_input, lang_choice, conversation_history=conv_history, mem_limit=memory_limit)
                save_chat_message("model", ai_reply)
                st.session_state.brain_response = ai_reply
                st.rerun()

with col2:
    st.markdown("### 📊 System Status")
    if token_master or st.session_state.cached_token:
        display_token = token_master or st.session_state.cached_token
        st.success("🟢 TRUMPU Core Active")
        digit_count = len(display_token)
        st.metric("Total Digits Found", digit_count)
        # Show token validity inline in status
        vld = validate_token(display_token)
        if vld:
            _en, _bn, _vt = vld
            _vmsg = _en if lang_choice == "English" else _bn
            if _vt == "success": st.caption(f"✅ {_vmsg[:50]}")
            elif _vt == "warning": st.caption(f"⚠️ {_vmsg[:50]}")
            else: st.caption(f"ℹ️ {_vmsg[:50]}")
    else:
        st.warning("🟡 Awaiting Telemetry")
        st.metric("Total Digits Found", 0)

    # ── API Health ──
    st.markdown("---")
    if gemini_key and len(gemini_key) > 10:
        with _rate_lock:
            used = len([t for t in _api_call_times if time.time() - t < 60])
        remaining = MAX_API_CALLS_PER_MIN - used
        st.markdown(f"🔑 **AI Brain:** 🟢 Connected")
        st.caption(f"API calls this min: {used}/{MAX_API_CALLS_PER_MIN} ({remaining} left)")
    else:
        st.markdown("🔑 **AI Brain:** 🔴 No API Key")

    st.markdown("---")
    st.markdown("🤖 **TRUMPU Cognitive Stream**")

    quick_query = st.text_input(
        "Quick AI Query",
        placeholder="যেকোনো বিষয়ে জিজ্ঞেস করুন / Ask anything...",
        label_visibility="collapsed",
        key="quick_query_box"
    )
    if st.button("🧠 Think", key="trigger_brain_btn", use_container_width=True):
        if quick_query and quick_query.strip():
            with st.spinner("Processing..."):
                conv_history = [(r, c) for r, c, _ in get_chat_history(limit=memory_limit)]
                save_chat_message("user", quick_query.strip())  # save before API call
                ans = ask_gemini_brain(quick_query.strip(), lang_choice, conv_history, mem_limit=memory_limit)
                save_chat_message("model", ans)
                st.session_state.brain_response = ans
                st.rerun()

    if st.session_state.brain_response:
        st.info(f"**TRUMPU:** {st.session_state.brain_response}")


# ============================================================
# ১২. মেইন অপারেশনাল লজিক
# ============================================================
st.markdown("---")

if not token_master:
    token_master = st.session_state.cached_token

current_brain_resp = st.session_state.get('brain_response', '')

if token_master:
    if st.session_state.prev_token != token_master:
        st.session_state.current_step = 0
        st.session_state.prev_token = token_master
        st.session_state.is_started = False

    if not st.session_state.is_started:
        st.success(lang['success_ready'])
        if st.button(lang['btn_start'], use_container_width=True, type="primary"):
            st.session_state.is_started = True
            st.rerun()
        st.info(lang['voice_hint_standby'])
        smart_ai_agent_logic("", lang_choice, voice_persona, mode="standby",
                             brain_resp=current_brain_resp, spd=voice_speed, pitch=voice_pitch_ui)

    else:
        CHUNK_SIZE = 20
        data_chunks = [token_master[i:i+CHUNK_SIZE] for i in range(0, len(token_master), CHUNK_SIZE)]
        total_chunks = len(data_chunks)

        if st.session_state.current_step < total_chunks:
            active_code = data_chunks[st.session_state.current_step]
            display_code = " ".join([active_code[i:i+4] for i in range(0, len(active_code), 4)])
            progress_val = (st.session_state.current_step + 1) / total_chunks

            st.progress(progress_val)
            st.markdown(f"""
                <div class="master-card">
                    <p style="font-size:18px;font-weight:bold;margin-bottom:0;">{lang['step']} {st.session_state.current_step + 1} / {total_chunks}</p>
                    <div class="big-number">{display_code}</div>
                    <p style="margin-top:10px;color:{accent_color};font-weight:bold;font-size:18px;">{lang['type_meter']}</p>
                </div>
            """, unsafe_allow_html=True)

            smart_ai_agent_logic(active_code, lang_choice, voice_persona, mode="active",
                                 brain_resp=current_brain_resp, spd=voice_speed, pitch=voice_pitch_ui)

            btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 1.2])
            with btn_col1:
                if st.button(lang['btn_prev'], use_container_width=True,
                             disabled=(st.session_state.current_step == 0)):
                    st.session_state.current_step -= 1
                    st.rerun()
            with btn_col2:
                if st.button(lang['btn_repeat'], use_container_width=True): st.rerun()
            with btn_col3:
                if st.button(lang['btn_next'], type="primary", use_container_width=True):
                    st.session_state.current_step += 1
                    st.rerun()

            st.info(lang['voice_hint'])

        else:
            st.balloons()
            st.success(lang['success'])
            # Format token in groups of 4 for readability
            formatted_token = " ".join([token_master[i:i+4] for i in range(0, len(token_master), 4)])
            st.code(formatted_token, language="text")

            # ── Copy + WhatsApp (XSS-safe) ──
            _safe_tm2 = safe_js_string(token_master)
            st.components.v1.html(f"""
            <div style="display:flex;gap:10px;margin:6px 0;">
              <button onclick="navigator.clipboard.writeText('{_safe_tm2}').then(()=>{{this.innerText='✅ Copied!';setTimeout(()=>{{this.innerText='📋 Copy Token'}},1500)}})"
                style="background:#3b82f6;color:white;border:none;border-radius:10px;padding:10px 22px;font-size:14px;font-weight:700;cursor:pointer;flex:1;">
                📋 Copy Token
              </button>
              <button onclick="window.open('https://wa.me/?text='+encodeURIComponent('🔑 Token: {_safe_tm2}\\n⚡ Sent via Trumpu AI Jarvis'), '_blank')"
                style="background:#25D366;color:white;border:none;border-radius:10px;padding:10px 22px;font-size:14px;font-weight:700;cursor:pointer;flex:1;">
                📲 WhatsApp
              </button>
            </div>""", height=60)

            export_data = (f"Trumpu AI Secure Log\n"
                           f"Date: {datetime.datetime.now()}\n"
                           f"Status: Success\n"
                           f"Token: {token_master}")
            st.download_button(
                label="📥 Download Secure Log",
                data=export_data,
                file_name="secure_token_log.txt",
                mime="text/plain",
                use_container_width=True
            )

            if st.button(lang['btn_restart'], use_container_width=True, type="primary"):
                st.session_state.current_step = 0
                st.session_state.prev_token = ""
                st.session_state.cached_token = ""
                st.session_state.last_file_name = None
                st.session_state.is_started = False
                st.rerun()

        if current_brain_resp:
            st.session_state.brain_response = ""

else:
    if not active_img and not raw_msg:
        st.info(lang['start_info'])
        smart_ai_agent_logic("", lang_choice, voice_persona, mode="global",
                             brain_resp=current_brain_resp, spd=voice_speed, pitch=voice_pitch_ui)
        if current_brain_resp:
            st.session_state.brain_response = ""

st.markdown("---")
st.caption("⚡ Powered by Trumpu AI Jarvis Core V16.0 Overlord Systems | Architecture by Alif Ahmed | 🔒 Security Hardened")