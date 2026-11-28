"""Цены монеты по биржам через @SpreaderApp (вопрос задаёт юзербот).

Зачем: перед автопокупкой на HTX убедиться, что дорого не только на Gate.
Если на остальном рынке (или на биржах из списка) цена заметно ниже Gate, значит,
задрана одна Gate, а не дёшев HTX. Пока монеты едут на Gate, цена там обычно
возвращается, и сделка уходит в минус.

Правило: на каждой выбранной бирже цена не ниже Gate больше чем на половину
спреда HTX→Gate. Например, при спреде 6% любая из бирж может быть дешевле Gate
не больше чем на 3%.

Ответ бота: шапка «DigiByte (DGB) · Цены спота», затем таблица «биржа | цена | Δ% |
объём». Разбор терпимый к оформлению: ячейки через «|», через пробелы или каждая
на своей строке. Ничего не разобрали — «не проверено», и монету не покупаем.
"""

import os
import re
import time

import userbot

BOT = os.environ.get("SPREADER_BOT", "SpreaderApp").strip().lstrip("@")
TTL = 120          # цены живут недолго: кэш на 2 минуты
MIN_VOL_ALL = 1000  # «весь рынок»: биржи с объёмом за сутки меньше этого ($) не считаем
USD_QUOTES = {"USDT", "USDC", "USD", "FDUSD"}
_HEADER_WORDS = {"биржа", "цена", "объём", "объем", "exchange", "price", "volume", "δ%", "Δ%"}
_ALIASES = {"gateio": "gate", "huobi": "htx", "okex": "okx", "cryptocom": "cryptocom"}

_NAME = r"[A-Za-z][A-Za-z0-9.\-]*(?:\s[A-Za-z][A-Za-z0-9.\-]*)?(?:\s*\([A-Za-z]{2,6}\))?"
_NUM = r"\d[\d,]*(?:\.\d+)?(?:[eE]-?\d+)?"
_ROW_SPACES = re.compile(r"^\s*(%s)\s+(%s)\s+([+\-−]?\d[\d.,]*)\s+(\d[\d.,]*\s*[KMBkmb]?)\s*$" % (_NAME, _NUM))


def ex_key(name):
    """«OKX (USDT)» → «okx», «Gate.io» → «gate», «Huobi» → «htx»."""
    k = re.sub(r"\(.*?\)", "", name or "").lower()
    k = re.sub(r"[^a-z0-9]", "", k)
    return _ALIASES.get(k, k)


def _quote(name):
    m = re.search(r"\(([A-Za-z]{2,6})\)", name or "")
    return m.group(1).upper() if m else "USDT"


def _num(s):
    s = (s or "").strip().replace("−", "-").replace(",", "")
    mult = 1.0
    if s[-1:].upper() in ("K", "M", "B"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1].upper()]
        s = s[:-1].strip()
    return float(s) * mult


def _row(name, price, vol):
    name = re.sub(r"\s+", " ", name).strip()
    if name.lower() in _HEADER_WORDS:
        return None
    try:
        return {"name": name, "ex": ex_key(name), "quote": _quote(name), "price": _num(price), "vol": _num(vol)}
    except (ValueError, IndexError):
        return None


def parse_prices(text, ticker):
    """{"rows": [...], "mismatch": bool, "coin": str}. Строка: name, ex, quote, price, vol."""
    text = (text or "").replace("‎", "").replace("‏", "")
    m = re.search(r"\(([A-Za-z0-9]{1,15})\)\s*[·\-—|]", text) or re.search(r"\(([A-Za-z0-9]{1,15})\)", text.split("\n")[0])
    coin = m.group(1).upper() if m else ""
    rows = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        ln2 = re.sub(r"[│┃¦\t]", "|", ln)
        if "|" in ln2:
            cells = [c.strip() for c in ln2.split("|") if c.strip()]
            if len(cells) >= 3 and re.fullmatch(_NAME, cells[0]) and re.fullmatch(_NUM, cells[1]):
                r = _row(cells[0], cells[1], cells[-1])
                if r:
                    rows.append(r)
            continue
        mm = _ROW_SPACES.match(ln2)
        if mm:
            r = _row(mm.group(1), mm.group(2), mm.group(4))
            if r:
                rows.append(r)
    if not rows:
        # Каждая ячейка на своей строке: имя, цена, Δ%, объём.
        cells = [ln.strip() for ln in lines]
        i = 0
        while i + 3 < len(cells):
            n, p, d, v = cells[i:i + 4]
            if (re.fullmatch(_NAME, n) and n.lower() not in _HEADER_WORDS and re.fullmatch(_NUM, p)
                    and re.fullmatch(r"[+\-−]?\d[\d.,]*", d) and re.fullmatch(r"\d[\d.,]*\s*[KMBkmb]?", v)):
                r = _row(n, p, v)
                if r:
                    rows.append(r)
                i += 4
            else:
                i += 1
    return {"rows": rows, "coin": coin, "mismatch": bool(coin) and coin != ticker.upper()}


def is_final(text, ticker):
    p = parse_prices(text, ticker)
    return not p["mismatch"] and len(p["rows"]) >= 2


def spot_button(msg, ticker):
    """Кнопка «Спот» под карточкой монеты (на тикер @SpreaderApp сначала присылает карточку:
    «Ethereum Classic (ETC) · Цена … · Спот 12 · перп 16» и меню, таблица — по кнопке «Спот»).
    Нажимаем, только если карточка про нашу монету."""
    head = re.search(r"\(([A-Za-z0-9]{1,15})\)", (getattr(msg, "raw_text", "") or "").split("\n")[0])
    if head and head.group(1).upper() != ticker.upper():
        return None
    for row in getattr(msg, "buttons", None) or []:
        for b in row:
            if re.sub(r"[^\w]", "", getattr(b, "text", "") or "").lower() in ("спот", "spot"):
                return b
    return None


def market_check(rows, gate_ref, spread_pct, markets=None):
    """(ok, пояснение). markets — ключи бирж (okx, binance…); пусто — весь рынок, кроме HTX и Gate.
    ok=False, если хоть одна выбранная биржа дешевле Gate больше чем на spread_pct/2,
    или сверять не с чем."""
    rows = [r for r in rows if r["quote"] in USD_QUOTES and r["price"] > 0]
    gate = next((r["price"] for r in rows if r["ex"] == "gate"), None) or gate_ref
    if not gate:
        return False, "нет цены Gate"
    best = {}  # одна строка на биржу: пара к USDT, иначе к USDC
    for r in rows:
        if r["ex"] in ("gate", "htx"):
            continue
        if markets:
            if r["ex"] not in markets:
                continue
        elif r["vol"] < MIN_VOL_ALL:
            continue
        cur = best.get(r["ex"])
        if cur is None or (cur["quote"] != "USDT" and r["quote"] == "USDT"):
            best[r["ex"]] = r
    if not best:
        return False, ("на выбранных биржах (%s) монеты нет" % ", ".join(markets) if markets
                       else "кроме HTX и Gate сравнить не с чем")
    allowed = float(spread_pct) / 2
    parts, bad = [], []
    for r in sorted(best.values(), key=lambda r: r["price"]):
        below = (gate - r["price"]) / gate * 100
        part = "%s %+.2f%%" % (r["name"], -below)
        parts.append(part)
        if below > allowed:
            bad.append(part)
    tail = " (к Gate; допуск −%.2f%% = половина спреда)" % allowed
    if bad:
        return False, "дешевле Gate больше допуска: %s%s" % (", ".join(bad), tail)
    return True, ", ".join(parts) + tail


def _unknown(note):
    return {"status": "unknown", "rows": [], "note": note}


class SpreaderChecker:
    def __init__(self, ub):
        self.ub = ub
        self.cache = {}

    def cached(self, ticker):
        e = self.cache.get(ticker.upper())
        return e if e and time.time() - e.get("ts", 0) < TTL else None

    async def check(self, ticker, fresh=False):
        """Цены по биржам. status ok — таблица разобрана. Никогда не бросает исключений."""
        ticker = ticker.upper()
        if not fresh:
            hit = self.cached(ticker)
            if hit:
                return hit
        try:
            text, note = await self.ub.ask(BOT, ticker, lambda t: is_final(t, ticker), tag="SPREADER",
                                           skip_if=None if fresh else (lambda: self.cached(ticker) is not None),
                                           press=lambda m: spot_button(m, ticker))
            if text is None and note == userbot.SKIPPED:
                return self.cached(ticker) or _unknown("проверка в очереди")
            if text is None:
                res = _unknown(note)
            else:
                p = parse_prices(text, ticker)
                if p["mismatch"]:
                    res = _unknown("ответ про другую монету (%s)" % p["coin"])
                elif len(p["rows"]) < 2:
                    res = _unknown("не разобрал ответ: «%s»" % re.sub(r"\s+", " ", text).strip()[:120])
                else:
                    res = {"status": "ok", "rows": p["rows"], "note": ""}
        except Exception as e:
            res = _unknown("ошибка: %s" % type(e).__name__)
            print("[SPREADER] %s: ошибка %r" % (ticker, e), flush=True)
        if res["status"] != "ok":
            print("[SPREADER] %s: не проверено — %s" % (ticker, res["note"]), flush=True)
        res["ts"] = time.time()
        self.cache[ticker] = res
        return res
