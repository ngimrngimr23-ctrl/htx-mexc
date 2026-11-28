"""Автопроверка монеты на халяльность через @CryptoGulfHalal_Bot (вопрос задаёт юзербот).

Разбор ответа намеренно строгий: монета блокируется только на точную фразу
«غير مباح» («не разрешено»). Любой непонятный ответ — «не проверено»: сигнал
приходит с пометкой, а кусок сырого ответа пишется в лог и в пометку.
"""

import os
import re
import time

import userbot

BOT = os.environ.get("HALAL_BOT", "CryptoGulfHalal_Bot").strip().lstrip("@")

TTL_KNOWN = 7 * 24 * 3600   # вердикт может поменяться — бот сам об этом предупреждает
TTL_NO_RULING = 24 * 3600   # «фатвы пока нет» — новые фатвы появляются нечасто
TTL_UNKNOWN = 6 * 3600      # прочее «не проверено» перепроверяем чаще

HARAM, HALAL, UNKNOWN = "haram", "halal", "unknown"

# Невидимые символы направления текста и татвиль — без их удаления сравнение арабских строк ломается.
_INVISIBLE = re.compile("[‎‏‪-‮⁦-⁩ـ]")
_COIN_KEYS = ("أسم العملة", "اسم العملة", "إسم العملة")
_RULING_KEY = "حكم العملة"
_LINK_KEY = "رابط الحكم"
_SOURCE_KEY = "مصدر الحكم"
_NO_RULING = re.compile(r"لا\s*يوجد\s*حكم")  # «لايوجد حكم لعملة X حتى الأن» — решения пока нет


def _clean(text):
    return _INVISIBLE.sub("", text or "")


def _mentions(text, ticker):
    return re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(ticker), _clean(text), re.I) is not None


def parse_reply(text, ticker):
    """Разбор ответа: dict со status (haram/halal/unknown), ruling, link, source, coin, note.
    mismatch=True — ответ про другую монету (запоздалый ответ на прошлый вопрос)."""
    clean = _clean(text)
    fields = {}
    for line in clean.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")  # по ПЕРВОМУ двоеточию: в ссылке оно тоже есть
            fields[re.sub(r"\s+", " ", key).strip()] = re.sub(r"\s+", " ", val).strip()
    coin = next((fields[k] for k in _COIN_KEYS if fields.get(k)), "")
    ruling = fields.get(_RULING_KEY, "")
    link = re.search(r"https?://\S+", fields.get(_LINK_KEY, ""))
    res = {"ruling": ruling, "link": link.group(0) if link else "", "source": fields.get(_SOURCE_KEY, ""),
           "coin": coin, "note": ""}
    if not ruling and _NO_RULING.search(clean):
        if not _mentions(clean, ticker):
            res.update(status=UNKNOWN, note="ответ про другую монету", mismatch=True)
        else:
            res.update(status=UNKNOWN, note="фатвы пока нет", no_ruling=True)
    elif not ruling:
        snippet = re.sub(r"\s+", " ", clean).strip()[:120]
        res.update(status=UNKNOWN, note="в ответе нет вердикта: «%s»" % snippet)
    elif coin and coin.strip().upper() != ticker.upper():
        res.update(status=UNKNOWN, note="ответ про другую монету (%s)" % coin, mismatch=True)
    elif "غير مباح" in ruling:  # раньше «مباح»: вторая фраза входит в первую
        res["status"] = HARAM
    elif "مباح" in ruling:
        res["status"] = HALAL
    else:
        res.update(status=UNKNOWN, note="непонятный вердикт: %s" % ruling[:120])
    return res


def is_final(text, ticker):
    """Окончательный ответ именно про этот тикер (вердикт или «фатвы нет»)."""
    r = parse_reply(text, ticker)
    return not r.get("mismatch") and (bool(r["ruling"]) or bool(r.get("no_ruling")))


def _unknown(note):
    return {"status": UNKNOWN, "note": note, "ruling": "", "link": "", "source": "", "coin": ""}


class HalalChecker:
    def __init__(self, ub):
        self.ub = ub
        self.cache = {}  # ТИКЕР -> результат parse_reply + ts

    def configured(self):
        return self.ub.is_configured()

    @property
    def ready(self):
        return self.ub.ready

    @property
    def busy(self):
        return self.ub.busy

    def describe(self):
        return "юзербот %s → @%s, в кэше %d" % (self.ub.describe(), BOT, len(self.cache))

    def cached(self, ticker):
        e = self.cache.get(ticker.upper())
        if not e:
            return None
        ttl = TTL_NO_RULING if e.get("no_ruling") else TTL_UNKNOWN if e["status"] == UNKNOWN else TTL_KNOWN
        return e if time.time() - e.get("ts", 0) < ttl else None

    async def check(self, ticker, fresh=False):
        """Вердикт по тикеру. Никогда не бросает исключений."""
        ticker = ticker.upper()
        if not fresh:
            hit = self.cached(ticker)
            if hit:
                return hit
        if not self.ub.ready:
            return _unknown("юзербот не подключён")
        if self.ub.cooldown_left():
            return _unknown("Telegram просит подождать ещё %d с" % self.ub.cooldown_left())
        try:
            text, note = await self.ub.ask(BOT, ticker, lambda t: is_final(t, ticker), tag="HALAL",
                                           skip_if=None if fresh else (lambda: self.cached(ticker) is not None))
            if text is None and note == userbot.SKIPPED:
                return self.cached(ticker) or _unknown("проверка в очереди")  # пока ждали очереди, уже проверили
            if text is None:
                res = _unknown(note)
            else:
                res = parse_reply(text, ticker)
                if res.get("mismatch"):
                    res = _unknown("бот ответил про другую монету")
            if res["status"] == UNKNOWN and res.get("note"):
                print("[HALAL] %s: не проверено — %s" % (ticker, res["note"]), flush=True)
        except Exception as e:  # на всякий случай: проверка не должна ронять сканер
            res = _unknown("ошибка: %s" % type(e).__name__)
            print("[HALAL] %s: ошибка %r" % (ticker, e), flush=True)
        res["ts"] = time.time()
        self.cache[ticker] = res
        print("[HALAL] %s: %s%s" % (ticker, res["status"], " — " + res["note"] if res.get("note") else ""), flush=True)
        return res

    def export_cache(self):
        return self.cache

    def import_cache(self, data):
        if isinstance(data, dict):
            self.cache = {str(k).upper(): v for k, v in data.items()
                          if isinstance(v, dict) and v.get("status") in (HARAM, HALAL, UNKNOWN)}
