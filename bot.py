#!/usr/bin/env python3
"""轻量 Telegram 视频下载机器人（适合 1 核 / 512MB 内存 / 2GB 磁盘的小 VPS）。

两种运行模式，按 .env 自动选择：
- 只填 BOT_TOKEN                 → Bot API 模式，单文件上限 50MB（自动选能放进 50MB 的画质）
- 再填 API_ID / API_HASH         → MTProto 模式（Telethon），单文件上限 2GB

每个下载任务在独立子进程里跑 yt-dlp，任务结束内存即释放；
默认同时只跑 1 个任务，其余排队；下载前按剩余磁盘空间自动计算大小上限。
"""
import asyncio
import html
import io
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import signal
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).resolve().parent
MB = 1024 * 1024


def load_env(path):
    """读取 .env（systemd 已经通过 EnvironmentFile 加载时，以环境变量为准）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env(BASE / '.env')


def env_int(name, default):
    value = os.environ.get(name, '').strip()
    return int(value) if value else default


def parse_ids(raw):
    return {int(x) for x in re.split(r'[,\s，]+', raw or '') if x.strip().lstrip('-').isdigit()}


def save_ids(key, ids):
    """把用户 ID 列表写回 .env，重启后依然有效。"""
    path = BASE / '.env'
    value = ','.join(str(i) for i in sorted(ids))
    lines = path.read_text(encoding='utf-8').splitlines() if path.is_file() else []
    for i, line in enumerate(lines):
        if line.startswith(f'{key}='):
            lines[i] = f'{key}={value}'
            break
    else:
        lines.append(f'{key}={value}')
    tmp = path.with_name('.env.tmp')
    tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def display_name(first, last, username):
    name = ' '.join(x for x in (first, last) if x) or '无名氏'
    return f'{name} (@{username})' if username else name


BOT_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
API_ID = env_int('API_ID', 0)
API_HASH = os.environ.get('API_HASH', '').strip()
ADMIN_IDS = parse_ids(os.environ.get('ADMIN_IDS', ''))
_allowed_raw = os.environ.get('ALLOWED_USERS', '').strip()
ALLOW_ALL = _allowed_raw == '*'
ALLOWED_USERS = set() if ALLOW_ALL else parse_ids(_allowed_raw)

DATA_DIR = Path(os.environ.get('DATA_DIR') or BASE / 'data')
DOWNLOAD_DIR = DATA_DIR / 'downloads'
MAX_FILE_MB = env_int('MAX_FILE_MB', 500)
MAX_HEIGHT = env_int('MAX_HEIGHT', 720)
DISK_RESERVE_MB = env_int('DISK_RESERVE_MB', 200)
MAX_CONCURRENT = max(1, env_int('MAX_CONCURRENT', 1))
MAX_QUEUE = env_int('MAX_QUEUE', 10)
PER_USER_QUEUE = env_int('PER_USER_QUEUE', 3)
DOWNLOAD_TIMEOUT = env_int('DOWNLOAD_TIMEOUT', 1800)
EDIT_INTERVAL = env_int('EDIT_INTERVAL', 4)
COOKIES_FILE = os.environ.get('COOKIES_FILE', '').strip()
PROXY = os.environ.get('PROXY', '').strip()
YTDLP_EXTRA_ARGS = shlex.split(os.environ.get('YTDLP_EXTRA_ARGS', ''))


def find_tool(env_name, name):
    path = os.environ.get(env_name, '').strip()
    if path:
        return path if os.access(path, os.X_OK) else ''
    local = BASE / 'bin' / name
    if os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name) or ''


FFMPEG = find_tool('FFMPEG_PATH', 'ffmpeg')
QJS = find_tool('QJS_PATH', 'qjs')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)  # 否则会把带 token 的 URL 打进日志
log = logging.getLogger('tgdl')

mimetypes.add_type('audio/ogg', '.opus')
mimetypes.add_type('audio/mp4', '.m4a')

URL_RE = re.compile(r'https?://[^\s<>"\'`]+', re.I)
CMD_RE = re.compile(r'^/(\w+)(?:@(\w+))?(?:\s+(.*))?$', re.S)


class UserError(Exception):
    """直接展示给用户的错误。"""


# ================================================================ 两种后端的统一接口

@dataclass
class Incoming:
    chat: object          # 回复用的会话标识（Bot API 为 chat_id，Telethon 为 InputPeer）
    msg_id: int
    sender_id: int
    is_private: bool
    text: str
    links: list = field(default_factory=list)  # 文字里隐藏的超链接
    reply_text: str = ''  # 被回复的那条消息的文字
    sender_name: str = ''


@dataclass
class MsgRef:
    chat: object
    msg_id: int


@dataclass
class Callback:
    sender_id: int
    data: str
    msg: MsgRef  # 按钮所在的消息
    raw: object


class BotApiError(Exception):
    def __init__(self, method, code, desc, retry_after=None):
        super().__init__(f'{method}: {code} {desc}')
        self.code, self.desc, self.retry_after = code, desc or '', retry_after


class ProgressFile(io.FileIO):
    """上传时边读边汇报进度。"""

    def __init__(self, path, callback):
        super().__init__(path, 'rb')
        self._cb = callback
        self._total = os.fstat(self.fileno()).st_size

    def read(self, size=-1):
        chunk = super().read(size)
        if self._cb:
            self._cb(self.tell(), self._total)
        return chunk


class BotApiBackend:
    """官方 Bot API（HTTPS），只需要 BOT_TOKEN，上传上限 50MB。"""

    name = 'Bot API'
    max_upload = 49 * MB  # 官方上限 50MB，留一点余量

    def __init__(self, token):
        import httpx
        self.httpx = httpx
        self.base = f'https://api.telegram.org/bot{token}/'
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
        self._tasks = set()

    async def call(self, method, payload=None, files=None, timeout=None):
        payload = {k: v for k, v in (payload or {}).items() if v is not None}
        kw = {'timeout': timeout} if timeout else {}
        if files:
            data = {k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items()}
            r = await self.http.post(self.base + method, data=data, files=files, **kw)
        else:
            r = await self.http.post(self.base + method, json=payload, **kw)
        try:
            res = r.json()
        except ValueError:
            raise BotApiError(method, r.status_code, r.text[:200]) from None
        if not res.get('ok'):
            raise BotApiError(
                method, res.get('error_code'), res.get('description'),
                (res.get('parameters') or {}).get('retry_after'),
            )
        return res['result']

    @staticmethod
    def _markup(buttons):
        if not buttons:
            return None
        return {'inline_keyboard': [[{'text': t, 'callback_data': d} for t, d in buttons]]}

    @staticmethod
    def _reply(msg_id):
        return {'message_id': msg_id, 'allow_sending_without_reply': True} if msg_id else None

    async def start(self):
        me = await self.call('getMe')
        await self.call('deleteWebhook')  # 设置过 webhook 的话 getUpdates 会报错
        return me.get('username') or ''

    async def set_commands(self, commands):
        await self.call('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in commands]})

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or not t.exception() or log.error(
            '处理消息出错', exc_info=t.exception()))

    @staticmethod
    def _incoming(m):
        sender = m.get('from')
        if not sender:
            return None
        entities = m.get('entities') or m.get('caption_entities') or []
        reply = m.get('reply_to_message') or {}
        return Incoming(
            chat=m['chat']['id'],
            msg_id=m['message_id'],
            sender_id=sender['id'],
            is_private=m['chat'].get('type') == 'private',
            text=m.get('text') or m.get('caption') or '',
            links=[e['url'] for e in entities if e.get('type') == 'text_link' and e.get('url')],
            reply_text=reply.get('text') or reply.get('caption') or '',
            sender_name=display_name(sender.get('first_name'), sender.get('last_name'), sender.get('username')),
        )

    async def run(self, on_message, on_callback):
        offset = 0
        while True:
            try:
                updates = await self.call('getUpdates', {
                    'offset': offset, 'timeout': 50,
                    'allowed_updates': ['message', 'callback_query'],
                }, timeout=70)
            except BotApiError as e:
                if e.code == 401:
                    raise
                log.warning('getUpdates 失败：%s', e)
                await asyncio.sleep(e.retry_after or 5)
                continue
            except self.httpx.HTTPError as e:
                log.warning('getUpdates 网络错误：%s', type(e).__name__)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u['update_id'] + 1
                if 'message' in u:
                    inc = self._incoming(u['message'])
                    if inc:
                        self._spawn(on_message(inc))
                elif 'callback_query' in u:
                    q = u['callback_query']
                    m = q.get('message') or {}
                    ref = MsgRef((m.get('chat') or {}).get('id'), m.get('message_id'))
                    self._spawn(on_callback(Callback(q['from']['id'], q.get('data') or '', ref, q)))

    async def send_text(self, chat, text, reply_to=None, buttons=None):
        m = await self.call('sendMessage', {
            'chat_id': chat, 'text': text, 'parse_mode': 'HTML',
            'link_preview_options': {'is_disabled': True},
            'reply_parameters': self._reply(reply_to),
            'reply_markup': self._markup(buttons),
        })
        return MsgRef(chat, m['message_id'])

    async def edit_text(self, ref, text, buttons=None):
        try:
            await self.call('editMessageText', {
                'chat_id': ref.chat, 'message_id': ref.msg_id, 'text': text, 'parse_mode': 'HTML',
                'link_preview_options': {'is_disabled': True},
                'reply_markup': self._markup(buttons),
            })
        except BotApiError as e:
            if 'not modified' not in e.desc:
                raise

    async def delete(self, ref):
        await self.call('deleteMessage', {'chat_id': ref.chat, 'message_id': ref.msg_id})

    async def answer_callback(self, cb, text, alert=False):
        await self.call('answerCallbackQuery', {
            'callback_query_id': cb.raw['id'], 'text': text, 'show_alert': alert,
        })

    async def send_media(self, chat, reply_to, path, *, kind, caption, duration=0, width=0,
                         height=0, title='', performer='', thumb=None, progress=None):
        method, field_name = {
            'video': ('sendVideo', 'video'),
            'audio': ('sendAudio', 'audio'),
            'document': ('sendDocument', 'document'),
        }[kind]
        payload = {
            'chat_id': chat, 'caption': caption, 'parse_mode': 'HTML',
            'reply_parameters': self._reply(reply_to),
        }
        if kind == 'video':
            payload.update(duration=int(duration) or None, width=width or None,
                           height=height or None, supports_streaming=True)
        elif kind == 'audio':
            payload.update(duration=int(duration) or None, title=title[:64] or None,
                           performer=performer[:64] or None)
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        f = ProgressFile(path, progress)
        files = {field_name: (path.name, f, mime)}
        th = None
        if thumb:
            th = open(thumb, 'rb')
            files['thumb'] = ('thumb.jpg', th, 'image/jpeg')
            payload['thumbnail'] = 'attach://thumb'
        try:
            await self.call(method, payload, files=files, timeout=self.httpx.Timeout(300, connect=20))
        finally:
            f.close()
            if th:
                th.close()


class TelethonBackend:
    """MTProto（Telethon），需要 API_ID / API_HASH，上传上限 2GB。"""

    name = 'MTProto'
    max_upload = 2000 * MB

    def __init__(self, token, api_id, api_hash):
        from telethon import Button, TelegramClient, errors, events, functions, types
        self.Button, self.errors, self.events, self.functions, self.types = Button, errors, events, functions, types
        self.token = token
        self.client = TelegramClient(str(DATA_DIR / 'bot'), api_id, api_hash)
        self.client.parse_mode = 'html'

    async def start(self):
        await self.client.start(bot_token=self.token)
        me = await self.client.get_me()
        return me.username or ''

    async def set_commands(self, commands):
        t = self.types
        await self.client(self.functions.bots.SetBotCommandsRequest(
            scope=t.BotCommandScopeDefault(), lang_code='',
            commands=[t.BotCommand(c, d) for c, d in commands],
        ))

    async def run(self, on_message, on_callback):
        types = self.types

        async def msg_handler(event):
            text = event.raw_text or ''
            reply_text = ''
            if event.is_reply and text.startswith('/'):
                r = await event.get_reply_message()
                reply_text = (r.raw_text or '') if r else ''
            sender = await event.get_sender()
            await on_message(Incoming(
                chat=await event.get_input_chat(),
                msg_id=event.id,
                sender_id=event.sender_id,
                is_private=event.is_private,
                text=text,
                links=[e.url for e in (event.message.entities or [])
                       if isinstance(e, types.MessageEntityTextUrl)],
                reply_text=reply_text,
                sender_name=display_name(
                    getattr(sender, 'first_name', None), getattr(sender, 'last_name', None),
                    getattr(sender, 'username', None),
                ),
            ))

        async def cb_handler(event):
            ref = MsgRef(await event.get_input_chat(), event.message_id)
            await on_callback(Callback(event.sender_id, event.data.decode(errors='ignore'), ref, event))

        self.client.add_event_handler(msg_handler, self.events.NewMessage(incoming=True))
        self.client.add_event_handler(cb_handler, self.events.CallbackQuery())
        await self.client.run_until_disconnected()

    def _buttons(self, buttons):
        return [[self.Button.inline(t, data=d.encode()) for t, d in buttons]] if buttons else None

    async def send_text(self, chat, text, reply_to=None, buttons=None):
        m = await self.client.send_message(
            chat, text, reply_to=reply_to, buttons=self._buttons(buttons), link_preview=False,
        )
        return MsgRef(chat, m.id)

    async def edit_text(self, ref, text, buttons=None):
        try:
            await self.client.edit_message(
                ref.chat, ref.msg_id, text, buttons=self._buttons(buttons), link_preview=False,
            )
        except self.errors.MessageNotModifiedError:
            pass

    async def delete(self, ref):
        await self.client.delete_messages(ref.chat, [ref.msg_id])

    async def answer_callback(self, cb, text, alert=False):
        await cb.raw.answer(text, alert=alert)

    async def send_media(self, chat, reply_to, path, *, kind, caption, duration=0, width=0,
                         height=0, title='', performer='', thumb=None, progress=None):
        t = self.types
        attributes = []
        if kind == 'video':
            attributes.append(t.DocumentAttributeVideo(
                duration=int(duration), w=width or 1, h=height or 1, supports_streaming=True,
            ))
        elif kind == 'audio':
            attributes.append(t.DocumentAttributeAudio(
                duration=int(duration), voice=False,
                title=title[:64] or None, performer=performer[:64] or None,
            ))
        await self.client.send_file(
            chat, str(path),
            caption=caption,
            reply_to=reply_to,
            attributes=attributes,
            thumb=thumb,
            force_document=kind == 'document',
            supports_streaming=kind == 'video',
            progress_callback=progress,
        )


# ================================================================ 任务

class Job:
    def __init__(self, user_id, chat, reply_to, url, mode):
        self.id = uuid.uuid4().hex[:10]
        self.user_id = user_id
        self.chat = chat
        self.reply_to = reply_to
        self.url = url
        self.mode = mode  # 'video' | 'audio'
        self.msg = None
        self.task = None
        self.proc = None
        self.dir = DOWNLOAD_DIR / self.id
        self.started = False
        self.status = ''
        self.shown = ''


backend = None  # 在 main() 里按配置创建
bot_username = ''
sem = asyncio.Semaphore(MAX_CONCURRENT)
jobs = {}  # id -> Job


def is_admin(uid):
    return uid in ADMIN_IDS


def is_allowed(uid):
    return ALLOW_ALL or uid in ADMIN_IDS or uid in ALLOWED_USERS


def fmt_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024


def fmt_eta(sec):
    if sec is None:
        return '--:--'
    sec = int(sec)
    return f'{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}' if sec >= 3600 else f'{sec // 60:02d}:{sec % 60:02d}'


def bar(pct, width=12):
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    return '█' * filled + '░' * (width - filled)


def cancel_button(job):
    return [('❌ 取消', f'c:{job.id}')]


def size_limit():
    """按剩余磁盘计算本次下载允许的最大文件大小。合并音视频时需要约 2 倍空间。"""
    free = shutil.disk_usage(DOWNLOAD_DIR).free
    budget = free - DISK_RESERVE_MB * MB
    factor = 2 if FFMPEG else 1
    return int(min(MAX_FILE_MB * MB, backend.max_upload, budget / factor / MAX_CONCURRENT))


def ytdlp_args(mode):
    args = [
        '--no-playlist', '--playlist-items', '1',
        '--no-mtime', '--abort-on-error',
        '--socket-timeout', '30', '--retries', '5', '--fragment-retries', '5',
        '--cache-dir', str(DATA_DIR / 'cache'),
        '-o', '%(title).80B [%(id).30B].%(ext)s',
    ]
    if FFMPEG:
        args += ['--ffmpeg-location', FFMPEG]
    if QJS:
        args += ['--js-runtimes', f'quickjs:{QJS}']
    if COOKIES_FILE:
        args += ['--cookies', COOKIES_FILE]
    if PROXY:
        args += ['--proxy', PROXY]
    if mode == 'audio':
        args += ['-f', 'ba[ext=m4a]/ba[acodec^=mp4a]/ba/b']
        if FFMPEG:
            args += ['-x']  # 从视频里提取音轨（能直接拷贝就不转码，不费 CPU）
    else:
        # 优先 H.264 + AAC，Telegram 各客户端都能直接播放；分辨率不超过 MAX_HEIGHT
        args += ['-S', f'vcodec:h264,res:{MAX_HEIGHT},acodec:m4a']
        if FFMPEG:
            args += [
                '-f', 'bv*+ba/b', '--merge-output-format', 'mp4',
                '--ppa', 'Merger+ffmpeg_o:-movflags +faststart',
            ]
        else:
            args += ['-f', 'b']  # 没有 ffmpeg 只能下载音视频一体的格式
    return args + YTDLP_EXTRA_ARGS


async def safe_edit(ref, text, buttons=None):
    if ref is None:
        return
    try:
        await backend.edit_text(ref, text, buttons=buttons)
    except Exception as e:  # noqa: BLE001  编辑失败（被限流、消息被删）不影响任务
        log.warning('编辑消息失败：%s', e)


async def status_ticker(job):
    """后台定时把 job.status 刷到消息上，避免频繁编辑被限流，也不阻塞下载。"""
    while True:
        await asyncio.sleep(EDIT_INTERVAL)
        if job.status and job.status != job.shown:
            job.shown = job.status
            await safe_edit(job.msg, job.status, buttons=cancel_button(job))


def kill_proc(proc):
    if proc and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # 连同 ffmpeg / qjs 子进程一起杀掉
        except ProcessLookupError:
            pass


async def run_cmd(*cmd, timeout=120):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode, out.decode(errors='replace')


async def probe(path):
    """没有 ffprobe，用 ffmpeg -i 的输出解析时长和分辨率。"""
    duration = width = height = 0
    if not FFMPEG:
        return duration, width, height
    try:
        _, out = await run_cmd(FFMPEG, '-hide_banner', '-i', str(path), timeout=30)
    except asyncio.TimeoutError:
        return duration, width, height
    m = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', out)
    if m:
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r'Video: .*?, (\d{2,5})x(\d{2,5})', out)
    if m:
        width, height = int(m[1]), int(m[2])
    return duration, width, height


async def make_thumb(video, duration, out):
    if not FFMPEG:
        return None
    ss = min(max(duration * 0.1, 0), 10) if duration else 1
    try:
        await run_cmd(
            FFMPEG, '-y', '-v', 'error', '-ss', f'{ss:.2f}', '-i', str(video),
            '-frames:v', '1', '-vf', 'scale=320:320:force_original_aspect_ratio=decrease',
            '-q:v', '6', str(out), timeout=60,
        )
    except asyncio.TimeoutError:
        return None
    if out.is_file() and 0 < out.stat().st_size <= 200 * 1024:
        return str(out)
    return None


async def download(job, limit):
    cmd = [
        sys.executable, str(BASE / 'worker.py'), job.url, str(job.dir), str(limit), job.mode,
        *ytdlp_args(job.mode),
    ]
    job.proc = proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=1 * MB,
    )
    stderr_tail = deque(maxlen=15)
    result, error, title = None, None, ''

    async def read_stderr():
        async for raw in proc.stderr:
            line = raw.decode(errors='replace').rstrip()
            if line:
                stderr_tail.append(line)
                log.info('[%s] %s', job.id, line)

    async def read_stdout():
        nonlocal result, error, title
        async for raw in proc.stdout:
            line = raw.decode(errors='replace').strip()
            if not line.startswith('@@TGDL '):
                continue
            try:
                msg = json.loads(line[7:])
            except ValueError:
                continue
            kind = msg.get('t')
            if kind == 'info':
                title = msg.get('title') or ''
                quality = f' {msg["height"]}p' if msg.get('height') and job.mode == 'video' else ''
                est = f'（约 {fmt_size(msg["est"])}）' if msg.get('est') else ''
                job.status = f'📥 开始下载{quality}{est}\n{html.escape(title[:100])}'
            elif kind == 'progress':
                got, total = msg.get('got') or 0, msg.get('total') or 0
                pct = got * 100 / total if total else 0
                part = msg.get('part', '')
                head = f'📥 下载{part}中 {bar(pct)} {pct:.0f}%' if total else f'📥 下载{part}中…'
                job.status = (
                    f'{head}\n{fmt_size(got)} / {fmt_size(total) if total else "?"}'
                    f' · {fmt_size(msg.get("speed"))}/s · 剩余 {fmt_eta(msg.get("eta"))}\n'
                    f'{html.escape(title[:100])}'
                )
            elif kind == 'done':
                result = msg
            elif kind == 'error':
                error = msg.get('msg') or '下载失败'

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
            DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise UserError(f'下载超时（超过 {DOWNLOAD_TIMEOUT // 60} 分钟）') from None
    finally:
        kill_proc(proc)

    if result:
        return result
    if error:
        raise UserError(error)
    if proc.returncode in (-9, 137):
        raise UserError('下载进程被系统杀掉（多半是内存不足）')
    tail = '\n'.join(stderr_tail)[-400:]
    raise UserError(f'下载失败（退出码 {proc.returncode}）\n{tail}')


async def upload(job, result):
    path = Path(result['file'])
    size = path.stat().st_size
    if size > backend.max_upload:
        raise UserError(f'文件 {fmt_size(size)} 超过上传上限 {fmt_size(backend.max_upload)}')

    title = result.get('title') or path.stem
    url = result.get('url') or job.url
    caption = f'<b>{html.escape(title[:200])}</b>\n<a href="{html.escape(url, quote=True)}">原链接</a>'

    duration, width, height = result.get('duration') or 0, result.get('width') or 0, result.get('height') or 0
    if not (duration and (job.mode == 'audio' or (width and height))):
        p_dur, p_w, p_h = await probe(path)
        duration, width, height = duration or p_dur, width or p_w, height or p_h

    thumb = None
    if job.mode == 'audio':
        kind = 'audio'
    elif path.suffix.lower() in ('.mp4', '.mov', '.m4v'):
        kind = 'video'
        thumb = await make_thumb(path, duration, job.dir / 'thumb.jpg')
    else:
        kind = 'document'  # mkv/webm 等格式 Telegram 不一定能直接播，按文件发送

    last = [0.0]

    def on_progress(sent, total):
        now = time.monotonic()
        if now - last[0] < 1 and sent < total:
            return
        last[0] = now
        pct = sent * 100 / total if total else 0
        job.status = (
            f'📤 上传中 {bar(pct)} {pct:.0f}%\n{fmt_size(sent)} / {fmt_size(total)}\n'
            f'{html.escape(title[:100])}'
        )

    job.status = f'📤 准备上传 {fmt_size(size)}\n{html.escape(title[:100])}'
    await backend.send_media(
        job.chat, job.reply_to, path,
        kind=kind, caption=caption, duration=duration, width=width, height=height,
        title=title, performer=result.get('uploader') or '', thumb=thumb, progress=on_progress,
    )


async def run_job(job):
    ticker = None
    try:
        async with sem:
            job.started = True
            limit = size_limit()
            if limit < 10 * MB:
                raise UserError(
                    f'磁盘空间不足（剩余 {fmt_size(shutil.disk_usage(DOWNLOAD_DIR).free)}），请稍后再试'
                )
            job.dir.mkdir(parents=True, exist_ok=True)
            await safe_edit(job.msg, f'🔍 正在解析链接…\n本次大小上限 {fmt_size(limit)}', buttons=cancel_button(job))
            ticker = asyncio.create_task(status_ticker(job))
            result = await download(job, limit)
            await upload(job, result)
            ticker.cancel()
            ticker = None
            try:
                await backend.delete(job.msg)
            except Exception:  # noqa: BLE001
                await safe_edit(job.msg, '✅ 完成')
            log.info('任务 %s 完成: %s', job.id, job.url)
    except asyncio.CancelledError:
        await safe_edit(job.msg, '❌ 已取消')
    except UserError as e:
        await safe_edit(job.msg, f'❌ {html.escape(str(e))}')
        log.info('任务 %s 失败: %s', job.id, e)
    except Exception as e:  # noqa: BLE001
        log.exception('任务 %s 出错', job.id)
        await safe_edit(job.msg, f'❌ 出错了：{html.escape(type(e).__name__)}: {html.escape(str(e)[:300])}')
    finally:
        if ticker:
            ticker.cancel()
        kill_proc(job.proc)
        shutil.rmtree(job.dir, ignore_errors=True)
        jobs.pop(job.id, None)


async def enqueue(inc, url, mode):
    uid = inc.sender_id
    if len(jobs) >= MAX_QUEUE:
        await backend.send_text(inc.chat, '⏳ 当前任务太多，请稍后再试', reply_to=inc.msg_id)
        return
    if not is_admin(uid) and sum(j.user_id == uid for j in jobs.values()) >= PER_USER_QUEUE:
        await backend.send_text(
            inc.chat, f'⏳ 你已经有 {PER_USER_QUEUE} 个任务在排队了，等它们完成再发吧', reply_to=inc.msg_id,
        )
        return
    job = Job(uid, inc.chat, inc.msg_id, url, mode)
    ahead = max(0, len(jobs) - MAX_CONCURRENT + 1)
    tip = f'⏳ 已加入队列，前面还有 {ahead} 个任务' if ahead else '⏳ 准备开始…'
    job.msg = await backend.send_text(inc.chat, tip, reply_to=inc.msg_id, buttons=cancel_button(job))
    job.task = asyncio.create_task(run_job(job))
    jobs[job.id] = job
    log.info('用户 %s 提交任务 %s [%s] %s', uid, job.id, mode, url)


# ================================================================ 命令

COMMANDS = [
    ('start', '使用说明'),
    ('audio', '只下载音频'),
    ('dl', '下载视频（群组用）'),
    ('cancel', '取消我的任务'),
    ('id', '查看我的用户 ID'),
]


def help_text(uid):
    text = (
        '🎬 <b>视频下载机器人</b>\n\n'
        '直接发送视频链接即可下载（YouTube、B站、X/Twitter、TikTok、Instagram 等 yt-dlp 支持的网站）。\n'
        f'单个文件上限 {fmt_size(backend.max_upload)}，超出时会自动降低画质。\n\n'
        '/audio 链接 — 只下载音频\n'
        '/dl 链接 — 下载视频（群组里用）\n'
        '/cancel — 取消你的所有任务\n'
        '/id — 查看你的用户 ID\n'
    )
    if is_admin(uid):
        text += (
            '\n<b>管理员</b>\n'
            '/allow ID — 允许用户使用（陌生人发消息时也会收到一键允许的按钮）\n'
            '/remove ID — 移除用户 · /users — 用户列表\n'
            '/status — 运行状态 · /update — 更新 yt-dlp'
        )
    return text


INSTALL_CMD = 'bash &lt;(curl -fsSL https://raw.githubusercontent.com/zhushili/tg-video-bot/main/install.sh) admin'
access_requests = {}  # uid -> 上次通知管理员的时间，防止刷屏


async def request_access(inc):
    """陌生人发消息：告诉他 ID，并给管理员发一条带「允许」按钮的通知。"""
    uid = inc.sender_id

    async def reply(t):
        await backend.send_text(inc.chat, t, reply_to=inc.msg_id)

    if not ADMIN_IDS:
        await reply(
            f'⛔ 你没有使用权限。你的用户 ID：<code>{uid}</code>\n\n'
            f'如果你是机器人的主人，在 VPS 上运行下面的命令，按提示操作即可成为管理员：\n<code>{INSTALL_CMD}</code>'
        )
        return
    now = time.monotonic()
    if now - access_requests.get(uid, -1e9) < 3600:
        await reply('⛔ 已经通知过管理员了，批准后会告诉你。')
        return
    access_requests[uid] = now
    await reply(f'⛔ 你还没有使用权限（ID：<code>{uid}</code>），已通知管理员，批准后会告诉你。')
    for admin in ADMIN_IDS:
        try:
            await backend.send_text(
                admin,
                f'👤 <b>{html.escape(inc.sender_name)}</b>（ID <code>{uid}</code>）请求使用机器人',
                buttons=[('✅ 允许', f'a:{uid}'), ('🚫 忽略', f'x:{uid}')],
            )
        except Exception as e:  # noqa: BLE001
            log.warning('通知管理员 %s 失败：%s', admin, e)


async def manage_users(cmd, arg, reply):
    if ALLOW_ALL:
        await reply('当前 ALLOWED_USERS=*，所有人都能使用，不需要单独添加。')
        return
    if cmd == 'users':
        admins = ', '.join(f'<code>{i}</code>' for i in sorted(ADMIN_IDS)) or '（无）'
        users = ', '.join(f'<code>{i}</code>' for i in sorted(ALLOWED_USERS)) or '（无）'
        await reply(f'管理员：{admins}\n允许的用户：{users}')
        return
    ids = parse_ids(arg)
    if not ids:
        await reply(f'用法：/{cmd} 用户ID（多个用空格或逗号隔开）')
        return
    if cmd == 'allow':
        ALLOWED_USERS.update(ids)
    else:
        ALLOWED_USERS.difference_update(ids)
    save_ids('ALLOWED_USERS', ALLOWED_USERS)
    action = '已允许' if cmd == 'allow' else '已移除'
    await reply(f'✅ {action}：' + ', '.join(f'<code>{i}</code>' for i in sorted(ids)))


def clean_urls(urls):
    seen, out = set(), []
    for u in urls:
        u = u.rstrip('.,;!?)）】」』，。')
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def cmd_status(reply):
    du = shutil.disk_usage(DOWNLOAD_DIR)
    mem = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':', 1)
                mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    try:
        _, ver = await run_cmd(sys.executable, '-m', 'yt_dlp', '--version', timeout=60)
    except asyncio.TimeoutError:
        ver = '?'
    running = sum(j.started for j in jobs.values())
    await reply(
        f'📊 <b>状态</b>\n'
        f'模式：{backend.name}（单文件上限 {fmt_size(backend.max_upload)}）\n'
        f'任务：运行 {running} / 排队 {len(jobs) - running}\n'
        f'磁盘：剩余 {fmt_size(du.free)} / 共 {fmt_size(du.total)}\n'
        f'内存：可用 {fmt_size(mem.get("MemAvailable"))} / 共 {fmt_size(mem.get("MemTotal"))}\n'
        f'当前大小上限：{fmt_size(max(size_limit(), 0))}\n'
        f'yt-dlp：{html.escape(ver.strip())}\n'
        f'ffmpeg：{"✅" if FFMPEG else "❌"} · QuickJS：{"✅" if QJS else "❌（YouTube 可能受限）"}'
    )


async def cmd_update(reply):
    msg = await reply('🔄 正在更新 yt-dlp…')
    try:
        code, out = await run_cmd(
            sys.executable, '-m', 'pip', 'install', '-U', '--no-cache-dir', 'yt-dlp[default]', timeout=600,
        )
        _, ver = await run_cmd(sys.executable, '-m', 'yt_dlp', '--version', timeout=60)
    except asyncio.TimeoutError:
        await safe_edit(msg, '❌ 更新超时')
        return
    if code == 0:
        await safe_edit(msg, f'✅ yt-dlp 已是最新：{html.escape(ver.strip())}')
    else:
        await safe_edit(msg, f'❌ 更新失败\n<pre>{html.escape(out[-800:])}</pre>')


async def on_message(inc):
    text = inc.text.strip()
    uid = inc.sender_id

    async def reply(t, **kw):
        return await backend.send_text(inc.chat, t, reply_to=inc.msg_id, **kw)

    m = CMD_RE.match(text)
    cmd, arg = None, ''
    if m:
        cmd, target, arg = m[1].lower(), m[2], (m[3] or '').strip()
        if target and target.lower() != bot_username.lower():
            return  # 群里发给别的机器人的命令

    if cmd is None and not inc.is_private:
        return  # 群里只响应命令
    if not is_allowed(uid):
        if inc.is_private:
            await request_access(inc)
        elif cmd in ('start', 'help', 'id'):
            await reply(f'你的用户 ID：<code>{uid}</code>（没有使用权限，请私聊机器人申请）')
        return

    if cmd in ('start', 'help', 'id'):
        await reply(f'你的用户 ID：<code>{uid}</code>' if cmd == 'id' else help_text(uid))
        return
    if cmd in ('allow', 'remove', 'users') and is_admin(uid):
        await manage_users(cmd, arg, reply)
        return
    if cmd == 'status' and is_admin(uid):
        await cmd_status(reply)
        return
    if cmd == 'update' and is_admin(uid):
        await cmd_update(reply)
        return
    if cmd == 'cancel':
        mine = [j for j in jobs.values() if j.user_id == uid]
        for j in mine:
            j.task.cancel()
        await reply(f'已取消 {len(mine)} 个任务' if mine else '你没有进行中的任务')
        return

    if cmd in ('dl', 'video', 'audio', 'mp3'):
        mode = 'audio' if cmd in ('audio', 'mp3') else 'video'
        urls = clean_urls(URL_RE.findall(arg)) or clean_urls(URL_RE.findall(inc.reply_text))
        if not urls:
            await reply(f'用法：/{cmd} 视频链接')
            return
    elif cmd is not None:
        return  # 未知命令
    else:
        mode = 'video'
        urls = clean_urls(URL_RE.findall(text) + inc.links)
        if not urls:
            await reply('请发送视频链接 🙂  发 /help 查看用法')
            return

    for url in urls[:5]:
        await enqueue(inc, url, mode)


async def on_callback(cb):
    kind, _, arg = cb.data.partition(':')
    if kind in ('a', 'x'):
        await on_access_button(cb, kind, arg)
        return
    if kind != 'c':
        return
    job = jobs.get(arg)
    if not job:
        await backend.answer_callback(cb, '任务已经结束了')
        return
    if cb.sender_id != job.user_id and not is_admin(cb.sender_id):
        await backend.answer_callback(cb, '这不是你的任务', alert=True)
        return
    job.task.cancel()
    await backend.answer_callback(cb, '已取消')


async def on_access_button(cb, kind, arg):
    """管理员点了通知里的「允许」/「忽略」。"""
    if not is_admin(cb.sender_id):
        await backend.answer_callback(cb, '只有管理员可以操作', alert=True)
        return
    if not arg.lstrip('-').isdigit():
        return
    uid = int(arg)
    if kind == 'x':
        await backend.answer_callback(cb, '已忽略')
        await safe_edit(cb.msg, f'🚫 已忽略用户 <code>{uid}</code>')
        return
    if not ALLOW_ALL:
        ALLOWED_USERS.add(uid)
        save_ids('ALLOWED_USERS', ALLOWED_USERS)
    access_requests.pop(uid, None)
    await backend.answer_callback(cb, '已允许')
    await safe_edit(cb.msg, f'✅ 已允许用户 <code>{uid}</code>（发 /remove {uid} 可撤销）')
    try:
        await backend.send_text(uid, '✅ 管理员已批准，现在直接发视频链接就能下载了。发 /help 查看用法。')
    except Exception as e:  # noqa: BLE001
        log.warning('通知用户 %s 失败：%s', uid, e)


# ================================================================ 启动

async def main():
    global backend, bot_username
    if not BOT_TOKEN:
        sys.exit(f'缺少 BOT_TOKEN，请编辑 {BASE / ".env"}')
    if bool(API_ID) != bool(API_HASH):
        sys.exit('API_ID 和 API_HASH 要么都填，要么都留空（留空 = Bot API 模式，上限 50MB）')

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)  # 清理上次异常退出留下的文件
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if not FFMPEG:
        log.warning('未找到 ffmpeg：只能下载音视频一体的格式，YouTube 最高约 360p')
    if not QJS:
        log.warning('未找到 QuickJS：YouTube 可能只有少量格式可用')
    if not ADMIN_IDS and not ALLOWED_USERS and not ALLOW_ALL:
        log.warning('ADMIN_IDS / ALLOWED_USERS 都为空，目前没人能用；给机器人发 /id 获取 ID 后填入 .env')
    if ALLOW_ALL:
        log.warning('ALLOWED_USERS=*，任何人都可以使用这个机器人')

    backend = TelethonBackend(BOT_TOKEN, API_ID, API_HASH) if API_ID else BotApiBackend(BOT_TOKEN)
    try:
        bot_username = await backend.start()
    except BotApiError as e:
        sys.exit(f'登录失败，请检查 BOT_TOKEN：{e.desc}')
    try:
        await backend.set_commands(COMMANDS)
    except Exception:  # noqa: BLE001
        log.warning('设置命令菜单失败', exc_info=True)

    log.info(
        '机器人 @%s 已启动 | %s 模式，单文件上限 %s | ffmpeg=%s qjs=%s 最高 %sp 并发 %s',
        bot_username, backend.name, fmt_size(backend.max_upload),
        FFMPEG or '无', QJS or '无', MAX_HEIGHT, MAX_CONCURRENT,
    )
    await backend.run(on_message, on_callback)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
