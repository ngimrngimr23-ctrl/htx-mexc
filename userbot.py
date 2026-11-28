"""Пользовательский аккаунт Telegram (юзербот) для вопросов другим ботам.

Боты в Telegram не могут писать другим ботам — через Bot API это невозможно.
Поэтому вопросы @CryptoGulfHalal_Bot (халяльность) и @SpreaderApp (цены по
биржам) задаёт обычный аккаунт через Telethon: отправляет текст и читает ответ,
как человек руками. Один аккаунт — одна очередь и одна пауза FloodWait на всех.

Настройка (Render → Environment). Все три значения — секреты:
    TG_API_ID, TG_API_HASH  с my.telegram.org → API development tools
    TG_SESSION              строка сессии, см. tools/make_session.py
Без них юзербот выключен, а бот работает как раньше.
"""

import asyncio
import os
import time

API_ID = os.environ.get("TG_API_ID", "").strip()
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION = os.environ.get("TG_SESSION", "").strip()

REPLY_TIMEOUT = 15   # сколько ждём ответ после отправки
POLL_INTERVAL = 1.0  # как часто перечитываем ответы бота
HARD_TIMEOUT = 30    # потолок на весь запрос, включая send_message: дольше висеть нельзя
MIN_INTERVAL = 1.5   # пауза между запросами, чтобы не спамить ботов
SKIPPED = "уже в кэше"


def log(msg, tag="TG"):
    print(f"[{tag}] {msg}", flush=True)


class UserBot:
    def __init__(self):
        self.client = None
        self.me = ""
        self.last_error = ""
        self.last_query = 0.0
        self.queries = 0
        self.cooldown_until = 0.0  # Telegram попросил аккаунт подождать (FloodWait)
        self.lock = asyncio.Lock()

    @staticmethod
    def is_configured():
        return bool(API_ID and API_HASH and SESSION)

    @property
    def ready(self):
        return self.client is not None

    @property
    def busy(self):
        return self.lock.locked()

    def cooldown_left(self):
        return max(0, int(self.cooldown_until - time.time()))

    def describe(self):
        if not self.is_configured():
            return "не настроен (нет TG_API_ID / TG_API_HASH / TG_SESSION)"
        if not self.ready:
            return "не подключён: %s" % (self.last_error or "ещё не запускался")
        pause = " — Telegram просит подождать ещё %d с" % self.cooldown_left() if self.cooldown_left() else ""
        return "подключён как %s%s" % (self.me, pause)

    async def start(self):
        """Подключение при старте бота. Битая сессия или сбой бота не роняют."""
        if not self.is_configured():
            log("юзербот выключен: не заданы TG_API_ID / TG_API_HASH / TG_SESSION")
            return False
        try:
            # Импорт здесь: без настроек Telethon вообще не нужен.
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            # flood_sleep_threshold=0: на FloodWait сразу получаем ошибку, а не молчаливый
            # сон до минуты внутри send_message, во время которого висит вся очередь.
            client = TelegramClient(StringSession(SESSION), int(API_ID), API_HASH,
                                    flood_sleep_threshold=0)
            await asyncio.wait_for(client.connect(), HARD_TIMEOUT)
            if not await client.is_user_authorized():
                self.last_error = "строка сессии недействительна — получи новую (tools/make_session.py)"
                log("юзербот: " + self.last_error, "ERR")
                await client.disconnect()
                return False
            me = await client.get_me()
            self.client = client
            self.me = "@%s" % me.username if getattr(me, "username", None) else "id %s" % me.id
            self.last_error = ""
            log("юзербот подключён как %s" % self.me)
            return True
        except Exception as e:
            self.last_error = type(e).__name__ + (": %s" % e if str(e) else "")
            log("юзербот не подключился: %s" % self.last_error, "ERR")
            return False

    async def ask(self, bot, text, is_final, reply_timeout=None, tag="TG", skip_if=None):
        """Отправляет text боту и ждёт ответ. Никогда не бросает исключений.

        is_final(text) -> True, когда ответ окончательный (вердикт, таблица и т.п.).
        Возвращает (текст ответа или None, пояснение при неудаче). Если окончательного
        ответа не было, отдаёт последний увиденный текст: пусть разбор решит, что это.
        skip_if() проверяется уже внутри очереди: True — не спрашивать (ответ уже в кэше,
        пока ждали очереди, эту монету спросили), вернётся (None, SKIPPED).
        """
        if not self.ready:
            return None, "юзербот не подключён"
        if self.cooldown_left():
            return None, "Telegram просит подождать ещё %d с" % self.cooldown_left()
        async with self.lock:
            if skip_if is not None and skip_if():
                return None, SKIPPED
            if self.cooldown_left():  # пока ждали очереди, мог прийти FloodWait
                return None, "Telegram просит подождать ещё %d с" % self.cooldown_left()
            wait = MIN_INTERVAL - (time.time() - self.last_query)
            if wait > 0:
                await asyncio.sleep(wait)
            started = time.time()
            reply_timeout = reply_timeout or REPLY_TIMEOUT
            log("спрашиваю @%s: %s" % (bot, text), tag)
            try:
                reply, note = await asyncio.wait_for(
                    self._ask(bot, text, is_final, reply_timeout), HARD_TIMEOUT)
            except asyncio.TimeoutError:
                reply, note = None, "запрос завис дольше %d с" % HARD_TIMEOUT
                self.last_error = "@%s %s: %s" % (bot, text, note)
                log(self.last_error, "ERR")
            except Exception as e:
                secs = int(getattr(e, "seconds", 0) or 0)
                if type(e).__name__ == "FloodWaitError" and secs:
                    self.cooldown_until = time.time() + secs
                    reply, note = None, "Telegram просит подождать %d с" % secs
                    self.last_error = "FloodWait %d с" % secs
                    log("Telegram просит аккаунт подождать %d с — до тех пор ботов не спрашиваю" % secs, "WARN")
                else:
                    reply, note = None, "ошибка запроса: %s" % type(e).__name__
                    self.last_error = "@%s %s: %r" % (bot, text, e)
                    log("запрос упал: " + self.last_error, "ERR")
            finally:
                self.last_query = time.time()
                self.queries += 1
            log("@%s %s: %s (%.1f с)" % (bot, text, "ответ получен" if reply is not None else note,
                                         time.time() - started), tag)
            return reply, note

    async def _ask(self, bot, text, is_final, reply_timeout):
        """Не client.conversation: бот может сначала прислать «ищу…», а потом
        ОТРЕДАКТИРОВАТЬ это же сообщение в ответ — события новых сообщений правку
        не видят. get_messages всегда отдаёт текущий текст: видны и новые, и правки."""
        sent = await self.client.send_message(bot, text)
        deadline = time.time() + reply_timeout
        last = None
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            msgs = await self.client.get_messages(bot, limit=5, min_id=sent.id)
            for msg in sorted(msgs or [], key=lambda m: m.id):
                if getattr(msg, "out", False):
                    continue  # своё исходящее
                t = getattr(msg, "raw_text", None) or ""
                if not t:
                    continue
                if is_final(t):
                    return t, ""
                last = t
        if last is not None:
            return last, "окончательного ответа за %d с не было" % reply_timeout
        return None, "бот не ответил за %d с" % reply_timeout
