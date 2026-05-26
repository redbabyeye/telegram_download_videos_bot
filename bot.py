import asyncio
import csv
import html
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LabeledPrice,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',')
    if x.strip().isdigit()
}
DB_PATH = os.getenv('DB_PATH', 'bot.db')
COOKIES_FILE = os.getenv('COOKIES_FILE', '').strip()
PORT = int(os.getenv('PORT', '8080'))
WORKERS = int(os.getenv('WORKERS', '2'))

ALLOWED_DOMAINS = (
    'youtube.com',
    'youtu.be',
    'instagram.com',
    'tiktok.com',
    'vm.tiktok.com',
)
URL_RE = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)

download_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
request_counter = 0

DEFAULT_SETTINGS = {
    'free_daily_limit': '3',
    'premium_daily_limit': '0',
    'free_max_download_mb': '45',
    'premium_max_download_mb': '45',
    'free_max_height': '480',
    'premium_max_height': '720',
    'ad_enabled': '0',
    'ad_text': 'Реклама: место свободно. По вопросам рекламы напишите администратору.',
    'premium_30_stars': '250',
    'broadcast_delay_ms': '80',
}


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.init()

    def init(self) -> None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_banned INTEGER NOT NULL DEFAULT 0,
                    premium_until INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS download_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    size_mb REAL,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    finished_at INTEGER
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
            ''')
            for key, value in DEFAULT_SETTINGS.items():
                cur.execute('INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)', (key, value))
            self.conn.commit()

    def upsert_user(self, user) -> None:
        now = int(time.time())
        with self.lock:
            self.conn.execute('''
                INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    last_seen=excluded.last_seen
            ''', (user.id, user.username, user.first_name, user.last_name, now, now))
            self.conn.commit()

    def get_user(self, user_id: int):
        with self.lock:
            return self.conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()

    def get_all_user_ids(self, include_banned: bool = False) -> list[int]:
        with self.lock:
            if include_banned:
                rows = self.conn.execute('SELECT user_id FROM users').fetchall()
            else:
                rows = self.conn.execute('SELECT user_id FROM users WHERE is_banned=0').fetchall()
            return [int(r['user_id']) for r in rows]

    def set_ban(self, user_id: int, banned: bool) -> None:
        now = int(time.time())
        with self.lock:
            self.conn.execute('''
                INSERT INTO users(user_id, created_at, last_seen, is_banned)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET is_banned=excluded.is_banned
            ''', (user_id, now, now, int(banned)))
            self.conn.commit()

    def set_premium(self, user_id: int, days: int) -> int:
        now = int(time.time())
        user = self.get_user(user_id)
        current_until = int(user['premium_until']) if user else 0
        base = max(current_until, now)
        premium_until = base + days * 86400
        with self.lock:
            self.conn.execute('''
                INSERT INTO users(user_id, created_at, last_seen, premium_until)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET premium_until=excluded.premium_until
            ''', (user_id, now, now, premium_until))
            self.conn.commit()
        return premium_until

    def revoke_premium(self, user_id: int) -> None:
        now = int(time.time())
        with self.lock:
            self.conn.execute('''
                INSERT INTO users(user_id, created_at, last_seen, premium_until)
                VALUES(?, ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET premium_until=0
            ''', (user_id, now, now))
            self.conn.commit()

    def is_premium(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return bool(user and int(user['premium_until']) > int(time.time()))

    def is_banned(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return bool(user and int(user['is_banned']) == 1)

    def get_setting(self, key: str, default: str | None = None) -> str:
        with self.lock:
            row = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else (default if default is not None else '')

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute('''
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''', (key, value))
            self.conn.commit()

    def all_settings(self) -> dict[str, str]:
        with self.lock:
            rows = self.conn.execute('SELECT key, value FROM settings ORDER BY key').fetchall()
        return {r['key']: r['value'] for r in rows}

    def add_download_log(self, user_id: int, url: str, status: str) -> int:
        now = int(time.time())
        with self.lock:
            cur = self.conn.execute('''
                INSERT INTO download_logs(user_id, url, status, created_at)
                VALUES(?, ?, ?, ?)
            ''', (user_id, url, status, now))
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_download_log(self, log_id: int, status: str, size_mb: float | None = None, error: str | None = None) -> None:
        with self.lock:
            self.conn.execute('''
                UPDATE download_logs
                SET status=?, size_mb=?, error=?, finished_at=?
                WHERE id=?
            ''', (status, size_mb, error, int(time.time()), log_id))
            self.conn.commit()

    def today_download_count(self, user_id: int) -> int:
        start = int(time.time()) - (int(time.time()) % 86400)
        with self.lock:
            row = self.conn.execute('''
                SELECT COUNT(*) AS c FROM download_logs
                WHERE user_id=? AND created_at>=? AND status IN ('queued', 'processing', 'success')
            ''', (user_id, start)).fetchone()
        return int(row['c'])

    def stats(self) -> dict[str, int]:
        now = int(time.time())
        day_start = now - (now % 86400)
        with self.lock:
            users = self.conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
            active_today = self.conn.execute('SELECT COUNT(*) AS c FROM users WHERE last_seen>=?', (day_start,)).fetchone()['c']
            premium = self.conn.execute('SELECT COUNT(*) AS c FROM users WHERE premium_until>?', (now,)).fetchone()['c']
            banned = self.conn.execute('SELECT COUNT(*) AS c FROM users WHERE is_banned=1').fetchone()['c']
            downloads_today = self.conn.execute('SELECT COUNT(*) AS c FROM download_logs WHERE created_at>=?', (day_start,)).fetchone()['c']
            success_today = self.conn.execute("SELECT COUNT(*) AS c FROM download_logs WHERE created_at>=? AND status='success'", (day_start,)).fetchone()['c']
        return {
            'users': int(users),
            'active_today': int(active_today),
            'premium': int(premium),
            'banned': int(banned),
            'downloads_today': int(downloads_today),
            'success_today': int(success_today),
        }

    def recent_users(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute('SELECT * FROM users ORDER BY last_seen DESC LIMIT ?', (limit,)).fetchall()

    def export_users_csv(self, path: Path) -> None:
        with self.lock:
            rows = self.conn.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['user_id', 'username', 'first_name', 'last_name', 'is_banned', 'premium_until', 'created_at', 'last_seen'])
            for r in rows:
                writer.writerow([r['user_id'], r['username'], r['first_name'], r['last_name'], r['is_banned'], r['premium_until'], r['created_at'], r['last_seen']])

    def add_payment(self, user_id: int, payload: str, currency: str, amount: int) -> None:
        with self.lock:
            self.conn.execute('''
                INSERT INTO payments(user_id, payload, currency, amount, created_at)
                VALUES(?, ?, ?, ?, ?)
            ''', (user_id, payload, currency, amount, int(time.time())))
            self.conn.commit()


db = DB(DB_PATH)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, format, *args):
        return


def start_health_server() -> None:
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def fmt_ts(ts: int) -> str:
    if not ts:
        return 'нет'
    return time.strftime('%d.%m.%Y %H:%M', time.localtime(ts))


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text or '')
    if not match:
        return None
    return match.group(0).strip()


def is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'}:
        return False
    host = (parsed.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return any(host == domain or host.endswith('.' + domain) for domain in ALLOWED_DOMAINS)


async def run_process(cmd: list[str], timeout_seconds: int = 600) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return 124, '', 'Превышено время ожидания загрузки.'
    return (
        process.returncode,
        stdout.decode('utf-8', errors='ignore'),
        stderr.decode('utf-8', errors='ignore'),
    )


async def has_audio_stream(video_path: Path) -> bool:
    if not shutil.which('ffprobe'):
        return True
    cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', str(video_path),
    ]
    code, stdout, _ = await run_process(cmd, timeout_seconds=60)
    return code == 0 and 'audio' in stdout.lower()


async def download_video(url: str, temp_dir: Path, max_download_mb: int, max_height: int) -> Path:
    if not shutil.which('ffmpeg'):
        raise RuntimeError(
            'На сервере не установлен ffmpeg. Без него yt-dlp часто скачивает видео без звука. '
            'Запускай проект через Dockerfile из архива или установи ffmpeg вручную.'
        )

    output_template = str(temp_dir / '%(extractor)s_%(id)s.%(ext)s')
    format_rule = (
        f'bv*[height<={max_height}]+ba/'
        f'bestvideo[height<={max_height}]+bestaudio/'
        f'b[height<={max_height}]/best'
    )

    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--playlist-items', '1',
        '--max-filesize', f'{max_download_mb}M',
        '-f', format_rule,
        '-S', f'hasaud,ext:mp4:m4a,res:{max_height},vcodec:h264,acodec:m4a',
        '--merge-output-format', 'mp4',
        '--remux-video', 'mp4',
        '--restrict-filenames',
        '--windows-filenames',
        '-o', output_template,
        url,
    ]

    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        cmd.extend(['--cookies', COOKIES_FILE])

    code, stdout, stderr = await run_process(cmd)
    files = [
        p for p in temp_dir.iterdir()
        if p.is_file()
        and not p.name.endswith('.part')
        and not p.name.endswith('.ytdl')
        and p.suffix.lower() in {'.mp4', '.mkv', '.webm', '.mov', '.m4v'}
    ]

    if code != 0 or not files:
        error_text = (stderr or stdout or 'Неизвестная ошибка').strip()
        raise RuntimeError(error_text[-1200:])

    video_path = max(files, key=lambda p: p.stat().st_size)
    if not await has_audio_stream(video_path):
        raise RuntimeError('Скачанный файл получился без аудиодорожки.')
    return video_path


@dataclass
class DownloadJob:
    user_id: int
    chat_id: int
    url: str
    is_premium: bool
    status_message_id: int
    log_id: int


async def download_worker(app: Application, worker_id: int) -> None:
    logger.info('Download worker %s started', worker_id)
    while True:
        priority, created, counter, job = await download_queue.get()
        try:
            db.finish_download_log(job.log_id, 'processing')
            settings = db.all_settings()
            max_mb = int(settings['premium_max_download_mb'] if job.is_premium else settings['free_max_download_mb'])
            max_height = int(settings['premium_max_height'] if job.is_premium else settings['free_max_height'])

            await app.bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                text=('⚡ Premium-загрузка началась...' if job.is_premium else '⏳ Загрузка началась...')
            )

            with tempfile.TemporaryDirectory() as tmp:
                video_path = await download_video(job.url, Path(tmp), max_download_mb=max_mb, max_height=max_height)
                size_mb = video_path.stat().st_size / (1024 * 1024)

                if size_mb > max_mb:
                    raise RuntimeError(f'Файл слишком большой: {size_mb:.1f} МБ. Лимит: {max_mb} МБ.')

                await app.bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text=f'📤 Отправляю видео: {size_mb:.1f} МБ...'
                )

                with video_path.open('rb') as f:
                    await app.bot.send_video(
                        chat_id=job.chat_id,
                        video=InputFile(f, filename=video_path.name),
                        caption='Готово ✅' + ('\n⚡ Premium: приоритетная очередь' if job.is_premium else ''),
                        supports_streaming=True,
                        read_timeout=240,
                        write_timeout=240,
                        connect_timeout=60,
                        pool_timeout=60,
                    )

                db.finish_download_log(job.log_id, 'success', size_mb=size_mb)
                await app.bot.delete_message(chat_id=job.chat_id, message_id=job.status_message_id)

                if not job.is_premium and db.get_setting('ad_enabled') == '1':
                    ad_text = db.get_setting('ad_text')
                    if ad_text.strip():
                        await app.bot.send_message(chat_id=job.chat_id, text=ad_text)

        except Exception as exc:
            error = str(exc).replace(BOT_TOKEN, '***')
            db.finish_download_log(job.log_id, 'failed', error=error[:900])
            try:
                await app.bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text=(
                        'Не получилось скачать видео.\n\n'
                        'Причины: видео приватное, нужен вход в аккаунт, нет аудиодорожки, файл слишком большой '
                        'или сервис временно блокирует загрузку.\n\n'
                        f'Ошибка:\n{error[:900]}'
                    )
                )
            except TelegramError:
                pass
        finally:
            download_queue.task_done()


async def post_init(app: Application) -> None:
    app.bot_data['workers'] = [asyncio.create_task(download_worker(app, i + 1)) for i in range(WORKERS)]


async def register_and_check(update: Update) -> bool:
    if not update.effective_user:
        return False
    db.upsert_user(update.effective_user)
    if db.is_banned(update.effective_user.id):
        if update.message:
            await update.message.reply_text('Доступ к боту ограничен.')
        return False
    return True


def main_menu(is_premium: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton('👑 Купить Premium', callback_data='buy_premium')],
        [InlineKeyboardButton('📊 Мой статус', callback_data='my_status')],
    ]
    if is_premium:
        buttons = [[InlineKeyboardButton('📊 Мой статус', callback_data='my_status')]]
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await register_and_check(update):
        return
    user_id = update.effective_user.id
    premium = db.is_premium(user_id)
    text = (
        'Привет! Пришли ссылку на публичное видео YouTube, Instagram или TikTok.\n\n'
        'Бесплатно действует дневной лимит. Premium получает приоритетную очередь, '
        'более высокое качество и отключение рекламы.\n\n'
        'Используй бот только для своего контента или роликов, на скачивание которых у тебя есть разрешение.'
    )
    if is_admin(user_id):
        text += '\n\nАдмин-панель: /panel'
    await update.message.reply_text(text, reply_markup=main_menu(premium))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await register_and_check(update):
        return
    settings = db.all_settings()
    text = (
        'Как пользоваться:\n'
        '1. Отправь ссылку на ролик.\n'
        '2. Дождись очереди и загрузки.\n'
        '3. Получи видео со звуком.\n\n'
        f'Бесплатно: {settings["free_daily_limit"]} скачиваний/день, качество до {settings["free_max_height"]}p.\n'
        f'Premium: приоритетная очередь, качество до {settings["premium_max_height"]}p.\n\n'
        'Команды: /start, /help, /premium, /buy'
    )
    await update.message.reply_text(text)


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await register_and_check(update):
        return
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    premium_until = int(user['premium_until']) if user else 0
    if premium_until > int(time.time()):
        await update.message.reply_text(f'👑 Premium активен до: {fmt_ts(premium_until)}')
    else:
        await update.message.reply_text('Premium не активен. Купить: /buy')


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await register_and_check(update):
        return
    stars = int(db.get_setting('premium_30_stars', '250'))
    payload = f'premium30:{update.effective_user.id}:{int(time.time())}'
    await update.message.reply_invoice(
        title='Premium на 30 дней',
        description='Приоритетная очередь, повышенное качество, отключение рекламы и увеличенные лимиты.',
        payload=payload,
        provider_token='',
        currency='XTR',
        prices=[LabeledPrice(label='Premium 30 дней', amount=stars)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if not query.invoice_payload.startswith('premium30:'):
        await query.answer(ok=False, error_message='Неверный платеж.')
        return
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    db.add_payment(user_id, payment.invoice_payload, payment.currency, payment.total_amount)
    premium_until = db.set_premium(user_id, 30)
    await update.message.reply_text(f'Оплата прошла ✅\nPremium активен до: {fmt_ts(premium_until)}')


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await register_and_check(update):
        return
    message = update.message
    user_id = update.effective_user.id
    url = extract_url(message.text or '')

    if not url:
        await message.reply_text('Пришли обычную ссылку на ролик.')
        return
    if not is_allowed_url(url):
        await message.reply_text('Поддерживаются только ссылки YouTube, Instagram и TikTok.')
        return

    is_prem = db.is_premium(user_id)
    settings = db.all_settings()
    daily_limit = int(settings['premium_daily_limit'] if is_prem else settings['free_daily_limit'])
    used_today = db.today_download_count(user_id)
    if daily_limit > 0 and used_today >= daily_limit:
        await message.reply_text(
            'Дневной лимит скачиваний закончился.\n'
            'Можно подождать до завтра или купить Premium: /buy'
        )
        return

    global request_counter
    request_counter += 1
    priority = 0 if is_prem else 10
    log_id = db.add_download_log(user_id, url, 'queued')
    position = download_queue.qsize() + 1
    status = await message.reply_text(
        ('⚡ Premium-заявка добавлена в приоритетную очередь.' if is_prem else '⏳ Заявка добавлена в очередь.')
        + f'\nПримерная позиция: {position}'
    )
    job = DownloadJob(
        user_id=user_id,
        chat_id=message.chat_id,
        url=url,
        is_premium=is_prem,
        status_message_id=status.message_id,
        log_id=log_id,
    )
    await download_queue.put((priority, time.monotonic(), request_counter, job))


def admin_panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📊 Статистика', callback_data='admin:stats'), InlineKeyboardButton('📋 Пользователи', callback_data='admin:users')],
        [InlineKeyboardButton('📣 Рассылка', callback_data='admin:broadcast'), InlineKeyboardButton('📢 Реклама', callback_data='admin:ad')],
        [InlineKeyboardButton('⚙️ Лимиты', callback_data='admin:limits'), InlineKeyboardButton('👑 Premium', callback_data='admin:premium')],
        [InlineKeyboardButton('🚫 Бан/разбан', callback_data='admin:banmenu'), InlineKeyboardButton('📦 Очередь', callback_data='admin:queue')],
        [InlineKeyboardButton('💾 Экспорт CSV', callback_data='admin:export')],
    ])


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text('Админ-панель:', reply_markup=admin_panel_markup())


async def stats_text() -> str:
    s = db.stats()
    return (
        '📊 <b>Статистика</b>\n\n'
        f'Пользователей: <b>{s["users"]}</b>\n'
        f'Активны сегодня: <b>{s["active_today"]}</b>\n'
        f'Premium: <b>{s["premium"]}</b>\n'
        f'Забанены: <b>{s["banned"]}</b>\n'
        f'Загрузок сегодня: <b>{s["downloads_today"]}</b>\n'
        f'Успешных сегодня: <b>{s["success_today"]}</b>\n'
        f'Очередь: <b>{download_queue.qsize()}</b>'
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text('Нет доступа.')
        return

    data = query.data
    if data == 'admin:stats':
        await query.edit_message_text(await stats_text(), parse_mode=ParseMode.HTML, reply_markup=admin_panel_markup())

    elif data == 'admin:users':
        rows = db.recent_users(15)
        lines = ['📋 <b>Последние пользователи</b>\n']
        for r in rows:
            name = html.escape(' '.join(x for x in [r['first_name'], r['last_name']] if x) or 'без имени')
            uname = '@' + html.escape(r['username']) if r['username'] else '—'
            prem = '👑' if int(r['premium_until']) > int(time.time()) else ''
            ban = '🚫' if int(r['is_banned']) else ''
            lines.append(f'{r["user_id"]} | {name} | {uname} {prem}{ban}')
        await query.edit_message_text('\n'.join(lines), parse_mode=ParseMode.HTML, reply_markup=admin_panel_markup())

    elif data == 'admin:broadcast':
        context.user_data['admin_mode'] = 'broadcast'
        await query.edit_message_text(
            '📣 Пришли следующим сообщением то, что нужно разослать.\n'
            'Можно отправить текст, фото, видео, документ — бот скопирует сообщение всем пользователям.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Отмена', callback_data='admin:cancel')]])
        )

    elif data == 'admin:ad':
        ad_enabled = db.get_setting('ad_enabled')
        ad_text = html.escape(db.get_setting('ad_text'))
        await query.edit_message_text(
            f'📢 <b>Реклама после скачивания</b>\n\nСтатус: <b>{"включена" if ad_enabled == "1" else "выключена"}</b>\n\nТекст:\n{ad_text}\n\n'
            'Команды:\n'
            '/ad_on — включить\n'
            '/ad_off — выключить\n'
            '/setad новый текст рекламы',
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_markup()
        )

    elif data == 'admin:limits':
        settings = db.all_settings()
        text = '⚙️ <b>Лимиты и настройки</b>\n\n'
        for k, v in settings.items():
            text += f'<code>{html.escape(k)}</code> = <b>{html.escape(v)}</b>\n'
        text += '\nИзменить: <code>/setlimit ключ значение</code>\nНапример: <code>/setlimit free_daily_limit 5</code>'
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_panel_markup())

    elif data == 'admin:premium':
        await query.edit_message_text(
            '👑 <b>Premium</b>\n\n'
            'Выдать: <code>/grant user_id days</code>\n'
            'Забрать: <code>/revoke user_id</code>\n'
            'Цена Stars: <code>/setlimit premium_30_stars 250</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_markup()
        )

    elif data == 'admin:banmenu':
        await query.edit_message_text(
            '🚫 <b>Бан/разбан</b>\n\n'
            'Забанить: <code>/ban user_id</code>\n'
            'Разбанить: <code>/unban user_id</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_markup()
        )

    elif data == 'admin:queue':
        await query.edit_message_text(f'📦 Сейчас в очереди: {download_queue.qsize()}', reply_markup=admin_panel_markup())

    elif data == 'admin:export':
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'users.csv'
            db.export_users_csv(path)
            with path.open('rb') as f:
                await query.message.reply_document(InputFile(f, filename='users.csv'), caption='Экспорт пользователей')
        await query.edit_message_text('CSV отправлен.', reply_markup=admin_panel_markup())

    elif data == 'admin:cancel':
        context.user_data.pop('admin_mode', None)
        context.user_data.pop('broadcast_draft', None)
        await query.edit_message_text('Отменено.', reply_markup=admin_panel_markup())

    elif data == 'admin:broadcast_confirm':
        draft = context.user_data.get('broadcast_draft')
        if not draft:
            await query.edit_message_text('Черновик рассылки не найден.', reply_markup=admin_panel_markup())
            return
        await query.edit_message_text('Рассылка запущена...')
        await do_broadcast(context, query.message.chat_id, draft['chat_id'], draft['message_id'])
        context.user_data.pop('admin_mode', None)
        context.user_data.pop('broadcast_draft', None)

    elif data == 'buy_premium':
        stars = int(db.get_setting('premium_30_stars', '250'))
        await query.message.reply_text(f'Купить Premium на 30 дней можно командой /buy. Цена: {stars} Stars.')

    elif data == 'my_status':
        user = db.get_user(user_id)
        prem = db.is_premium(user_id)
        used = db.today_download_count(user_id)
        await query.message.reply_text(
            f'Ваш ID: <code>{user_id}</code>\n'
            f'Premium: {"да, до " + fmt_ts(int(user["premium_until"])) if prem and user else "нет"}\n'
            f'Скачиваний сегодня: {used}',
            parse_mode=ParseMode.HTML
        )


async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, admin_chat_id: int, from_chat_id: int, message_id: int) -> None:
    user_ids = db.get_all_user_ids(include_banned=False)
    delay = int(db.get_setting('broadcast_delay_ms', '80')) / 1000
    sent = 0
    failed = 0
    progress = await context.bot.send_message(admin_chat_id, f'Рассылка: 0/{len(user_ids)}')
    for i, uid in enumerate(user_ids, start=1):
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            sent += 1
        except TelegramError:
            failed += 1
        if i % 20 == 0:
            try:
                await progress.edit_text(f'Рассылка: {i}/{len(user_ids)}\nОтправлено: {sent}\nОшибок: {failed}')
            except TelegramError:
                pass
        await asyncio.sleep(delay)
    await progress.edit_text(f'Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}')


async def admin_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    mode = context.user_data.get('admin_mode')
    if mode == 'broadcast':
        context.user_data['broadcast_draft'] = {
            'chat_id': update.message.chat_id,
            'message_id': update.message.message_id,
        }
        await update.message.reply_text(
            'Черновик рассылки сохранён. Запустить?',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('✅ Запустить рассылку', callback_data='admin:broadcast_confirm')],
                [InlineKeyboardButton('❌ Отмена', callback_data='admin:cancel')],
            ])
        )


async def admin_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cmd = update.message.text.split()[0].lower()
    args = context.args

    try:
        if cmd == '/grant':
            if len(args) != 2:
                await update.message.reply_text('Формат: /grant user_id days')
                return
            uid, days = int(args[0]), int(args[1])
            until = db.set_premium(uid, days)
            await update.message.reply_text(f'Premium выдан пользователю {uid} до {fmt_ts(until)}')

        elif cmd == '/revoke':
            if len(args) != 1:
                await update.message.reply_text('Формат: /revoke user_id')
                return
            uid = int(args[0])
            db.revoke_premium(uid)
            await update.message.reply_text(f'Premium снят у пользователя {uid}')

        elif cmd == '/ban':
            if len(args) != 1:
                await update.message.reply_text('Формат: /ban user_id')
                return
            uid = int(args[0])
            db.set_ban(uid, True)
            await update.message.reply_text(f'Пользователь {uid} забанен')

        elif cmd == '/unban':
            if len(args) != 1:
                await update.message.reply_text('Формат: /unban user_id')
                return
            uid = int(args[0])
            db.set_ban(uid, False)
            await update.message.reply_text(f'Пользователь {uid} разбанен')

        elif cmd == '/setlimit':
            if len(args) < 2:
                await update.message.reply_text('Формат: /setlimit key value')
                return
            key, value = args[0], ' '.join(args[1:])
            allowed = set(DEFAULT_SETTINGS.keys())
            if key not in allowed:
                await update.message.reply_text('Такого ключа нет. Открой /panel → Лимиты.')
                return
            db.set_setting(key, value)
            await update.message.reply_text(f'Настройка изменена: {key} = {value}')

        elif cmd == '/setad':
            text = update.message.text.partition(' ')[2].strip()
            if not text:
                await update.message.reply_text('Формат: /setad текст рекламы')
                return
            db.set_setting('ad_text', text)
            await update.message.reply_text('Текст рекламы обновлён.')

        elif cmd == '/ad_on':
            db.set_setting('ad_enabled', '1')
            await update.message.reply_text('Реклама включена.')

        elif cmd == '/ad_off':
            db.set_setting('ad_enabled', '0')
            await update.message.reply_text('Реклама выключена.')

    except ValueError:
        await update.message.reply_text('Ошибка в числах. Проверь user_id / days.')


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Пришли ссылку на видео. Команда /help покажет подсказку.')


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError('Не задан BOT_TOKEN. Используй .env или переменные окружения сервера.')
    if not ADMIN_IDS:
        logger.warning('ADMIN_IDS не задан. Админ-панель будет недоступна.')

    start_health_server()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('premium', premium_command))
    app.add_handler(CommandHandler('buy', buy_command))
    app.add_handler(CommandHandler('panel', panel_command))
    app.add_handler(CommandHandler(['grant', 'revoke', 'ban', 'unban', 'setlimit', 'setad', 'ad_on', 'ad_off'], admin_text_command))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(CallbackQueryHandler(admin_callback))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, admin_message_router), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link), group=1)
    app.add_handler(MessageHandler(filters.COMMAND, unknown), group=2)

    logger.info('Bot started')
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
