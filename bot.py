import asyncio
import html
import logging
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
DB_PATH = os.path.join(BASE_DIR, "db.sqlite")


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_env(ENV_PATH)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    sys.exit("Укажите BOT_TOKEN в файле .env или в переменной окружения")

PROXY = os.getenv("PROXY", "").strip() or None
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or None
COOKIES_BROWSER = os.getenv("COOKIES_BROWSER", "").strip() or None
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "0") or 0)
TG_API_SERVER = os.getenv("TG_API_SERVER", "").strip() or None
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
SUPPORT = "@DimaKacaricka14363"


def get_system_proxy() -> str | None:
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        if enabled and server:
            server = server.strip()
            if not server.startswith(("http://", "https://", "socks4://", "socks5://")):
                server = "http://" + server
            return server
    except Exception:
        pass
    return None


if not PROXY:
    PROXY = get_system_proxy()
    if PROXY:
        logging.info("system proxy detected: %s", PROXY)

PROXY_POOL_URLS = [
    "https://www.proxy-list.download/api/v1/get?type=https",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
]
proxy_pool: list[str] = []
proxy_pool_lock = threading.Lock()
last_pool_fetch = 0.0


def fetch_proxy_pool() -> None:
    global proxy_pool, last_pool_fetch
    if time.time() - last_pool_fetch < 600:
        return
    for url in PROXY_POOL_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            data = urllib.request.urlopen(req, timeout=15).read().decode()
            lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
            if lines:
                with proxy_pool_lock:
                    proxy_pool = lines
                last_pool_fetch = time.time()
                logging.info("proxy pool updated: %d proxies", len(lines))
                return
        except Exception as e:
            logging.warning("proxy pool fetch failed (%s): %s", url, e)


def next_proxy() -> str | None:
    fetch_proxy_pool()
    with proxy_pool_lock:
        return random.choice(proxy_pool) if proxy_pool else None


# Площадки, которые жёстко блокируют IP (бесплатный пул им не нужен)
STRICT_PLATFORMS = {"tiktok", "instagram"}

# Популярные локальные прокси-порты VPN-клиентов: (схема, порт)
LOCAL_PROXY_PORTS = [
    ("socks5", 1080), ("http", 7890), ("socks5", 7891),
    ("http", 10808), ("socks5", 10809), ("http", 8888),
    ("http", 8080), ("http", 8118), ("socks5", 20171),
    ("http", 2080), ("socks5", 12345), ("http", 1081),
]
local_proxy_cache = {"value": None, "checked": False}
local_proxy_lock = threading.Lock()


def get_local_proxy() -> str | None:
    if local_proxy_cache["checked"]:
        return local_proxy_cache["value"]
    with local_proxy_lock:
        if local_proxy_cache["checked"]:
            return local_proxy_cache["value"]
        for scheme, port in LOCAL_PROXY_PORTS:
            s = socket.socket()
            s.settimeout(1)
            try:
                s.connect(("127.0.0.1", port))
            except OSError:
                s.close()
                continue
            s.close()
            proxy = f"{scheme}://127.0.0.1:{port}"
            try:
                handler = urllib.request.ProxyHandler({scheme: proxy})
                opener = urllib.request.build_opener(handler)
                req = urllib.request.Request(
                    "https://tiktok.com/", headers={"User-Agent": "curl/8.0"}
                )
                with opener.open(req, timeout=6) as r:
                    r.read(64)
                local_proxy_cache["value"] = proxy
                logging.info("local VPN proxy detected: %s", proxy)
                break
            except Exception:
                continue
        local_proxy_cache["checked"] = True
    return local_proxy_cache["value"]

DAY = 86400

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8"),
    ],
)
admin_logger = logging.getLogger("admin_audit")
admin_handler = logging.FileHandler(os.path.join(LOG_DIR, "admin.log"), encoding="utf-8")
admin_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
admin_logger.addHandler(admin_handler)
admin_logger.setLevel(logging.INFO)
admin_logger.propagate = False


def audit(action: str, detail: str) -> None:
    admin_logger.info("%s | %s", action, detail)


RATE_WINDOW = 5.0
RATE_MAX = 25


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, max_per_window: int = RATE_MAX, window: float = RATE_WINDOW):
        super().__init__()
        self.max_per_window = max_per_window
        self.window = window
        self.events: dict[int, deque] = {}
        self._lock = threading.Lock()

    async def __call__(self, handler, event, data):
        uid = getattr(getattr(event, "from_user", None), "id", None)
        if uid is None or uid in ADMIN_IDS:
            return await handler(event, data)
        now = time.monotonic()
        with self._lock:
            q = self.events.setdefault(uid, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            blocked = len(q) >= self.max_per_window
            q.append(now)
        if blocked:
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Слишком часто! Подожди пару секунд.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⏳ Слишком часто! Подожди пару секунд.")
            except Exception:
                pass
            return None
        return await handler(event, data)

if TG_API_SERVER:
    bot = Bot(
        BOT_TOKEN,
        server=TelegramAPIServer.from_base(TG_API_SERVER),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
else:
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.message.outer_middleware(RateLimitMiddleware())
dp.callback_query.outer_middleware(RateLimitMiddleware())

DOWNLOAD_SEM = asyncio.Semaphore(2)
USER_DOWNLOAD_LOCKS: dict[int, asyncio.Lock] = {}

BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP_BACKUPS = 14


def backup_db() -> str | None:
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        name = f"db-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
        dest = os.path.join(BACKUP_DIR, name)
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(dest) as dst:
            src.backup(dst)
        backups = sorted(p for p in os.listdir(BACKUP_DIR) if p.startswith("db-") and p.endswith(".sqlite"))
        for old in backups[:-KEEP_BACKUPS]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass
        audit("BACKUP", f"created {name} (kept {len(backups)} total)")
        return dest
    except Exception as e:
        logging.warning("backup failed: %s", e)
        return None


async def backup_loop():
    while True:
        await asyncio.sleep(86400)
        path = await asyncio.to_thread(backup_db)
        if path and ADMIN_IDS:
            try:
                await bot.send_document(ADMIN_IDS[0], FSInputFile(path), caption="💾 Авто-бэкап базы данных")
            except Exception as e:
                logging.warning("backup send failed: %s", e)


@dp.error()
async def on_error(event: ErrorEvent):
    exc = event.exception
    logging.exception("unhandled error: %s", exc)
    text = (
        f"⚠️ <b>Ошибка бота</b>\n"
        f"<code>{html.escape(f'{type(exc).__name__}: {exc}')[:1800]}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass

# ─── Платформы и тарифы ────────────────────────────────────────────────

ALL_PLATFORMS = {
    "youtube", "tiktok", "instagram", "vk", "twitter", "reddit",
    "facebook", "pinterest", "twitch", "rutube", "likee", "coub", "streamable", "other",
}

PLATFORM_DOMAINS = {
    "youtube": ("youtube.com", "youtu.be", "music.youtube.com", "youtube-nocookie.com"),
    "tiktok": ("tiktok.com", "vt.tiktok.com"),
    "instagram": ("instagram.com"),
    "vk": ("vk.com", "m.vk.com", "vkvideo.ru"),
    "twitter": ("twitter.com", "x.com"),
    "reddit": ("reddit.com", "redd.it"),
    "facebook": ("facebook.com", "fb.watch", "m.facebook.com"),
    "pinterest": ("pinterest.com", "pinterest.ru", "pin.it"),
    "twitch": ("twitch.tv"),
    "rutube": ("rutube.ru"),
    "likee": ("likee.com"),
    "coub": ("coub.com"),
    "streamable": ("streamable.com"),
}

POPULAR_10 = "YouTube, TikTok, Instagram, VK, X/Twitter, Reddit, Facebook, Pinterest, Twitch, Rutube"
TOTAL_PLATFORMS = 1900
POPULAR_COUNT = 10

PLANS = {
    "trial": {
        "name": "🎁 Пробный период — 1 день",
        "price": 0,
        "period": 1,
        "duration_label": "1 день",
        "platforms": ALL_PLATFORMS,
        "desc": "Все платформы бесплатно на 1 день. Выдаётся один раз.",
    },
    "199": {
        "name": "199 ₽/мес",
        "price": 199,
        "period": 30,
        "duration_label": "30 дней",
        "platforms": {"youtube", "likee", "coub", "streamable", "other"},
        "desc": "YouTube + ~10 малоизвестных соцсетей (Likee, Coub, Streamable и др.).",
    },
    "299": {
        "name": "299 ₽/мес",
        "price": 299,
        "period": 30,
        "duration_label": "30 дней",
        "platforms": {"youtube", "tiktok", "likee", "coub", "streamable", "other"},
        "desc": "YouTube + TikTok, все соцсети из дешёвого тарифа и ещё ~10 (всего ~20).",
    },
    "399": {
        "name": "399 ₽/мес",
        "price": 399,
        "period": 30,
        "duration_label": "30 дней",
        "platforms": ALL_PLATFORMS,
        "desc": (
            "YouTube + TikTok + Instagram + VK, все ~20 соцсетей из среднего тарифа "
            "и ещё ~10 (всего ~30)."
        ),
    },
    "year": {
        "name": "4500 ₽/год",
        "price": 4500,
        "period": 365,
        "duration_label": "365 дней",
        "platforms": ALL_PLATFORMS,
        "desc": f"Абсолютно все соцсети (~{TOTAL_PLATFORMS}) на целый год.",
    },
}

PLAN_NAMES = {
    "trial": "Пробный (1 день)",
    "199": "199 ₽/мес",
    "299": "299 ₽/мес",
    "399": "399 ₽/мес",
    "year": "4500 ₽/год",
}

PLAN_ALIASES = {"t1": "299", "t2": "399", "t3": "year"}


def resolve_plan(code: str) -> str:
    return PLAN_ALIASES.get(code.lower(), code.lower())

# ─── База данных ───────────────────────────────────────────────────────

db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db_lock:
        con = db()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "user_id INTEGER PRIMARY KEY,"
                "plan TEXT NOT NULL,"
                "expires_at INTEGER NOT NULL,"
                "created_at INTEGER NOT NULL,"
                "username TEXT,"
                "first_name TEXT,"
                "used INTEGER DEFAULT 0,"
                "downloads INTEGER DEFAULT 0,"
                "trial_used INTEGER DEFAULT 0)"
            )
            for col, ddl in (
                ("username", "TEXT"),
                ("first_name", "TEXT"),
                ("used", "INTEGER DEFAULT 0"),
                ("downloads", "INTEGER DEFAULT 0"),
                ("trial_used", "INTEGER DEFAULT 0"),
            ):
                try:
                    con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass
            con.commit()
        finally:
            con.close()


def get_user(user_id: int) -> dict | None:
    with db_lock:
        con = db()
        try:
            row = con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def set_user(user_id: int, plan: str, expires_at: int) -> None:
    with db_lock:
        con = db()
        try:
            con.execute(
                "INSERT INTO users (user_id, plan, expires_at, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET plan = excluded.plan, expires_at = excluded.expires_at",
                (user_id, plan, expires_at, int(time.time())),
            )
            con.commit()
        finally:
            con.close()


def grant_trial(user_id: int) -> bool:
    with db_lock:
        con = db()
        try:
            row = con.execute(
                "SELECT trial_used FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row and row["trial_used"]:
                return False
            now = int(time.time())
            con.execute(
                "INSERT INTO users (user_id, plan, expires_at, created_at, trial_used) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "plan = excluded.plan, expires_at = excluded.expires_at, trial_used = 1",
                (user_id, "trial", now + DAY, now),
            )
            con.commit()
            return True
        finally:
            con.close()


def activate(user_id: int, plan: str) -> None:
    now = int(time.time())
    period = PLANS[plan]["period"] * DAY
    row = get_user(user_id)
    if row and row["plan"] == plan and row["expires_at"] > now:
        new_exp = row["expires_at"] + period
    else:
        new_exp = now + period
    set_user(user_id, plan, new_exp)


def active_plan(user_id: int) -> tuple[str, int] | None:
    row = get_user(user_id)
    if not row or row["expires_at"] <= int(time.time()):
        return None
    return row["plan"], row["expires_at"]


def platform_allowed(user_id: int, platform: str) -> bool:
    ap = active_plan(user_id)
    if not ap:
        return False
    return platform in PLANS[ap[0]]["platforms"]


def touch_user(user_id: int, username: str | None, first_name: str | None) -> None:
    with db_lock:
        con = db()
        try:
            con.execute(
                "UPDATE users SET username = COALESCE(?, username), "
                "first_name = COALESCE(?, first_name) WHERE user_id = ?",
                (username, first_name, user_id),
            )
            con.commit()
        finally:
            con.close()


def mark_used(user_id: int) -> None:
    with db_lock:
        con = db()
        try:
            con.execute(
                "UPDATE users SET used = 1, downloads = downloads + 1 WHERE user_id = ?",
                (user_id,),
            )
            con.commit()
        finally:
            con.close()


def remove_user(user_id: int) -> bool:
    with db_lock:
        con = db()
        try:
            cur = con.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def find_user(query: str) -> dict | None:
    query = query.strip().lstrip("@").lower()
    with db_lock:
        con = db()
        try:
            if query.isdigit():
                row = con.execute("SELECT * FROM users WHERE user_id = ?", (int(query),)).fetchone()
                if row:
                    return dict(row)
            row = con.execute(
                "SELECT * FROM users WHERE lower(username) = ?", (query,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def all_users() -> list[dict]:
    with db_lock:
        con = db()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM users ORDER BY created_at").fetchall()]
        finally:
            con.close()


# ─── Скачивание ────────────────────────────────────────────────────────


INVISIBLE_RE = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff\u00ad\u061c]"
)
TRAIL_RE = re.compile(r"[\s.,;:!?()\[\]{}<>«»„“”\"'`…]+$")


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for u in re.findall(r"https?://[^\s]+", text, re.IGNORECASE):
        u = INVISIBLE_RE.sub("", u)
        u = TRAIL_RE.sub("", u)
        if u and u not in urls:
            urls.append(u)
    return urls


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for key, domains in PLATFORM_DOMAINS.items():
        if isinstance(domains, str):
            domains = (domains,)
        if any(host == d or host.endswith("." + d) for d in domains):
            return key
    return "other"


def build_opts(
    outdir: str,
    is_audio: bool = False,
    proxy: str | None = None,
    platform: str = "",
) -> dict:
    base = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
    }
    effective = proxy or PROXY
    if effective:
        base["proxy"] = effective
    if COOKIES_FILE:
        base["cookiefile"] = COOKIES_FILE
    if COOKIES_BROWSER:
        base["cookiesfrombrowser"] = (COOKIES_BROWSER, None, None, None)
    if is_audio:
        return {
            **base,
            "format": "bestaudio/best",
            "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }
    if platform == "tiktok":
        fmt = "b[ext=mp4]/b"
    elif MAX_HEIGHT:
        fmt = f"bv*[vcodec^=avc][height<={MAX_HEIGHT}]+ba/b[ext=mp4][height<={MAX_HEIGHT}]/bv*+ba/b"
    else:
        fmt = "bv*[vcodec^=avc]+ba/b[ext=mp4]/bv*+ba/b"
    return {
        **base,
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
    }


def find_ffmpeg() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages"),
        "C:\\ffmpeg\\bin",
        "C:\\Program Files\\ffmpeg\\bin",
    ]
    for base in candidates:
        if not base or not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")
    return None


def find_ffprobe() -> str | None:
    path = shutil.which("ffprobe")
    if path:
        return path
    if FFMPEG_BIN_DIR:
        candidate = os.path.join(FFMPEG_BIN_DIR, "ffprobe.exe")
        if os.path.exists(candidate):
            return candidate
    return None


FFMPEG = find_ffmpeg()
FFMPEG_BIN_DIR = os.path.dirname(FFMPEG) if FFMPEG else None
FFPROBE = find_ffprobe()
if FFMPEG:
    logging.info("ffmpeg found: %s", FFMPEG)
else:
    logging.warning("ffmpeg not found — TikTok audio fix will be skipped")


def probe_audio_info(path: str) -> dict:
    if not FFPROBE:
        return {}
    try:
        r = subprocess.run(
            [
                FFPROBE, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,profile,bit_rate",
                "-of", "default=noprint_wrappers=1", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        info: dict[str, str] = {}
        for line in (r.stdout or "").splitlines():
            k, _, v = line.partition("=")
            if k:
                info[k.strip()] = v.strip()
        return info
    except Exception as e:
        logging.warning("probe failed: %s", e)
        return {}


def probe_fps(path: str) -> int:
    if not FFPROBE:
        return 30
    try:
        r = subprocess.run(
            [
                FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate",
                "-of", "default=noprint_wrappers=1", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        raw = ((r.stdout or "").partition("=")[2]).strip()
        num, _, den = raw.partition("/")
        num, den = float(num), float(den or 1)
        fps = num / den if den else 30.0
        if fps > 55:
            return 60
        if fps > 25:
            return 30
        return max(int(round(fps)), 24)
    except Exception:
        return 30


def audio_needs_fix(path: str) -> bool:
    if not path.lower().endswith(".mp4"):
        return False
    info = probe_audio_info(path)
    codec = info.get("codec_name", "")
    if codec in ("opus", "vorbis"):
        return False
    if "he" in info.get("profile", "").lower():
        return True
    try:
        bitrate = int(info.get("bit_rate") or 0)
        if 0 < bitrate < 128000:
            return True
    except ValueError:
        pass
    return False


def reencode_tiktok(path: str) -> str:
    if not FFMPEG:
        return path
    fps = probe_fps(path)
    out = path + ".fix.mp4"
    try:
        r = subprocess.run(
            [
                FFMPEG, "-y", "-i", path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(fps),
                "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k",
                "-movflags", "+faststart", out,
            ],
            capture_output=True,
            timeout=600,
        )
        if r.returncode == 0 and os.path.exists(out):
            os.replace(out, path)
            logging.info("tiktok re-encoded to CFR h264 + aac 192k (%d fps)", fps)
        else:
            logging.warning("tiktok re-encode failed: %s", r.stderr[-400:] if r.stderr else "?")
    except Exception as e:
        logging.warning("tiktok re-encode error: %s", e)
    return path


def fix_audio(path: str) -> str:
    if not FFMPEG or not audio_needs_fix(path):
        return path
    out = path + ".fix.mp4"
    try:
        r = subprocess.run(
            [
                FFMPEG, "-y", "-i", path,
                "-c:v", "copy", "-c:a", "aac", "-profile:a", "aac_low",
                "-b:a", "192k", "-movflags", "+faststart", out,
            ],
            capture_output=True,
            timeout=180,
        )
        if r.returncode == 0 and os.path.exists(out):
            os.replace(out, path)
            logging.info("audio upgraded to aac 192k")
        else:
            logging.warning("audio fix failed: %s", r.stderr[-400:] if r.stderr else "?")
    except Exception as e:
        logging.warning("audio fix error: %s", e)
    return path


def _download_once(
    url: str, is_audio: bool, proxy: str | None, platform: str = ""
) -> str:
    with tempfile.TemporaryDirectory() as outdir:
        opts = build_opts(outdir, is_audio, proxy, platform)
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist" and not info.get("entries"):
                raise ValueError("Не удалось получить видео по ссылке")
            path = ydl.prepare_filename(info)
            if is_audio:
                path = os.path.splitext(path)[0] + ".mp3"
            else:
                for suffix in (".mp4", ".webm", ".mkv", ".mov", ".m4a", ".mp3", ".m4v"):
                    if os.path.exists(path):
                        break
                    path = os.path.splitext(path)[0] + suffix
            if not os.path.exists(path):
                raise FileNotFoundError("Файл не был сохранён")
            if platform == "tiktok" and not is_audio:
                path = reencode_tiktok(path)
            elif not is_audio:
                path = fix_audio(path)
            final_dir = tempfile.mkdtemp(prefix="tg_")
            final_path = os.path.join(final_dir, os.path.basename(path))
            os.replace(path, final_path)
    return final_path


def download(url: str, is_audio: bool = False) -> str:
    platform = detect_platform(url)
    strict = platform in STRICT_PLATFORMS
    local = get_local_proxy()

    if strict:
        order = [p for p in (PROXY, local) if p]
        order.append(None)
    else:
        order = [None, PROXY, local]
        order += [next_proxy() for _ in range(2)]

    last_error: Exception | None = None
    seen: set[str] = set()
    for proxy in order:
        key = proxy or "direct"
        if key in seen:
            continue
        seen.add(key)
        try:
            path = _download_once(url, is_audio, proxy, platform)
            logging.info("download ok via %s", key)
            return path
        except DownloadError as e:
            last_error = e
            logging.warning("download attempt failed via %s: %s", key, e)
        except Exception:
            raise
    raise last_error or RuntimeError("Скачивание не удалось")


def cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
    parent = os.path.dirname(path)
    try:
        if parent and os.path.basename(parent).startswith("tg_") and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


# ─── Меню и тарифы: дизайн ─────────────────────────────────────────────

def price_text(code: str) -> str:
    price = PLANS[code]["price"]
    return "Бесплатно" if price == 0 else f"{price} ₽"


def platform_hint(platform: str) -> str:
    if platform == "tiktok":
        return (
            "\n\n💡 TikTok блокирует IP сервера. Решение: резидентный (мобильный) прокси "
            "в PROXY= в .env на сервере, после чего перезапустить бота. "
            "Обычный VPN/датацентр-прокси TikTok тоже банит."
        )
    if platform == "instagram":
        return "\n\n💡 Instagram требует вход: укажи COOKIES_BROWSER=chrome в .env (браузер должен быть закрыт)."
    return ""


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="💰 Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton(text="📋 Мой тариф", callback_data="my_tariff")],
        [InlineKeyboardButton(text="📞 Служба поддержки", url=f"https://t.me/{SUPPORT.lstrip('@')}")],
    ]
    if is_admin(user_id):
        kb.append([InlineKeyboardButton(text="⚙️ Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def tariffs_text() -> str:
    lines = ["📊 <b>Доступные тарифы:</b>\n"]
    for key in ("trial", "199", "299", "399", "year"):
        t = PLANS[key]
        lines.append(f"• {t['name']} — {price_text(key)} ({t['period']} дн.)")
    lines.append("\nВыбери тариф:")
    return "\n".join(lines)


def tariffs_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text=f"✅ {PLANS[key]['name']} — {price_text(key)}", callback_data=f"buy_{key}")]
        for key in ("trial", "199", "299", "399", "year")
    ]
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def my_tariff_text(user_id: int) -> str:
    ap = active_plan(user_id)
    if ap:
        code, exp = ap
        left = max(exp - int(time.time()), 0)
        days, secs = divmod(left, DAY)
        hours = secs // 3600
        return (
            f"📋 <b>Ваш тариф:</b> {PLAN_NAMES[code]}\n"
            f"📅 Действует до: {datetime.fromtimestamp(exp).strftime('%d.%m.%Y %H:%M')}\n"
            f"⏳ Осталось дней: {days} (≈{days * 24 + hours} ч)\n"
            f"✅ Активирован: Да"
        )
    u = get_user(user_id)
    hint = ""
    if not u or not u.get("trial_used"):
        hint = "\n🎁 У тебя есть бесплатный пробный период (1 день, все платформы)!\nАктивируй в 💰 Тарифы."
    return (
        "📋 <b>Ваш тариф:</b> —\n"
        "❌ Активной подписки нет" + hint
    )


def admin_panel_text() -> str:
    return (
        "⚙️ <b>Админ панель</b>\n\n"
        "/admin — все пользователи\n"
        "/list — все пользователи (то же)\n"
        "/search [id или @username] — поиск пользователя\n"
        "/add [id или @username] [тариф] — выдать подписку\n"
        "/remove [id или @username] — удалить пользователя"
    )


def no_access_text() -> str:
    return (
        "🚫 <b>Эта платформа недоступна на твоём тарифе.</b>\n\n"
        "Подписка не активна или платформа не входит в твой тариф.\n"
        "Открой 💰 Тарифы и выбери подходящий."
    )


# ─── Команды ───────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def on_start(message: Message):
    user_id = message.from_user.id
    touch_user(
        user_id,
        message.from_user.username,
        message.from_user.first_name,
    )
    text = (
        f"👋 Привет, {html.escape(message.from_user.first_name or '')}!\n\n"
        "🎬 Скачиваю видео БЕЗ водяных знаков:\n"
        "TikTok, Instagram, YouTube, VK, X и ещё ~1900 площадок.\n\n"
        "📌 <b>Как пользоваться:</b>\n"
        "1. Скопируй ссылку на видео (кнопка «Поделиться»)\n"
        "2. Пришли её сюда — файл придёт сразу в чат\n"
        "3. Для звука без видео: /audio + ссылка\n\n"
        "👇 Выбери действие:"
    )
    ap = active_plan(user_id)
    if ap:
        code, exp = ap
        text += (
            f"\n\n💳 Твой тариф: <b>{PLAN_NAMES[code]}</b>\n"
            f"Действует до {datetime.fromtimestamp(exp).strftime('%d.%m.%Y %H:%M')}"
        )
    await message.answer(text, reply_markup=main_menu_keyboard(user_id))


@dp.message(Command("plans"))
async def on_plans(message: Message):
    touch_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    await message.answer(tariffs_text(), reply_markup=tariffs_keyboard())


@dp.message(Command("status"))
async def on_status(message: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]]
    )
    await message.answer(my_tariff_text(message.from_user.id), reply_markup=kb)


# ─── Меню: callback-навигация ──────────────────────────────────────────

@dp.callback_query(F.data == "back")
async def on_back(call: CallbackQuery):
    await call.message.edit_text(
        "👋 Выбери действие:", reply_markup=main_menu_keyboard(call.from_user.id)
    )


@dp.callback_query(F.data == "tariffs")
async def on_tariffs(call: CallbackQuery):
    await call.message.edit_text(tariffs_text(), reply_markup=tariffs_keyboard())


@dp.callback_query(F.data.startswith("buy_"))
async def on_buy(call: CallbackQuery):
    key = call.data[4:]
    if key not in PLANS:
        await call.answer("Ошибка: тариф не найден.", show_alert=True)
        return
    t = PLANS[key]
    text = (
        f"📦 <b>{t['name']}</b>\n"
        f"⏳ Дней: {t['period']}\n"
        f"💰 Цена: {price_text(key)}\n\n"
        f"{t['desc']}\n\n"
        "<i>Нажми «Оплатить» для тестовой оплаты.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{key}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="tariffs")],
        ]
    )
    await call.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("pay_"))
async def on_pay(call: CallbackQuery):
    key = call.data[4:]
    if key not in PLANS:
        await call.answer("Ошибка: тариф не найден.", show_alert=True)
        return
    t = PLANS[key]

    if key == "trial":
        if not grant_trial(call.from_user.id):
            await call.answer(
                "❌ Пробный период уже был использован — повторная активация невозможна.",
                show_alert=True,
            )
            return
        audit("TRIAL", f"activated by @{call.from_user.username or call.from_user.first_name or 'user'}")
    else:
        activate(call.from_user.id, key)
        audit(
            "PAY",
            f"{key} activated by @{call.from_user.username or call.from_user.first_name or 'user'}",
        )

    await call.answer("✅ Оплата прошла (тестовый режим)", show_alert=True)
    await call.message.edit_text(
        f"✅ Оплата прошла успешно!\n\n"
        f"Тариф: {t['name']}\n"
        f"Дней: {t['period']}\n"
        f"Сумма: {price_text(key)}\n\n"
        "Спасибо за покупку! 🎉\n"
        "Просто пришли ссылку — и скачивай."
    )


@dp.callback_query(F.data == "my_tariff")
async def on_my_tariff(call: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]]
    )
    await call.message.edit_text(my_tariff_text(call.from_user.id), reply_markup=kb)


@dp.callback_query(F.data == "admin_panel")
async def on_admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        logging.warning("admin panel denied (admins=%s)", ADMIN_IDS)
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]]
    )
    await call.message.edit_text(admin_panel_text(), reply_markup=kb)


# ─── Админ-панель ──────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def user_line(u: dict) -> str:
    now = int(time.time())
    active = u["expires_at"] > now
    remain = u["expires_at"] - now
    days, secs = divmod(max(remain, 0), DAY)
    hours = secs // 3600
    name = html.escape(u.get("username") or u.get("first_name") or "—")
    link = f"@{name}" if u.get("username") else name
    used = "да" if u.get("used") else "нет"
    return (
        f"• ID <code>{u['user_id']}</code> | {link}\n"
        f"  Тариф: <b>{PLAN_NAMES.get(u['plan'], u['plan'])}</b> — "
        f"{'✅ оформлена' if active else '❌ истекла/нет'}\n"
        f"  До: {datetime.fromtimestamp(u['expires_at']).strftime('%d.%m.%Y %H:%M')} "
        f"(осталось {days} дн {hours} ч)\n"
        f"  Пользовался ботом: {used}, скачиваний: {u.get('downloads', 0)}"
    )


def split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


async def admin_send(message: Message, text: str):
    for part in split_for_telegram(text):
        await message.answer(part)


@dp.message(Command("admin", "list"))
async def on_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    users = all_users()
    if not users:
        await message.answer("👥 Пользователей пока нет.")
        return
    now = int(time.time())
    active = sum(1 for u in users if u["expires_at"] > now)
    header = f"👥 <b>Всего: {len(users)}</b> | с активной подпиской: <b>{active}</b>\n\n"
    await admin_send(message, header + "\n".join(user_line(u) for u in users))


@dp.message(Command("search"))
async def on_search(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /search [user_id или @username]")
        return
    u = find_user(args[1])
    if not u:
        await message.answer(f"🔍 Пользователь {args[1]} не найден.")
        return
    await message.answer(user_line(u))


@dp.message(Command("add"))
async def on_add(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "Использование: /add [user_id или @username] [тариф]\n"
            f"Тарифы: {', '.join(k for k in PLANS if k != 'trial')} "
            f"(короткие: t1=299 ₽/мес, t2=399 ₽/мес, t3=4500 ₽/год)"
        )
        return
    target = args[1].lstrip("@")
    code = resolve_plan(args[2])
    if code not in PLANS or code == "trial":
        await message.answer(
            "Неверный тариф. Доступно: "
            f"{', '.join(k for k in PLANS if k != 'trial')} "
            f"(короткие: t1=299 ₽/мес, t2=399 ₽/мес, t3=4500 ₽/год)"
        )
        return
    if target.isdigit():
        user_id = int(target)
    else:
        u = find_user(target)
        if not u:
            await message.answer(f"Пользователь @{target} не найден в базе.")
            return
        user_id = u["user_id"]
    activate(user_id, code)
    who = message.from_user.username or message.from_user.first_name or "admin"
    audit("ADD", f"by @{who}: granted {code} to @{target}")
    await message.answer(
        f"✅ Пользователю <code>{user_id}</code> выдан тариф "
        f"<b>{PLAN_NAMES[code]}</b>."
    )


@dp.message(Command("remove"))
async def on_remove(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /remove [user_id или @username]")
        return
    target = args[1].lstrip("@")
    user_id = int(target) if target.isdigit() else None
    if user_id is None:
        u = find_user(target)
        if not u:
            await message.answer(f"Пользователь @{target} не найден в базе.")
            return
        user_id = u["user_id"]
    if remove_user(user_id):
        who = message.from_user.username or message.from_user.first_name or "admin"
        audit("REMOVE", f"by @{who}: removed @{target}")
        await message.answer(f"🗑 Пользователь <code>{user_id}</code> удалён из базы.")
    else:
        await message.answer(f"Пользователь <code>{user_id}</code> не найден.")


async def try_download(message: Message, urls: list[str], is_audio: bool = False):
    allowed = [u for u in urls if platform_allowed(message.from_user.id, detect_platform(u))]
    if not allowed:
        await message.answer(no_access_text(), reply_markup=tariffs_keyboard())
        return None

    label = "⏳ Скачиваю аудио…" if is_audio else "⏳ Скачиваю, подожди немного…"
    status = await message.answer(label)
    last_err: Exception | None = None
    user_lock = USER_DOWNLOAD_LOCKS.setdefault(message.from_user.id, asyncio.Lock())
    for url in allowed:
        try:
            async with user_lock, DOWNLOAD_SEM:
                path = await asyncio.to_thread(download, url, is_audio)
            mark_used(message.from_user.id)
            return path
        except Exception as e:
            last_err = e
            logging.warning("url failed: %s", url)
    await status.edit_text(
        f"❌ Не удалось скачать: {html.escape(str(last_err))}"
        f"{platform_hint(detect_platform(allowed[0]))}"
    )
    return None


@dp.message(F.text)
async def on_text(message: Message):
    urls = extract_urls(message.text)
    if not urls:
        await message.answer("Не вижу ссылки в сообщении. Пришли ссылку на видео.")
        return

    path = await try_download(message, urls)
    if not path:
        return

    status = await message.answer("✅ Готово!")
    try:
        if path.endswith(".mp3"):
            await message.answer_document(FSInputFile(path))
        else:
            await message.answer_video(FSInputFile(path))
    except Exception as e:
        logging.warning("video send failed, sending as document: %s", e)
        try:
            await message.answer_document(FSInputFile(path))
        except Exception:
            size = os.path.getsize(path) / 1024 / 1024
            await status.edit_text(
                f"❌ Файл {size:.0f} МБ не проходит лимит Telegram (50 МБ). "
                f"Попробуй /audio для звука или другую ссылку."
            )
    finally:
        cleanup(path)


@dp.message(Command("audio"))
async def on_audio(message: Message):
    urls = extract_urls(message.text)
    if not urls:
        await message.answer("Пример: /audio https://youtube.com/watch?v=…")
        return

    path = await try_download(message, urls, is_audio=True)
    if not path:
        return

    await message.answer_audio(FSInputFile(path))
    cleanup(path)


async def main():
    init_db()
    backup_path = await asyncio.to_thread(backup_db)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                "✅ <b>Бот запущен</b>"
                + (f"\n💾 Бэкап БД: <code>{os.path.basename(backup_path)}</code>" if backup_path else ""),
            )
        except Exception as e:
            logging.warning("startup notify failed: %s", e)
    for attempt in range(10):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            break
        except TelegramNetworkError as e:
            logging.warning("net error (attempt %d/10): %s", attempt + 1, e)
            await asyncio.sleep(5)
    try:
        await asyncio.gather(dp.start_polling(bot), backup_loop())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
