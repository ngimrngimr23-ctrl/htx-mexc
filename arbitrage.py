"""
Автоарбитраж СТРОГО в одну сторону: купить на HTX → вывести на СВОЙ кошелёк →
переслать с кошелька на MEXC → продать на MEXC.

Подключается к основному боту (spread_scanner_bot.py) через setup() + router.

Логика одной сделки (подробно — в /arb_help):
  1. Монета из списка /arb_add, спред (грязный, MEXC bid против HTX ask) не
     ниже заданного минимума. Покупка на HTX выкупом ордеров на продажу
     (buy-ioc) по цене не выше той, где спред ещё равен минимуму. На весь
     USDT-баланс, либо (режим «проба») сперва на N$, а после успешного вывода
     пробы — сразу на весь остаток.
  2. Вывод с HTX на свой адрес. Если HTX вернул ошибку — сразу аварийная
     продажа на HTX в ноль. Если заявку принял — через минуту смотрим
     СВОБОДНЫЙ баланс монеты: не обнулился = вывод не прошёл → продажа в ноль.
  3. Ждём монеты на кошельке, пересылаем всё на депозитный адрес MEXC.
  4. Ждём зачисления на MEXC. Продажа: сразу по ордерам на покупку, пока цена
     даёт минимальный %; остаток — лимиткой в безубыток (или чуть ниже
     ближайшего продавца, если он стоит ниже безубытка), каждые N минут минус
     шаг, до пола. Как только плановую выгоду взять не удалось — спам в
     Telegram каждую секунду до /stop или до полной продажи.

Все ключи — только из переменных окружения.
"""
import asyncio
import base64
import datetime
import hashlib
import html
import hmac
import json
import os
import re
import time
import traceback
import urllib.parse
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import aiohttp
from aiogram import F, Router, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# ================= КЛЮЧИ И ОКРУЖЕНИЕ =================
# HTX: ключ с правами Read + Trade + Withdraw, привязанный к IP сервера. На HTX
# обязательно включить вывод только на адреса из адресной книги и добавить туда
# адреса кошельков бота (EVM и Sui) — тогда даже утёкший ключ не выведет чужому.
HTX_API_KEY = os.environ.get("HTX_API_KEY")
HTX_API_SECRET = os.environ.get("HTX_API_SECRET")
# MEXC: достаточно Read + Trade. Права на вывод НЕ нужны.
MEXC_API_KEY = os.environ.get("MEXC_API_KEY")
MEXC_API_SECRET = os.environ.get("MEXC_API_SECRET")
# Кошельки бота: один EVM-ключ на ETH/BNB/Monad (адрес одинаковый) + ключ Sui.
EVM_PRIVATE_KEY = os.environ.get("EVM_PRIVATE_KEY")
SUI_PRIVATE_KEY = os.environ.get("SUI_PRIVATE_KEY")
# Кто может управлять автоторговлей: Telegram user id через запятую. Без этой
# переменной команды /arb* не работают вообще — бот торгует реальными деньгами,
# и любой, кто нашёл бота в поиске, не должен иметь к этому доступа.
ADMIN_IDS = {
    int(x) for x in re.findall(r"-?\d+", os.environ.get("ARB_ADMIN_IDS", ""))
}

RPC_URLS = {
    "eth": os.environ.get("ETH_RPC_URL") or "https://ethereum-rpc.publicnode.com",
    "bsc": os.environ.get("BSC_RPC_URL") or "https://bsc-dataseed.bnbchain.org",
    "monad": os.environ.get("MONAD_RPC_URL") or "https://rpc.monad.xyz",
    "sui": os.environ.get("SUI_RPC_URL") or "https://fullnode.mainnet.sui.io:443",
    # Cosmos Hub: REST (LCD) API ноды, не JSON-RPC.
    "atom": (os.environ.get("COSMOS_REST_URL") or "https://cosmos-rest.publicnode.com").rstrip("/"),
    "sol": os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com",
    "trx": (os.environ.get("TRON_API_URL") or "https://api.trongrid.io").rstrip("/"),
}
NET_TITLES = {"eth": "Ethereum", "bsc": "BNB Chain", "monad": "Monad", "sui": "Sui", "atom": "Cosmos Hub",
              "sol": "Solana", "trx": "Tron"}
NATIVE_COIN = {"eth": "ETH", "bsc": "BNB", "monad": "MON", "sui": "SUI", "atom": "ATOM", "sol": "SOL", "trx": "TRX"}
BUILTIN_NETS = ("eth", "bsc", "monad", "sui", "atom", "sol", "trx")
# Как сеть может называться у бирж. Сравниваем по отдельным словам названия
# ("BEP20(BSC)" → BEP20, BSC), а не подстрокой — иначе ETH совпал бы с ETHW.
NET_ALIASES = {
    "eth": {"ERC20", "ETH", "ETHEREUM"},
    "bsc": {"BEP20", "BSC", "BNB SMART CHAIN", "BNBSMARTCHAIN", "BSC20"},
    "monad": {"MONAD", "MON"},
    "sui": {"SUI"},
    "atom": {"ATOM", "COSMOS", "COSMOSHUB"},
    "sol": {"SOL", "SOLANA", "SPL"},
    "trx": {"TRX", "TRON", "TRC20"},
}
SUI_NATIVE_TYPE = "0x2::sui::SUI"


def register_net(name, rpc_url, native, aliases):
    """Добавляет пользовательскую EVM-сеть (/arb_net add) в общие справочники —
    дальше она работает во всех этапах сделки так же, как встроенные."""
    RPC_URLS[name] = rpc_url
    NET_TITLES[name] = name.upper()
    NATIVE_COIN[name] = native.upper()
    NET_ALIASES[name] = {a.upper() for a in aliases} | {name.upper()}


def unregister_net(name):
    for table in (RPC_URLS, NET_TITLES, NATIVE_COIN, NET_ALIASES):
        table.pop(name, None)


NET_WORDS = {"bnb": "bsc", "bep20": "bsc", "erc20": "eth", "ethereum": "eth", "mon": "monad",
             "cosmos": "atom", "cosmoshub": "atom", "solana": "sol", "spl": "sol",
             "tron": "trx", "trc20": "trx"}


def parse_net(word):
    """Слово из команды → код сети или None, если это не сеть."""
    w = str(word or "").lower()
    w = NET_WORDS.get(w, w)
    return w if w in NET_TITLES else None


def net_for_coin(coin):
    """Сеть, у которой эта монета — родная (или сеть называется так же), иначе None."""
    c = coin.upper()
    for net, native in NATIVE_COIN.items():
        if native == c:
            return net
    return parse_net(c)


def nets_list_text():
    return ", ".join(f"<code>{n}</code>" for n in NET_TITLES)

HTX_HOST = "api.huobi.pro"
HTX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# ================= НАСТРОЙКИ АВТОТОРГОВЛИ =================
arb = {
    "enabled": False,     # /arb_on /arb_off
    "dry_run": True,      # тестовый режим: только сообщает, что сделал бы
    # "PEPE": {"pct": 3.0, "net": "bsc", "probe": 0, "htx_chain": None, "mexc_net": None}
    "coins": {},
    # Свои EVM-сети: "mapo": {"rpc": "https://...", "native": "MAPO", "aliases": ["MAPO", "MAP"]}
    "networks": {},
    "step_pct": 0.3,       # шаг снижения лимитки на MEXC, % от безубытка
    "step_sec": 120,       # как часто снижать, сек
    "floor_pct": 1.2,      # ниже безубытка минус этот % не опускаемся
    "check_sec": 60,       # через сколько после заявки на вывод смотреть баланс HTX
    "spam_sec": 1.0,       # период спама при аварии
    "poll_sec": 1.0,       # как часто проверять спред по монетам из списка (/arb_set poll)
    # Учитывать стакан MEXC при покупке: брать на HTX только столько, сколько на
    # MEXC сейчас покупают с нужным %, а не ориентироваться на одну лучшую цену.
    "mexc_depth": True,
    # С какой суммы купленного сразу выводить партию, не снимая ордер (/arb_set batch).
    "batch_usd": 15.0,
    # Монеты, на которые бот временно «забил» после /stop аварии: {монета: до_когда}.
    "paused": {},
    # Пополнение HTX с кошелька бота: если USDT на HTX меньше topup_usd — перевести
    # недостающее из сетей TOPUP_NETS (0 — выключено). /arb_set topup 600
    "topup_usd": 600.0,
    # Куплено на HTX, но не выведено (вывод не окупался): {монета: {"qty", "cost"}}.
    # Следующая сделка по монете начинает с этого остатка и выводит всё разом.
    "carry": {},
}



def net_pct(qty, cost, fee_qty, mexc_bid):
    """Выгода партии в % ЧИСТЫМИ — после комиссии вывода с HTX (в монетах):
    на MEXC дойдёт qty − fee_qty монет, продать их можно по mexc_bid."""
    if qty <= 0 or cost <= 0:
        return None
    return ((qty - fee_qty) * mexc_bid - cost) / cost * 100


def fee_extra_pct(fee_qty, mexc_bid, batch_cost):
    """Сколько % спреда съест комиссия вывода, размазанная на партию batch_cost $."""
    if batch_cost <= 0:
        return Decimal(10) ** 6
    extra = fee_qty * mexc_bid / batch_cost * 100
    # Мизерная комиссия (< 0.01%) порог не двигает: иначе спред ровно на пороге
    # отклонялся бы из-за тысячных долей цента.
    return extra if extra >= Decimal("0.01") else Decimal(0)

# Сделки по монетам: {монета: сделка}. Покупать может только одна (весь USDT в
# ней), остальные в это время доводят свои партии до продажи.
deals = {}
rescues = []      # аварийные продажи на HTX (вывод не прошёл)
alarms = {}       # id -> {"text": str, "acked": bool}


class _Ctx:
    bot = None
    session_getter = None
    redis = None
    chat_get = None
    chat_set = None


ctx = _Ctx()


def setup(bot, session_getter, redis_cmd, chat_get, chat_set):
    ctx.bot = bot
    ctx.session_getter = session_getter
    ctx.redis = redis_cmd
    ctx.chat_get = chat_get
    ctx.chat_set = chat_set


def http():
    return ctx.session_getter()


# Event loop держит на задачи только слабые ссылки — без этого набора сделку
# посреди выполнения мог бы убить сборщик мусора.
_tasks = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


class ExchangeError(Exception):
    pass


class NetworkMissing(ExchangeError):
    """Нужной сети у монеты на бирже просто нет — не ошибка, а повод пропустить монету."""


class CoinNotOnMexc(NetworkMissing):
    """Монеты с таким тикером на MEXC нет — добавлять её в список бессмысленно."""


class ContractMismatch(ExchangeError):
    """Контракты на HTX и MEXC разные — это две разные монеты с одним тикером."""


# ================= ЧИСЛА =================

def D(x):
    return Decimal(str(x))


def step_of(decimals):
    return Decimal(1).scaleb(-int(decimals))


def round_down(x, step):
    step = D(step)
    return (D(x) / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_up(x, step):
    step = D(step)
    return (D(x) / step).to_integral_value(rounding=ROUND_UP) * step


def dstr(x):
    """Decimal → строка без экспоненты (биржи не понимают 1E-8)."""
    s = format(D(x), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def fmt(x, digits=8):
    try:
        x = float(x)
    except Exception:
        return str(x)
    if x == 0:
        return "0"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def net_tokens(name):
    """'BNB Smart Chain(BEP20)' → {'BNB', 'SMART', 'CHAIN', 'BEP20', 'BNBSMARTCHAIN'}."""
    words = re.findall(r"[A-Z0-9]+", str(name or "").upper())
    return set(words) | {"".join(words)}


def matches_net(net, *names):
    aliases = NET_ALIASES[net]
    return any(net_tokens(n) & aliases for n in names if n)


# ================= HTX =================

def _htx_signed_query(method, path, params=None):
    p = {k: str(v) for k, v in (params or {}).items()}
    p.update({
        "AccessKeyId": HTX_API_KEY,
        "SignatureMethod": "HmacSHA256",
        "SignatureVersion": "2",
        "Timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    })
    query = urllib.parse.urlencode(sorted(p.items()), quote_via=urllib.parse.quote)
    payload = f"{method}\n{HTX_HOST}\n{path}\n{query}"
    sig = base64.b64encode(
        hmac.new(HTX_API_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    return f"{query}&Signature={urllib.parse.quote(sig, safe='')}"


async def htx_req(method, path, params=None, body=None, signed=True):
    if signed:
        if not HTX_API_KEY or not HTX_API_SECRET:
            raise ExchangeError("не заданы HTX_API_KEY / HTX_API_SECRET")
        query = _htx_signed_query(method, path, params if method == "GET" else None)
    else:
        query = urllib.parse.urlencode(params or {})
    url = f"https://{HTX_HOST}{path}" + (f"?{query}" if query else "")
    headers = dict(HTX_HEADERS)
    kwargs = {}
    if method == "POST":
        headers["Content-Type"] = "application/json"
        kwargs["data"] = json.dumps(body or {})
    async with http().request(method, url, headers=headers, timeout=TIMEOUT, **kwargs) as r:
        text = await r.text()
    try:
        data = json.loads(text)
    except Exception:
        raise ExchangeError(f"HTX {path}: HTTP {r.status} {text[:200]}")
    if data.get("status") == "error" or ("code" in data and data.get("code") not in (200, "200")):
        code = data.get("err-code") or data.get("code")
        msg = data.get("err-msg") or data.get("message")
        raise ExchangeError(f"HTX {path}: {msg} ({code})")
    if "tick" in data:
        return data["tick"]
    return data.get("data")


_htx_account_id = None


async def htx_account_id():
    global _htx_account_id
    if _htx_account_id is None:
        accounts = await htx_req("GET", "/v1/account/accounts")
        spot = [a for a in accounts or [] if a.get("type") == "spot"]
        if not spot:
            raise ExchangeError("HTX: не найден spot-аккаунт")
        _htx_account_id = spot[0]["id"]
    return _htx_account_id


async def htx_balance(currency):
    """(свободно, заморожено) по валюте на спотовом аккаунте HTX."""
    acc = await htx_account_id()
    data = await htx_req("GET", f"/v1/account/accounts/{acc}/balance")
    free, frozen = D(0), D(0)
    for item in (data or {}).get("list", []):
        if item.get("currency") != currency.lower():
            continue
        if item.get("type") == "trade":
            free += D(item.get("balance") or 0)
        elif item.get("type") == "frozen":
            frozen += D(item.get("balance") or 0)
    return free, frozen


_htx_sym_cache = {}


async def htx_symbol(sym):
    """Точности пары HTX: шаг цены, шаг количества, мин. сумма ордера."""
    sym = sym.lower()
    if sym in _htx_sym_cache:
        return _htx_sym_cache[sym]
    info = None
    try:
        data = await htx_req("GET", "/v1/settings/common/market-symbols",
                             {"symbols": sym}, signed=False)
        row = (data or [None])[0]
        if row:
            info = {
                "tick": step_of(row["pp"]),
                "step": step_of(row["ap"]),
                "min_value": D(row.get("minov") or 1),
                "min_qty": D(row.get("lominoa") or row.get("minoa") or 0),
            }
    except Exception:
        info = None
    if info is None:
        data = await htx_req("GET", "/v1/common/symbols", signed=False)
        row = next((s for s in data or [] if s.get("symbol") == sym), None)
        if not row:
            raise ExchangeError(f"HTX: пара {sym} не найдена")
        info = {
            "tick": step_of(row["price-precision"]),
            "step": step_of(row["amount-precision"]),
            "min_value": D(row.get("min-order-value") or 1),
            "min_qty": D(row.get("limit-order-min-order-amt") or row.get("min-order-amt") or 0),
        }
    _htx_sym_cache[sym] = info
    return info


async def htx_depth(sym):
    tick = await htx_req("GET", "/market/depth",
                         {"symbol": sym.lower(), "type": "step0"}, signed=False)
    asks = [(D(p), D(q)) for p, q in (tick or {}).get("asks", [])]
    bids = [(D(p), D(q)) for p, q in (tick or {}).get("bids", [])]
    return bids, asks


async def htx_place(sym, order_type, amount, price=None):
    body = {
        "account-id": str(await htx_account_id()),
        "symbol": sym.lower(),
        "type": order_type,
        "amount": dstr(amount),
        "source": "spot-api",
    }
    if price is not None:
        body["price"] = dstr(price)
    try:
        return str(await htx_req("POST", "/v1/order/orders/place", body=body))
    except ExchangeError as e:
        # Защита цены HTX: лимитка не дальше определённой полосы от рынка («Buy price
        # cannot be higher than 0.3671»). Прижимаем цену к границе и пробуем ещё раз:
        # покупка дешевле / продажа дороже только лучше, просто исполнится меньше.
        m = re.search(r"(higher|lower) than ([0-9]+(?:\.[0-9]+)?)", str(e))
        if price is None or not m or "price-m" not in str(e):
            raise
        limit = D(m.group(2))
        if (m.group(1) == "higher" and limit >= D(price)) or (m.group(1) == "lower" and limit <= D(price)):
            raise
        body["price"] = dstr(limit)
        return str(await htx_req("POST", "/v1/order/orders/place", body=body))


async def htx_order(order_id):
    o = await htx_req("GET", f"/v1/order/orders/{order_id}")
    filled = D(o.get("filled-amount", o.get("field-amount")) or 0)
    cash = D(o.get("filled-cash-amount", o.get("field-cash-amount")) or 0)
    return {"state": o.get("state"), "filled": filled, "cash": cash}


async def htx_cancel(order_id):
    try:
        await htx_req("POST", f"/v1/order/orders/{order_id}/submitcancel")
    except ExchangeError:
        pass  # уже исполнен/отменён


async def htx_wait_final(order_id, timeout=15):
    deadline = time.time() + timeout
    while True:
        o = await htx_order(order_id)
        if o["state"] in ("filled", "partial-canceled", "canceled") or time.time() > deadline:
            return o
        await asyncio.sleep(1)


_htx_chains_cache = {}


async def htx_chains(coin):
    """Сети монеты на HTX (кэш 60 сек — статус вывода должен быть свежим)."""
    hit = _htx_chains_cache.get(coin)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    data = await htx_req("GET", "/v2/reference/currencies",
                         {"currency": coin.lower()}, signed=False)
    # Берём строго свою монету: на неизвестный тикер HTX может ответить чужими.
    item = next((x for x in data or [] if str(x.get("currency", "")).lower() == coin.lower()), None)
    chains = item.get("chains", []) if item else []
    _htx_chains_cache[coin] = (time.time(), chains)
    return chains


_htx_ca_cache = {}


async def htx_v1_row(coin, chain):
    """Строка сети из второго источника HTX (/v1/settings/common/chains):
    ca — контракт, we/de — вывод/ввод включены, withdraw-desc — причина
    приостановки. Кэш 60 сек. {} если HTX не ответил."""
    key = (coin.lower(), chain.get("chain"))
    hit = _htx_ca_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    found = {}
    try:
        rows = await htx_req("GET", "/v1/settings/common/chains",
                             {"currency": coin.lower()}, signed=False)
        found = next((r for r in rows or [] if r.get("chain") == chain.get("chain")), {}) or {}
    except Exception as e:
        print(f"[arb] HTX chains {coin}: {e}", flush=True)
    _htx_ca_cache[key] = (time.time(), found)
    return found


async def htx_contract(coin, chain):
    """Адрес контракта монеты в сети на HTX или None, если HTX его не отдал."""
    row = await htx_v1_row(coin, chain)
    ca = row.get("ca") or row.get("contractAddress") or chain.get("contractAddress") or chain.get("ca")
    return str(ca).strip() if ca else None


def v1_closed(v):
    """True, если флаг второго источника HTX явно говорит «выключено»."""
    return v is not None and v != "" and str(v).strip().lower() in ("false", "0", "no", "off")


def norm_contract(x):
    """Приводит адреса к одному виду: регистр EVM не важен, у Sui 0x2 == 0x000…02,
    Tron в hex (41…) == Tron в base58 (T…)."""
    raw = str(x or "").strip()
    m = re.fullmatch(r"(?:0x)?(41[0-9a-fA-F]{40})", raw)
    if m:
        return _b58check(bytes.fromhex(m.group(1)))
    if re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", raw) or (len(raw) >= 32 and not raw.startswith("0x")):
        return raw  # base58 (Tron/Solana): регистр значим
    x = raw.lower()
    addr, sep, rest = x.partition("::")
    if addr.startswith("0x"):
        addr = "0x" + (addr[2:].lstrip("0") or "0")
    return addr + sep + rest


def htx_chain_fee(chain):
    ftype = chain.get("withdrawFeeType")
    try:
        if ftype == "fixed":
            return D(chain.get("transactFeeWithdraw") or 0)
        if ftype in ("circulated", "ratio"):
            return D(chain.get("minTransactFeeWithdraw") or 0)
    except Exception:
        pass
    return D(0)


async def htx_withdraw(address, coin, amount, chain_code, fee):
    body = {
        "address": address,
        "currency": coin.lower(),
        "amount": dstr(amount),
        "chain": chain_code,
    }
    if fee:
        body["fee"] = dstr(fee)
    return await htx_req("POST", "/v1/dw/withdraw/api/create", body=body)


# ================= MEXC =================

_mexc_clock = {"offset_ms": 0}


async def mexc_sync_clock():
    """Поправка на расхождение часов сервера бота и MEXC (ошибка 700003 «outside of the recvWindow»)."""
    t0 = time.time()
    async with http().get("https://api.mexc.com/api/v3/time", timeout=TIMEOUT) as r:
        data = json.loads(await r.text())
    t1 = time.time()
    _mexc_clock["offset_ms"] = int(data["serverTime"]) - int((t0 + t1) / 2 * 1000)


async def mexc_req(method, path, params=None, signed=True):
    try:
        return await _mexc_req(method, path, params, signed)
    except ExchangeError as e:
        if not signed or "700003" not in str(e):
            raise
        await mexc_sync_clock()  # часы разошлись — подстраиваемся и повторяем один раз
        return await _mexc_req(method, path, params, signed)


async def _mexc_req(method, path, params=None, signed=True):
    params = dict(params or {})
    if signed:
        if not MEXC_API_KEY or not MEXC_API_SECRET:
            raise ExchangeError("не заданы MEXC_API_KEY / MEXC_API_SECRET")
        params["timestamp"] = int(time.time() * 1000) + _mexc_clock["offset_ms"]
        params.setdefault("recvWindow", 10000)
        query = urllib.parse.urlencode(params)
        sig = hmac.new(MEXC_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = f"{query}&signature={sig}"
    else:
        query = urllib.parse.urlencode(params)
    url = f"https://api.mexc.com{path}" + (f"?{query}" if query else "")
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-MEXC-APIKEY"] = MEXC_API_KEY
    async with http().request(method, url, headers=headers, timeout=TIMEOUT) as r:
        text = await r.text()
        status = r.status
    try:
        data = json.loads(text)
    except Exception:
        raise ExchangeError(f"MEXC {path}: HTTP {status} {text[:200]}")
    if status != 200 or (isinstance(data, dict) and data.get("code") not in (None, 0, 200, "0", "200")):
        msg = data.get("msg") if isinstance(data, dict) else text[:200]
        code = data.get("code") if isinstance(data, dict) else status
        raise ExchangeError(f"MEXC {path}: {msg} ({code})")
    return data


async def mexc_depth(sym):
    data = await mexc_req("GET", "/api/v3/depth", {"symbol": sym, "limit": 100}, signed=False)
    bids = [(D(p), D(q)) for p, q in data.get("bids", [])]
    asks = [(D(p), D(q)) for p, q in data.get("asks", [])]
    return bids, asks


_mexc_sym_cache = {}


async def mexc_symbol(sym):
    if sym in _mexc_sym_cache:
        return _mexc_sym_cache[sym]
    data = await mexc_req("GET", "/api/v3/exchangeInfo", {"symbol": sym}, signed=False)
    row = (data.get("symbols") or [None])[0]
    if not row:
        raise ExchangeError(f"MEXC: пара {sym} не найдена")
    step = D(row.get("baseSizePrecision") or 0)
    if step <= 0:
        step = step_of(row.get("baseAssetPrecision", 8))
    info = {
        "tick": step_of(row.get("quotePrecision", row.get("quoteAssetPrecision", 8))),
        "step": step,
        "min_value": D(row.get("quoteAmountPrecision") or 1),
    }
    _mexc_sym_cache[sym] = info
    return info


async def mexc_free(asset):
    data = await mexc_req("GET", "/api/v3/account")
    for b in data.get("balances", []):
        if b.get("asset") == asset.upper():
            return D(b.get("free") or 0)
    return D(0)


async def mexc_total(asset):
    """Свободно + в ордерах."""
    data = await mexc_req("GET", "/api/v3/account")
    for b in data.get("balances", []):
        if b.get("asset") == asset.upper():
            return D(b.get("free") or 0) + D(b.get("locked") or 0)
    return D(0)


async def mexc_sold_since(sym, since):
    """Продажи по паре на MEXC с момента since (сек.): (кол-во, выручка $ за вычетом
    комиссии в USDT) — по истории сделок, кто бы ни продавал: бот или ты вручную."""
    qty, quote = D(0), D(0)
    start = int(since * 1000)
    while True:
        data = await mexc_req("GET", "/api/v3/myTrades", {"symbol": sym, "startTime": start, "limit": 1000})
        for t in data:
            if t.get("isBuyer"):
                continue
            qty += D(t["qty"])
            quote += D(t["quoteQty"])
            if str(t.get("commissionAsset", "")).upper() == "USDT":
                quote -= D(t.get("commission") or 0)
        if len(data) < 1000:
            return qty, quote
        start = int(data[-1]["time"]) + 1


async def mexc_place(sym, side, order_type, qty, price):
    data = await mexc_req("POST", "/api/v3/order", {
        "symbol": sym, "side": side, "type": order_type,
        "quantity": dstr(qty), "price": dstr(price),
    })
    return str(data["orderId"])


async def mexc_order(sym, order_id):
    o = await mexc_req("GET", "/api/v3/order", {"symbol": sym, "orderId": order_id})
    return {
        "status": o.get("status"),
        "filled": D(o.get("executedQty") or 0),
        "quote": D(o.get("cummulativeQuoteQty") or 0),
    }


async def mexc_cancel(sym, order_id):
    try:
        await mexc_req("DELETE", "/api/v3/order", {"symbol": sym, "orderId": order_id})
    except ExchangeError:
        pass


async def mexc_wait_final(sym, order_id, timeout=15):
    deadline = time.time() + timeout
    while True:
        o = await mexc_order(sym, order_id)
        if o["status"] in ("FILLED", "CANCELED", "PARTIALLY_CANCELED") or time.time() > deadline:
            return o
        await asyncio.sleep(1)


_mexc_config_cache = {"ts": 0.0, "data": []}


async def mexc_networks(coin):
    """Сети монеты на MEXC. getall — тяжёлый ответ по всем монетам, кэш 2 мин."""
    if time.time() - _mexc_config_cache["ts"] > 120:
        data = await mexc_req("GET", "/api/v3/capital/config/getall")
        _mexc_config_cache.update(ts=time.time(), data=data if isinstance(data, list) else [])
    for item in _mexc_config_cache["data"]:
        if str(item.get("coin", "")).upper() == coin.upper():
            return item.get("networkList", [])
    return []


async def mexc_deposit_address(coin, net_entry):
    names = {net_entry.get("netWork"), net_entry.get("network")} - {None}

    def pick(rows):
        for row in rows if isinstance(rows, list) else []:
            if row.get("network") in names or row.get("netWork") in names:
                return row
        return None

    row = pick(await mexc_req("GET", "/api/v3/capital/deposit/address", {"coin": coin.upper()}))
    if not row:
        # Адреса ещё нет — просим MEXC его сгенерировать.
        net_param = net_entry.get("netWork") or net_entry.get("network")
        await mexc_req("POST", "/api/v3/capital/deposit/address",
                       {"coin": coin.upper(), "network": net_param})
        row = pick(await mexc_req("GET", "/api/v3/capital/deposit/address", {"coin": coin.upper()}))
    if not row or not row.get("address"):
        raise ExchangeError(f"MEXC: не удалось получить адрес депозита {coin} ({'/'.join(names)})")
    return row["address"], (row.get("memo") or row.get("tag") or None)


# ================= КОШЕЛЬКИ =================

async def _rpc_once(url, net, method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with http().post(url, json=body, timeout=TIMEOUT) as r:
        data = await r.json(content_type=None)
    if data.get("error"):
        raise ExchangeError(f"RPC {net} {method}: {data['error']}")
    return data.get("result")


async def rpc(net, method, params):
    """JSON-RPC к ноде сети. Если нода не отвечает, а у сети есть проверенные
    запасные (сети, найденные автоматически), — пробуем их и переходим на рабочую."""
    try:
        return await _rpc_once(RPC_URLS[net], net, method, params)
    except ExchangeError:
        raise  # нода ответила ошибкой по существу — запасная не поможет
    except Exception:
        spares = [u for u in arb.get("networks", {}).get(net, {}).get("rpcs", []) if u != RPC_URLS[net]]
        for url in spares:
            try:
                res = await _rpc_once(url, net, method, params)
            except Exception:
                continue
            RPC_URLS[net] = url
            arb["networks"][net]["rpc"] = url
            return res
        raise


def _pad32(hex_no0x):
    return hex_no0x.rjust(64, "0")


_evm_account = None


def evm_account():
    global _evm_account
    if _evm_account is None:
        if not EVM_PRIVATE_KEY:
            raise ExchangeError("не задан EVM_PRIVATE_KEY")
        from eth_account import Account
        _evm_account = Account.from_key(EVM_PRIVATE_KEY.strip())
    return _evm_account


async def evm_balance(net, token):
    addr = evm_account().address
    if not token:
        return int(await rpc(net, "eth_getBalance", [addr, "latest"]), 16)
    data = "0x70a08231" + _pad32(addr[2:].lower())
    res = await rpc(net, "eth_call", [{"to": token, "data": data}, "latest"])
    return int(res or "0x0", 16)


async def evm_decimals(net, token):
    if not token:
        return 18
    res = await rpc(net, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
    return int(res, 16)


_evm_send_locks = {}


async def evm_send_all(net, token, to, amount=None):
    """Отправляет ВЕСЬ баланс токена (или нативной монеты за вычетом газа), а если
    задан amount (в минимальных единицах токена) — столько, но не больше баланса.
    Возвращает (tx_hash, отправлено_в_минимальных_единицах)."""
    lock = _evm_send_locks.setdefault(net, asyncio.Lock())
    async with lock:  # две отправки в одной сети одновременно получили бы один nonce
        return await _evm_send(net, token, to, amount)


async def _evm_send(net, token, to, want=None):
    from eth_utils import to_checksum_address
    acct = evm_account()
    to = to_checksum_address(to)
    chain_id = int(await rpc(net, "eth_chainId", []), 16)
    nonce = int(await rpc(net, "eth_getTransactionCount", [acct.address, "pending"]), 16)
    gas_price = int(int(await rpc(net, "eth_gasPrice", []), 16) * 1.2)
    balance = await evm_balance(net, token)
    if token:
        amount = balance if want is None else min(int(want), balance)
        data = "0xa9059cbb" + _pad32(to[2:].lower()) + _pad32(format(amount, "x"))
        est = int(await rpc(net, "eth_estimateGas", [{
            "from": acct.address, "to": token, "data": data}]), 16)
        tx = {"to": to_checksum_address(token), "value": 0, "data": data,
              "gas": int(est * 1.3) + 10000}
    else:
        gas = 21000
        amount = balance - gas * gas_price * 2  # запас на скачок цены газа
        if amount <= 0:
            raise ExchangeError(f"{NET_TITLES[net]}: на кошельке не хватает даже на газ")
        tx = {"to": to, "value": amount, "data": b"", "gas": gas}
    if amount <= 0:
        raise ExchangeError(f"{NET_TITLES[net]}: на кошельке нет монет для пересылки")
    tx.update({"nonce": nonce, "gasPrice": gas_price, "chainId": chain_id})
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = await rpc(net, "eth_sendRawTransaction", ["0x" + bytes(raw).hex()])
    deadline = time.time() + 600
    while time.time() < deadline:
        await asyncio.sleep(4)
        receipt = await rpc(net, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            if int(receipt.get("status", "0x0"), 16) != 1:
                raise ExchangeError(f"{NET_TITLES[net]}: транзакция {tx_hash} упала")
            return tx_hash, amount
    raise ExchangeError(f"{NET_TITLES[net]}: транзакция {tx_hash} не подтвердилась за 10 минут")


# ---- Sui ----
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def bech32_decode(s):
    s = s.lower()
    pos = s.rfind("1")
    hrp, data = s[:pos], [_BECH32.index(c) for c in s[pos + 1:]]
    if _bech32_polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data) != 1:
        raise ValueError("неверная контрольная сумма bech32")
    acc, bits, out = 0, 0, []
    for v in data[:-6]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xff)
    return hrp, bytes(out)


def parse_sui_key(raw):
    """Принимает suiprivkey1..., hex (32 байта) или base64 (флаг + 32 байта)."""
    raw = raw.strip()
    if raw.startswith("suiprivkey"):
        _, data = bech32_decode(raw)
        if data[0] != 0:
            raise ValueError("поддерживается только ключ Ed25519")
        return data[1:33]
    hex_part = raw[2:] if raw.startswith("0x") else raw
    if re.fullmatch(r"[0-9a-fA-F]{64}", hex_part):
        return bytes.fromhex(hex_part)
    data = base64.b64decode(raw)
    if len(data) == 33:
        if data[0] != 0:
            raise ValueError("поддерживается только ключ Ed25519")
        return data[1:]
    if len(data) == 32:
        return data
    raise ValueError("не удалось разобрать SUI_PRIVATE_KEY")


_sui_keys = None


def sui_keys():
    """(приватный ключ, публичный ключ bytes, адрес 0x...)."""
    global _sui_keys
    if _sui_keys is None:
        if not SUI_PRIVATE_KEY:
            raise ExchangeError("не задан SUI_PRIVATE_KEY")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        priv = Ed25519PrivateKey.from_private_bytes(parse_sui_key(SUI_PRIVATE_KEY))
        pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
        addr = "0x" + hashlib.blake2b(b"\x00" + pub, digest_size=32).hexdigest()
        _sui_keys = (priv, pub, addr)
    return _sui_keys


def sui_sign(tx_bytes_b64):
    priv, pub, _ = sui_keys()
    tx_bytes = base64.b64decode(tx_bytes_b64)
    digest = hashlib.blake2b(b"\x00\x00\x00" + tx_bytes, digest_size=32).digest()
    return base64.b64encode(b"\x00" + priv.sign(digest) + pub).decode()


async def sui_balance(coin_type):
    res = await rpc("sui", "suix_getBalance", [sui_keys()[2], coin_type or SUI_NATIVE_TYPE])
    return int(res.get("totalBalance", 0))


async def sui_decimals(coin_type):
    if not coin_type or coin_type == SUI_NATIVE_TYPE:
        return 9
    meta = await rpc("sui", "suix_getCoinMetadata", [coin_type])
    return int(meta["decimals"])


async def sui_send_all(coin_type, to):
    addr = sui_keys()[2]
    coin_type = coin_type or SUI_NATIVE_TYPE
    coins, cursor = [], None
    while True:
        page = await rpc("sui", "suix_getCoins", [addr, coin_type, cursor, 50])
        coins += page.get("data", [])
        if not page.get("hasNextPage"):
            break
        cursor = page.get("nextCursor")
    if not coins:
        raise ExchangeError("Sui: на кошельке нет монет для пересылки")
    ids = [c["coinObjectId"] for c in coins]
    total = sum(int(c["balance"]) for c in coins)
    gas_budget = "20000000"
    if coin_type == SUI_NATIVE_TYPE:
        tx = await rpc("sui", "unsafe_payAllSui", [addr, ids, to, gas_budget])
        amount = total - int(gas_budget)
    else:
        tx = await rpc("sui", "unsafe_pay", [addr, ids, [to], [str(total)], None, gas_budget])
        amount = total
    res = await rpc("sui", "sui_executeTransactionBlock", [
        tx["txBytes"], [sui_sign(tx["txBytes"])], {"showEffects": True}, "WaitForLocalExecution"])
    status = ((res.get("effects") or {}).get("status") or {})
    if status.get("status") != "success":
        raise ExchangeError(f"Sui: транзакция не прошла: {status.get('error') or res}")
    return res.get("digest"), amount


# ---- Cosmos Hub (ATOM) ----
# Ключ secp256k1 — тот же, что у EVM (EVM_PRIVATE_KEY), если не задан отдельный
# COSMOS_PRIVATE_KEY. Адрес cosmos1… = bech32(ripemd160(sha256(сжатый pubkey))).
# Транзакции — protobuf Cosmos SDK, режим подписи SIGN_MODE_DIRECT, отправка через
# REST ноды. Поддерживается только сам ATOM (uatom), не IBC-токены.
COSMOS_PRIVATE_KEY = os.environ.get("COSMOS_PRIVATE_KEY")
COSMOS_DENOM = "uatom"
COSMOS_GAS_LIMIT = 150000
MEMO_NETS = {"atom"}  # сети, где бот умеет передать memo для депозита MEXC


def _bech32_encode(hrp, data8):
    acc = bits = 0
    data5 = []
    for b in data8:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data5.append((acc >> bits) & 31)
    if bits:
        data5.append((acc << (5 - bits)) & 31)
    values = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data5 + [0] * 6
    pm = _bech32_polymod(values) ^ 1
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[x] for x in data5 + checksum)


def _ripemd160(data):
    try:
        from Crypto.Hash import RIPEMD160
        return RIPEMD160.new(data).digest()
    except ImportError:
        return hashlib.new("ripemd160", data).digest()


_cosmos_keys = None


def cosmos_keys():
    """(приватный ключ eth_keys, сжатый pubkey 33 байта, адрес cosmos1…)."""
    global _cosmos_keys
    if _cosmos_keys is None:
        raw = (COSMOS_PRIVATE_KEY or EVM_PRIVATE_KEY or "").strip()
        if not raw:
            raise ExchangeError("не задан EVM_PRIVATE_KEY (или COSMOS_PRIVATE_KEY) для Cosmos Hub")
        from eth_keys import keys
        pk = keys.PrivateKey(bytes.fromhex(raw[2:] if raw.startswith("0x") else raw))
        pub = pk.public_key.to_compressed_bytes()
        addr = _bech32_encode("cosmos", _ripemd160(hashlib.sha256(pub).digest()))
        _cosmos_keys = (pk, pub, addr)
    return _cosmos_keys


def _pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_bytes(field, data):
    """Поле length-delimited (строки, байты, вложенные сообщения). Пустые — опускаем."""
    if isinstance(data, str):
        data = data.encode()
    if not data:
        return b""
    return _pb_varint(field << 3 | 2) + _pb_varint(len(data)) + data


def _pb_uint(field, n):
    return _pb_varint(field << 3) + _pb_varint(n) if n else b""


def _pb_coin(denom, amount):
    return _pb_bytes(1, denom) + _pb_bytes(2, str(amount))


def cosmos_build_tx(from_addr, to_addr, amount, memo, fee_amount, gas_limit, pubkey,
                    account_number, sequence, chain_id, sign_fn):
    """Собирает и подписывает MsgSend. Возвращает байты TxRaw."""
    msg = _pb_bytes(1, from_addr) + _pb_bytes(2, to_addr) + _pb_bytes(3, _pb_coin(COSMOS_DENOM, amount))
    any_msg = _pb_bytes(1, "/cosmos.bank.v1beta1.MsgSend") + _pb_bytes(2, msg)
    body = _pb_bytes(1, any_msg) + _pb_bytes(2, memo or "")
    any_pub = _pb_bytes(1, "/cosmos.crypto.secp256k1.PubKey") + _pb_bytes(2, _pb_bytes(1, pubkey))
    mode_info = _pb_bytes(1, _pb_uint(1, 1))  # single { mode: SIGN_MODE_DIRECT }
    signer = _pb_bytes(1, any_pub) + _pb_bytes(2, mode_info) + _pb_uint(3, sequence)
    fee = _pb_bytes(1, _pb_coin(COSMOS_DENOM, fee_amount)) + _pb_uint(2, gas_limit)
    auth = _pb_bytes(1, signer) + _pb_bytes(2, fee)
    sign_doc = _pb_bytes(1, body) + _pb_bytes(2, auth) + _pb_bytes(3, chain_id) + _pb_uint(4, account_number)
    sig = sign_fn(hashlib.sha256(sign_doc).digest())
    return _pb_bytes(1, body) + _pb_bytes(2, auth) + _pb_bytes(3, sig)


def cosmos_sign(digest):
    """Подпись secp256k1 в формате Cosmos: r‖s (64 байта, low-S)."""
    pk = cosmos_keys()[0]
    sig = pk.sign_msg_hash(digest)
    n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    s_ = sig.s if sig.s <= n // 2 else n - sig.s
    return sig.r.to_bytes(32, "big") + s_.to_bytes(32, "big")


async def cosmos_get(path):
    async with http().get(RPC_URLS["atom"] + path, timeout=TIMEOUT) as r:
        data = await r.json(content_type=None)
        if r.status != 200:
            raise ExchangeError(f"Cosmos {path}: HTTP {r.status} {str(data)[:200]}")
        return data


async def cosmos_balance():
    data = await cosmos_get(f"/cosmos/bank/v1beta1/balances/{cosmos_keys()[2]}/by_denom?denom={COSMOS_DENOM}")
    return int((data.get("balance") or {}).get("amount") or 0)


async def cosmos_fee():
    """Комиссия в uatom: текущая цена газа сети (x/feemarket) с запасом, иначе 0.025."""
    price = D("0.025")
    try:
        data = await cosmos_get(f"/feemarket/v1/gas_price/{COSMOS_DENOM}")
        price = max(D(str(data["price"]["amount"])) * D("1.5"), D("0.005"))
    except Exception:
        pass
    return int((price * COSMOS_GAS_LIMIT).to_integral_value(rounding=ROUND_UP))


async def cosmos_send_all(to, memo):
    """Отправляет весь ATOM за вычетом комиссии. Возвращает (txhash, отправлено_uatom)."""
    pk, pub, addr = cosmos_keys()
    acc = (await cosmos_get(f"/cosmos/auth/v1beta1/accounts/{addr}"))["account"]
    acc = acc.get("base_account") or acc
    chain_id = (await cosmos_get("/cosmos/base/tendermint/v1beta1/node_info"))["default_node_info"]["network"]
    fee = await cosmos_fee()
    amount = await cosmos_balance() - fee
    if amount <= 0:
        raise ExchangeError("Cosmos: на кошельке не хватает ATOM даже на комиссию")
    tx = cosmos_build_tx(addr, to, amount, memo, fee, COSMOS_GAS_LIMIT, pub,
                         int(acc.get("account_number") or 0), int(acc.get("sequence") or 0), chain_id, cosmos_sign)
    async with http().post(RPC_URLS["atom"] + "/cosmos/tx/v1beta1/txs", timeout=TIMEOUT,
                           json={"tx_bytes": base64.b64encode(tx).decode(), "mode": "BROADCAST_MODE_SYNC"}) as r:
        res = (await r.json(content_type=None)).get("tx_response") or {}
    if int(res.get("code") or 0) != 0:
        raise ExchangeError(f"Cosmos: транзакция отклонена: {res.get('raw_log') or res}")
    txhash = res.get("txhash")
    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            got = (await cosmos_get(f"/cosmos/tx/v1beta1/txs/{txhash}")).get("tx_response") or {}
        except ExchangeError:
            continue  # ещё не в блоке
        if int(got.get("code") or 0) != 0:
            raise ExchangeError(f"Cosmos: транзакция {txhash} упала: {got.get('raw_log')}")
        return txhash, amount
    raise ExchangeError(f"Cosmos: транзакция {txhash} не подтвердилась за 2 минуты")


# ---- base58 ----
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


# ---- Solana ----
# Ключ ed25519: SOLANA_PRIVATE_KEY (base58 из Phantom — 64 байта, или JSON-массив
# байтов), иначе выводится из EVM_PRIVATE_KEY (те же 32 байта как seed ed25519).
# Перевод SOL (System Program) и SPL-токенов (Token / Token-2022, transferChecked,
# ATA получателя создаётся при необходимости). Legacy-транзакции, отправка через RPC.
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")
SOL_SYSTEM = "11111111111111111111111111111111"
SOL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOL_TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SOL_ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWrXEJz1n4vcpVmS"
SOL_FEE_RESERVE = 3_000_000  # лампорты, оставляем на кошельке SOL под комиссии/ренту


_sol_keys = None


def sol_keys():
    """(приватный ключ ed25519, pubkey 32 байта, адрес base58)."""
    global _sol_keys
    if _sol_keys is None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        raw = (SOLANA_PRIVATE_KEY or "").strip()
        if raw.startswith("["):
            seed = bytes(json.loads(raw))[:32]
        elif raw:
            seed = b58decode(raw)[:32]
        else:
            evm = (EVM_PRIVATE_KEY or "").strip()
            if not evm:
                raise ExchangeError("не задан SOLANA_PRIVATE_KEY (или EVM_PRIVATE_KEY) для Solana")
            seed = bytes.fromhex(evm[2:] if evm.startswith("0x") else evm)
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        _sol_keys = (priv, pub, b58encode(pub))
    return _sol_keys


_ED_P = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P


def _on_curve(b32):
    """Лежит ли 32-байтовая строка на кривой ed25519 (для поиска PDA)."""
    y = int.from_bytes(b32, "little") & ((1 << 255) - 1)
    if y >= _ED_P:
        return False
    u = (y * y - 1) % _ED_P
    v = (_ED_D * y * y + 1) % _ED_P
    x2 = u * pow(v, _ED_P - 2, _ED_P) % _ED_P
    if x2 == 0:
        return True
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P != 0:
        x = x * pow(2, (_ED_P - 1) // 4, _ED_P) % _ED_P
    return (x * x - x2) % _ED_P == 0


def sol_find_pda(seeds, program):
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ExchangeError("Solana: не нашёл PDA")


def sol_ata(owner, mint, token_program=SOL_TOKEN):
    return sol_find_pda([b58decode(owner), b58decode(token_program), b58decode(mint)], SOL_ATA)


def _compact(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def sol_build_tx(payer, instructions, blockhash, sign_fn):
    """instructions: [(program, [(pubkey, is_signer, is_writable)], data)]. Legacy-сообщение."""
    metas = {payer: [True, True]}
    order = [payer]
    for prog, accs, _ in instructions:
        for pk, s_, w in accs:
            if pk not in metas:
                metas[pk] = [s_, w]
                order.append(pk)
            else:
                metas[pk][0] |= s_
                metas[pk][1] |= w
        if prog not in metas:
            metas[prog] = [False, False]
            order.append(prog)
    groups = ([k for k in order if metas[k] == [True, True]], [k for k in order if metas[k] == [True, False]],
              [k for k in order if metas[k] == [False, True]], [k for k in order if metas[k] == [False, False]])
    keys = groups[0] + groups[1] + groups[2] + groups[3]
    idx = {k: i for i, k in enumerate(keys)}
    msg = bytes([len(groups[0]) + len(groups[1]), len(groups[1]), len(groups[3])])
    msg += _compact(len(keys)) + b"".join(b58decode(k).rjust(32, b"\0") for k in keys)
    msg += b58decode(blockhash).rjust(32, b"\0")
    msg += _compact(len(instructions))
    for prog, accs, data in instructions:
        msg += bytes([idx[prog]]) + _compact(len(accs)) + bytes(idx[a[0]] for a in accs)
        msg += _compact(len(data)) + data
    return _compact(1) + sign_fn(msg) + msg


def sol_sign(msg):
    return sol_keys()[0].sign(msg)


async def sol_rpc(method, params):
    return await rpc("sol", method, params)


async def sol_token_program(mint):
    info = await sol_rpc("getAccountInfo", [mint, {"encoding": "base64"}])
    owner = ((info or {}).get("value") or {}).get("owner")
    if owner not in (SOL_TOKEN, SOL_TOKEN22):
        raise ExchangeError(f"Solana: {mint} — не SPL-токен")
    return owner


async def sol_token_accounts(owner, mint):
    res = await sol_rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    out = []
    for a in (res or {}).get("value", []):
        amt = a["account"]["data"]["parsed"]["info"]["tokenAmount"]
        out.append((a["pubkey"], int(amt["amount"]), int(amt["decimals"])))
    return out


async def sol_balance(mint):
    addr = sol_keys()[2]
    if not mint:
        return int((await sol_rpc("getBalance", [addr]))["value"])
    return sum(a for _, a, _ in await sol_token_accounts(addr, mint))


async def sol_decimals(mint):
    if not mint:
        return 9
    return int((await sol_rpc("getTokenSupply", [mint]))["value"]["decimals"])


async def sol_send_all(mint, to):
    addr = sol_keys()[2]
    bh = (await sol_rpc("getLatestBlockhash", [{"commitment": "finalized"}]))["value"]["blockhash"]
    if not mint:
        amount = await sol_balance(None) - 5000 - SOL_FEE_RESERVE
        if amount <= 0:
            raise ExchangeError("Solana: на кошельке нет SOL для пересылки")
        ix = [(SOL_SYSTEM, [(addr, True, True), (to, False, True)],
               (2).to_bytes(4, "little") + amount.to_bytes(8, "little"))]
    else:
        prog = await sol_token_program(mint)
        accs = sorted(await sol_token_accounts(addr, mint), key=lambda a: -a[1])
        if not accs or accs[0][1] <= 0:
            raise ExchangeError("Solana: на кошельке нет токенов для пересылки")
        src, amount, dec = accs[0]
        dest = sol_ata(to, mint, prog)
        ix = [
            # Создать ATA получателя, если его ещё нет (Idempotent — не падает, если есть).
            (SOL_ATA, [(addr, True, True), (dest, False, True), (to, False, False), (mint, False, False),
                       (SOL_SYSTEM, False, False), (prog, False, False)], bytes([1])),
            (prog, [(src, False, True), (mint, False, False), (dest, False, True), (addr, True, False)],
             bytes([12]) + amount.to_bytes(8, "little") + bytes([dec])),
        ]
    tx = sol_build_tx(addr, ix, bh, sol_sign)
    sig = await sol_rpc("sendTransaction", [base64.b64encode(tx).decode(),
                                            {"encoding": "base64", "preflightCommitment": "confirmed"}])
    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(3)
        st = ((await sol_rpc("getSignatureStatuses", [[sig]])) or {}).get("value", [None])[0]
        if st and st.get("err"):
            raise ExchangeError(f"Solana: транзакция {sig} упала: {st['err']}")
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig, amount
    raise ExchangeError(f"Solana: транзакция {sig} не подтвердилась за 2 минуты")


# ---- Tron ----
# Ключ secp256k1 — тот же EVM_PRIVATE_KEY (TronLink импортирует тот же hex-ключ),
# адрес T… = base58check(0x41 + последние 20 байт keccak(pubkey)). Транзакции
# собирает нода TronGrid, мы только подписываем txID и отправляем.
TRON_API_KEY = os.environ.get("TRON_API_KEY")
TRC20_FEE_LIMIT = 30_000_000  # сан (30 TRX) — потолок сжигания на энергию для TRC20


def _b58check(payload):
    return b58encode(payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4])


def tron_hex(addr):
    raw = b58decode(addr)
    return raw[:21]


_tron_keys = None


def tron_keys():
    """(ключ eth_keys, адрес T…)."""
    global _tron_keys
    if _tron_keys is None:
        from eth_keys import keys
        from eth_utils import keccak
        raw = (EVM_PRIVATE_KEY or "").strip()
        if not raw:
            raise ExchangeError("не задан EVM_PRIVATE_KEY для Tron")
        pk = keys.PrivateKey(bytes.fromhex(raw[2:] if raw.startswith("0x") else raw))
        addr = _b58check(b"\x41" + keccak(pk.public_key.to_bytes())[-20:])
        _tron_keys = (pk, addr)
    return _tron_keys


async def tron_post(path, body):
    headers = {"TRON-PRO-API-KEY": TRON_API_KEY} if TRON_API_KEY else {}
    async with http().post(RPC_URLS["trx"] + path, json=body, headers=headers, timeout=TIMEOUT) as r:
        data = await r.json(content_type=None)
    if isinstance(data, dict) and data.get("Error"):
        raise ExchangeError(f"Tron {path}: {data['Error']}")
    return data


def _abi_address(addr):
    return tron_hex(addr)[1:].hex().rjust(64, "0")


async def tron_const_call(contract, selector, param=""):
    res = await tron_post("/wallet/triggerconstantcontract", {
        "owner_address": tron_keys()[1], "contract_address": contract,
        "function_selector": selector, "parameter": param, "visible": True})
    out = (res.get("constant_result") or ["0"])[0]
    return int(out or "0", 16)


async def tron_balance(token):
    addr = tron_keys()[1]
    if not token:
        acc = await tron_post("/wallet/getaccount", {"address": addr, "visible": True})
        return int(acc.get("balance") or 0)
    return await tron_const_call(token, "balanceOf(address)", _abi_address(addr))


async def tron_decimals(token):
    return 6 if not token else await tron_const_call(token, "decimals()")


def tron_sign(txid_hex):
    """Подпись txID: r‖s‖v (65 байт, v = 0/1), как ждёт Tron."""
    return tron_keys()[0].sign_msg_hash(bytes.fromhex(txid_hex)).to_bytes().hex()


async def tron_send_all(token, to):
    addr = tron_keys()[1]
    if not token:
        amount = await tron_balance(None) - 2_000_000  # 2 TRX на bandwidth
        if amount <= 0:
            raise ExchangeError("Tron: на кошельке нет TRX для пересылки")
        tx = await tron_post("/wallet/createtransaction",
                             {"owner_address": addr, "to_address": to, "amount": amount, "visible": True})
    else:
        amount = await tron_balance(token)
        if amount <= 0:
            raise ExchangeError("Tron: на кошельке нет токенов для пересылки")
        res = await tron_post("/wallet/triggersmartcontract", {
            "owner_address": addr, "contract_address": token, "function_selector": "transfer(address,uint256)",
            "parameter": _abi_address(to) + format(amount, "x").rjust(64, "0"),
            "fee_limit": TRC20_FEE_LIMIT, "call_value": 0, "visible": True})
        tx = res.get("transaction") or {}
    if not tx.get("txID"):
        raise ExchangeError(f"Tron: нода не собрала транзакцию: {str(tx)[:200]}")
    tx["signature"] = [tron_sign(tx["txID"])]
    res = await tron_post("/wallet/broadcasttransaction", tx)
    if not res.get("result"):
        raise ExchangeError(f"Tron: транзакция отклонена: {res}")
    txid = tx["txID"]
    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(4)
        info = await tron_post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            rcpt = (info.get("receipt") or {}).get("result")
            if token and rcpt not in (None, "SUCCESS"):
                raise ExchangeError(f"Tron: транзакция {txid} упала: {rcpt}")
            return txid, amount
    raise ExchangeError(f"Tron: транзакция {txid} не подтвердилась за 2 минуты")


def wallet_address(net):
    if net == "sol":
        return sol_keys()[2]
    if net == "trx":
        return tron_keys()[1]
    if net == "sui":
        return sui_keys()[2]
    if net == "atom":
        return cosmos_keys()[2]
    return evm_account().address


async def wallet_balance(net, token):
    if net == "sol":
        return await sol_balance(token)
    if net == "trx":
        return await tron_balance(token)
    if net == "sui":
        return await sui_balance(token)
    if net == "atom":
        return await cosmos_balance()
    return await evm_balance(net, token)


async def wallet_decimals(net, token):
    if net == "sol":
        return await sol_decimals(token)
    if net == "trx":
        return await tron_decimals(token)
    if net == "sui":
        return await sui_decimals(token)
    if net == "atom":
        return 6
    return await evm_decimals(net, token)


async def wallet_send_all(net, token, to, memo=None):
    if net == "sol":
        return await sol_send_all(token, to)
    if net == "trx":
        return await tron_send_all(token, to)
    if net == "sui":
        return await sui_send_all(token, to)
    if net == "atom":
        return await cosmos_send_all(to, memo)
    return await evm_send_all(net, token, to)


address_book_errors = {}


_book_cache = {}


async def htx_book_rows(currency):
    """Адресная книга вывода HTX по монете (кэш 60 сек.)."""
    cur = currency.lower()
    hit = _book_cache.get(cur)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    rows = await htx_req("GET", "/v2/account/withdraw/address", {"currency": cur}) or []
    _book_cache[cur] = (time.time(), rows)
    return rows


def _book_match(rows, chain, address, any_chain=False):
    """Адрес из книги в ТОМ ЖЕ написании, что сохранён на HTX (регистр букв важен:
    HTX сравнивает строки буква в букву), или None."""
    addr = address.lower()
    for r in rows or []:
        if str(r.get("address", "")).lower() == addr and (any_chain or not chain or r.get("chain") == chain):
            return str(r["address"])
    return None


def _is_evm_address(a):
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", str(a or "")))


async def htx_find_saved(currency, chain, address):
    """Ищет адрес в адресной книге HTX: сначала у самой монеты, а для EVM-адреса —
    ещё и среди адресов других монет (так HTX хранит общий адрес на всю сеть).
    Возвращает (написание_как_в_книге, «монета»/«общий») или (None, None).
    Бросает исключение, если книгу не удалось прочитать вовсе."""
    found = _book_match(await htx_book_rows(currency), chain, address)
    if found:
        return found, "монета"
    if _is_evm_address(address):
        for other in ("usdt", "eth", "usdc", "bnb"):
            if other == currency.lower():
                continue
            try:
                found = _book_match(await htx_book_rows(other), None, address, any_chain=True)
            except Exception:
                continue
            if found:
                return found, "общий"
    return None, None


async def htx_address_saved(currency, chain, address):
    """Есть ли адрес в адресной книге вывода HTX (у монеты или общий на сеть).
    Через API HTX выводит ТОЛЬКО на сохранённые адреса (иначе ошибка
    api-not-support-temp-addr). None — проверить не удалось (ключ/ошибка API)."""
    try:
        found, _ = await htx_find_saved(currency, chain, address)
    except Exception as e:
        print(f"[arb] адресная книга HTX {currency}: {e}", flush=True)
        address_book_errors[currency.upper()] = str(e)[:200]
        return None
    return found is not None


async def htx_book_address(currency, chain, address):
    """Написание адреса для заявки на вывод — как в адресной книге HTX. Если адрес
    не нашёлся, EVM-адрес отдаём маленькими буквами (так его хранит HTX)."""
    try:
        found, _ = await htx_find_saved(currency, chain, address)
    except Exception:
        found = None
    if found:
        return found
    return address.lower() if _is_evm_address(address) else address


def _addr_key(address):
    return address.lower() if _is_evm_address(address) else address


async def htx_address_status(currency, chain, address, fee=None):
    """(True/False/None, как проверено).
    True — адрес есть в книге HTX у монеты, или по нему уже прошёл вывод через API
    (для EVM-адреса это значит, что общий адрес работает для всех EVM-монет).
    False — HTX уже отклонял вывод на него как на временный (для этой монеты).
    None — не подтверждён: через API общий адрес не виден, проверкой станет первый вывод."""
    key = _addr_key(address)
    try:
        found, kind = await htx_find_saved(currency, chain, address)
        if found:
            return True, ("в адресной книге" if kind == "монета" else "общий адрес на сеть")
    except Exception as e:
        address_book_errors[currency.upper()] = str(e)[:200]
    if arb.get("addr_ok", {}).get(key):
        return True, "по нему уже проходил вывод через API"
    if currency.upper() in arb.get("addr_bad", {}).get(key, []):
        return False, "HTX уже отклонял вывод на него как на временный"
    return None, "в книге через API не виден (общий адрес HTX не показывает) — проверкой станет первый вывод"


def remember_address(address, currency, ok):
    """Запоминает итог реального вывода на адрес: работает / HTX считает временным."""
    key = _addr_key(address)
    if ok:
        arb.setdefault("addr_ok", {})[key] = True
        arb.setdefault("addr_bad", {}).pop(key, None)
    else:
        bad = arb.setdefault("addr_bad", {}).setdefault(key, [])
        if currency.upper() not in bad:
            bad.append(currency.upper())


def address_book_hint(coin_htx, chain, address):
    extra = (" Для EVM-адреса (0x…) можно один раз добавить его как общий адрес на сеть — бот его увидит."
             if _is_evm_address(address) else "")
    return (f"Добавь адрес бота в адресную книгу вывода HTX: монета <b>{coin_htx}</b>, сеть <code>{chain}</code>, "
            f"адрес <code>{address.lower() if _is_evm_address(address) else address}</code>. "
            f"Через API HTX выводит только на сохранённые адреса.{extra}")


# ================= СОПОСТАВЛЕНИЕ СЕТЕЙ =================

_htx_pairs = {"ts": 0.0, "by_base": {}}


async def htx_usdt_pair(currency):
    """Настоящее имя спотовой пары монеты к USDT на HTX (например «monadusdt»)
    по коду монеты — из общего списка пар HTX, а не склейкой строк. None, если
    такой пары нет. Список кэшируется на час."""
    if time.time() - _htx_pairs["ts"] > 3600 or not _htx_pairs["by_base"]:
        by_base = {}
        try:
            rows = await htx_req("GET", "/v1/settings/common/market-symbols", signed=False)
            for r in rows or []:
                if str(r.get("qc", "")).lower() == "usdt" and r.get("state", "online") == "online":
                    by_base[str(r.get("bc", "")).lower()] = r.get("symbol")
        except ExchangeError:
            pass
        if not by_base:
            rows = await htx_req("GET", "/v1/common/symbols", signed=False)
            for r in rows or []:
                if str(r.get("quote-currency", "")).lower() == "usdt" and r.get("state", "online") == "online":
                    by_base[str(r.get("base-currency", "")).lower()] = r.get("symbol")
        _htx_pairs.update(ts=time.time(), by_base=by_base)
    return _htx_pairs["by_base"].get(currency.lower())


def hsym(coin, cfg):
    """Имя пары на HTX для запросов стакана/ордеров: найденное и сохранённое при
    сверке монеты (cfg["htx_symbol"]), иначе — по тикеру."""
    return (cfg.get("htx_symbol") or f"{hcoin(coin, cfg)}usdt").lower()


def hcoin(coin, cfg):
    """Тикер монеты на HTX. Обычно совпадает с MEXC, но не всегда: Monad на HTX —
    MONAD, на MEXC — MON (а MON на HTX — вообще другая монета, PixelMon)."""
    return (cfg.get("htx_coin") or coin).upper()


_htx_all_v1 = {"ts": 0.0, "rows": []}


async def htx_all_v1_rows():
    """Все сети всех монет HTX из /v1/settings/common/chains (с контрактами, ca).
    Нужен, чтобы найти монету на HTX по адресу контракта. Кэш 10 минут."""
    if time.time() - _htx_all_v1["ts"] > 600:
        rows = await htx_req("GET", "/v1/settings/common/chains", signed=False)
        _htx_all_v1.update(ts=time.time(), rows=rows or [])
    return _htx_all_v1["rows"]


def _net_chains(chains, net, cfg):
    if cfg.get("htx_chain"):
        return [c for c in chains if c.get("chain") == cfg["htx_chain"]]
    return [c for c in chains if matches_net(
        net, c.get("baseChain"), c.get("baseChainProtocol"), c.get("displayName"), c.get("chain"))]


async def find_htx_ticker(coin, net, token, cfg):
    """Как эта монета называется на HTX, если тикер отличается от MEXC.
    Токен ищем по адресу контракта среди ВСЕХ монет HTX; нативную монету сети
    (контракта у неё нет) — по названию сети (Monad: MEXC «MON», HTX «MONAD»).
    Возвращает (тикер, как_нашли) или (None, None)."""
    if token:
        want = norm_contract(token)
        hits = sorted({str(r.get("currency", "")).upper() for r in await htx_all_v1_rows()
                       if r.get("ca") and norm_contract(r.get("ca")) == want})
        for t in hits:
            if t != coin.upper() and _net_chains(await htx_chains(t), net, cfg):
                return t, "по адресу контракта"
        return None, None
    candidates = [net.upper(), NATIVE_COIN[net], re.sub(r"[^A-Z0-9]", "", NET_TITLES[net].upper())]
    for t in dict.fromkeys(candidates):
        if t != coin.upper() and _net_chains(await htx_chains(t), net, cfg):
            return t, f"нативная монета сети {NET_TITLES[net]}"
    return None, None


def _is_evm_net(net):
    return net in ("eth", "bsc", "monad") or net in arb.get("networks", {})


async def _mexc_net_by_contract(coin, net, nets):
    """Сеть MEXC, когда по названию не совпала (MEXC: «MAP», у нас «MAPO»):
      * родная монета сети — единственная её сеть на MEXC без контракта;
      * токен в EVM-сети — та сеть MEXC, чей контракт реально есть в НАШЕЙ сети
        (eth_getCode через её ноду).
    Возвращает запись сети или None."""
    if coin.upper() == NATIVE_COIN[net]:
        native = [n for n in nets if not (n.get("contract") or "").strip()]
        return native[0] if len(native) == 1 else None
    if not _is_evm_net(net):
        return None
    hits = []
    for n in nets:
        ca = (n.get("contract") or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", ca):
            continue
        try:
            code = await rpc(net, "eth_getCode", [ca, "latest"])
        except Exception:
            continue
        if code and code not in ("0x", "0x0"):
            hits.append(n)
    return hits[0] if len(hits) == 1 else None


async def _htx_chain_by_contract(ticker, chains, token):
    """Сеть HTX, когда по названию не совпала: для токена — та, где контракт на
    HTX совпадает с контрактом MEXC; для родной монеты — единственная без контракта."""
    rows = [r for r in await htx_all_v1_rows() if str(r.get("currency", "")).lower() == ticker.lower()]
    if token:
        want = norm_contract(token)
        codes = {r.get("chain") for r in rows if r.get("ca") and norm_contract(r.get("ca")) == want}
    else:
        codes = {r.get("chain") for r in rows if not str(r.get("ca") or "").strip()}
    cand = [c for c in chains if c.get("chain") in codes]
    return cand[0] if len(cand) == 1 else None


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _urls_in(obj):
    """Все ссылки в ответе биржи о сети монеты (эксплорер и т.п.), в любом поле."""
    if isinstance(obj, dict):
        return [u for v in obj.values() for u in _urls_in(v)]
    if isinstance(obj, list):
        return [u for v in obj for u in _urls_in(v)]
    return _URL_RE.findall(obj) if isinstance(obj, str) else []


def _host(url):
    h = urllib.parse.urlsplit(url).hostname or ""
    return h[4:] if h.startswith("www.") else h


async def explorer_check(net, *sources):
    """Сверка сети по эксплореру, который указала биржа: домен эксплорера ищем в
    реестре Chainlist (там у каждой сети список её эксплореров) и сравниваем
    chain id с сетью бота. Возвращает (True/False/None, пояснение)."""
    our_id = EVM_CHAIN_IDS.get(net) or arb.get("networks", {}).get(net, {}).get("chain_id")
    hosts = {_host(u) for src in sources for u in _urls_in(src)} - {""}
    if not our_id or not hosts:
        return None, "биржа не дала ссылку на эксплорер" if our_id else ""
    try:
        chains = await chainlist()
    except Exception:
        return None, "реестр сетей недоступен"
    hits = {}
    for c in chains:
        for e in c.get("explorers") or []:
            h = _host(str(e.get("url", "")))
            if h and h in hosts:
                hits[c.get("chainId")] = (c.get("name"), h)
    if not hits:
        return None, f"эксплорер {', '.join(sorted(hosts))} не найден в реестре сетей"
    if our_id in hits:
        return True, f"эксплорер {hits[our_id][1]} — сеть {hits[our_id][0]} (chain id {our_id})"
    cid, (name, h) = next(iter(hits.items()))
    return False, (f"по эксплореру биржи ({h}) монета в сети {name} (chain id {cid}), "
                   f"а у бота сеть {NET_TITLES[net]} (chain id {our_id})")


async def resolve_coin(coin, cfg):
    """Находит монету и её сеть на обеих биржах и сверяет контракт.
    Тикер на HTX бот находит сам: если под тем же тикером на HTX другая монета
    (другой контракт или нет нужной сети), ищет её по контракту / по сети и
    запоминает найденный тикер в cfg["htx_coin"].
    Возвращает dict или бросает ExchangeError с понятной причиной."""
    net = cfg["net"]

    # ---- MEXC: сеть и контракт — это «эталон», с которым сверяем HTX ----
    nets = await mexc_networks(coin)
    if not nets:
        raise CoinNotOnMexc(f"монеты {coin} нет на MEXC — проверь тикер (как в паре {coin}/USDT на бирже)")
    if cfg.get("mexc_net"):
        mx = [n for n in nets if cfg["mexc_net"] in (n.get("netWork"), n.get("network"))]
    else:
        mx = [n for n in nets if matches_net(net, n.get("netWork"), n.get("network"))]
    found = ", ".join(str(n.get("netWork") or n.get("network")) for n in nets) or "нет ни одной"
    if not mx and not cfg.get("mexc_net"):
        hit = await _mexc_net_by_contract(coin, net, nets)
        if hit:
            mx = [hit]
            cfg["mexc_net"] = hit.get("netWork") or hit.get("network")  # запоминаем сопоставление
    if not mx:
        raise NetworkMissing(f"на MEXC у {coin} нет сети {NET_TITLES[net]} (есть: {found})")
    if len(mx) > 1:
        raise ExchangeError(
            f"MEXC: не удалось однозначно найти сеть {NET_TITLES[net]} для {coin} "
            f"(сети на MEXC: {found}). Укажи вручную: /arb_add {coin} {cfg['pct']} {net} mexc=<имя>")
    mx = mx[0]

    token = None
    if coin.upper() != NATIVE_COIN[net]:
        token = (mx.get("contract") or "").strip()
        if not token:
            raise ExchangeError(f"MEXC не отдал контракт {coin} в сети {NET_TITLES[net]}")

    # ---- HTX: та же монета? ----
    async def check_htx(ticker):
        """(chains, htx_chain, htx_ca, problem) для тикера на HTX."""
        chains = await htx_chains(ticker)
        if not chains:
            return chains, None, None, "нет"
        if not await htx_usdt_pair(ticker):  # есть ли у этой монеты спотовая пара с USDT
            return chains, None, None, "нет пары"
        cand = _net_chains(chains, net, cfg)
        if not cand and not cfg.get("htx_chain"):
            hit = await _htx_chain_by_contract(ticker, chains, token)
            if hit:
                matched_chain[ticker] = hit["chain"]
                cand = [hit]
        if not cand:
            return chains, None, None, "нет сети"
        if len(cand) > 1:
            return chains, None, None, "много сетей"
        ca = await htx_contract(ticker, cand[0]) if token else None
        if token and ca and norm_contract(ca) != norm_contract(token):
            return chains, cand[0], ca, "другой контракт"
        return chains, cand[0], ca, None

    matched_chain = {}  # тикер HTX → сеть, найденная по контракту, а не по названию
    auto_how = None
    hc = hcoin(coin, cfg)
    chains, htx, htx_ca, problem = await check_htx(hc)
    if problem and problem not in ("много сетей",) and not cfg.get("htx_coin_manual"):
        alt, how = await find_htx_ticker(coin, net, token, cfg)
        if alt:
            alt_res = await check_htx(alt)
            if not alt_res[3]:
                hc, (chains, htx, htx_ca, problem) = alt, alt_res
                auto_how = how
                cfg["htx_coin"] = alt  # запоминаем — дальше все запросы к HTX идут по нему

    found = ", ".join(c.get("chain", "?") for c in chains) or "нет ни одной"
    if problem == "нет":
        raise NetworkMissing(f"монеты {coin} нет на HTX (ни под этим тикером, ни по контракту/сети)")
    if problem == "нет пары":
        raise NetworkMissing(f"на HTX нет торговой пары {hc}/USDT (монета {hc} есть, но торговать её там нельзя)")
    if problem == "нет сети":
        raise NetworkMissing(f"на HTX у {hc} нет сети {NET_TITLES[net]} (есть: {found}), "
                             f"и по {'контракту' if token else 'названию сети'} другой тикер не нашёлся")
    if problem == "много сетей":
        raise ExchangeError(
            f"HTX: не удалось однозначно найти сеть {NET_TITLES[net]} для {hc} "
            f"(сети на HTX: {found}). Укажи вручную: /arb_add {coin} {cfg['pct']} {net} htx=<код>")
    if problem == "другой контракт":
        raise ContractMismatch(
            f"контракты разные — это РАЗНЫЕ монеты, а монеты с контрактом MEXC на HTX не нашлось.\n"
            f"HTX ({hc}): <code>{htx_ca}</code>\nMEXC ({coin}): <code>{token}</code>")

    if hc in matched_chain:
        cfg["htx_chain"] = matched_chain[hc]  # запоминаем сопоставление по контракту
    # Запоминаем найденную монету и НАСТОЯЩЕЕ имя пары на HTX — дальше стакан и
    # ордера идут строго по ним, а не по тикеру с MEXC.
    pair = await htx_usdt_pair(hc)
    if cfg.get("htx_symbol") != pair or (hc != coin.upper() and cfg.get("htx_coin") != hc):
        cfg["htx_symbol"] = pair
        if hc != coin.upper():
            cfg["htx_coin"] = hc
        auto_how = auto_how or "обновлена пара"

    # Сверка сети по эксплореру, который биржи указали для монеты.
    explorer_ok, explorer_note = await explorer_check(net, mx, htx, await htx_v1_row(hc, htx))
    if explorer_ok is False:
        raise ContractMismatch(explorer_note)

    if token:
        contract_status = "ok" if htx_ca else "unknown"
    else:
        contract_status = "native"
        if net == "sui":
            token = SUI_NATIVE_TYPE

    return {
        "htx_ticker": hc,
        "htx_pair": pair,
        "htx_ticker_auto": auto_how,
        "htx_chain": htx.get("chain"),
        # Вывод «открыт», только если ни один из двух источников HTX не говорит обратное.
        "htx_withdraw_ok": htx.get("withdrawStatus") == "allowed" and not v1_closed((await htx_v1_row(hc, htx)).get("we")),
        "htx_withdraw_status": htx.get("withdrawStatus"),
        "htx_fee": htx_chain_fee(htx),
        "htx_min_withdraw": D(htx.get("minWithdrawAmt") or 0),
        "htx_withdraw_step": step_of(htx.get("withdrawPrecision", 8)),
        "mexc_net": mx,
        "mexc_deposit_ok": bool(mx.get("depositEnable", True)),
        "token": token,
        "htx_contract": htx_ca,
        # ok — совпал с HTX; native — нативная монета сети, контракта нет;
        # unknown — HTX контракт не отдал, нужна ручная сверка (/arb_confirm).
        "contract_status": contract_status,
        "explorer": (explorer_ok, explorer_note),
    }


def contract_confirmed(cfg, res):
    return res["contract_status"] in ("ok", "native") or \
        (cfg.get("confirmed_contract") and norm_contract(cfg["confirmed_contract"]) == norm_contract(res["token"]))


def contract_line(coin, cfg, res):
    ok, note = res.get("explorer") or (None, "")
    ex = (f"\n✅ Сеть по эксплореру биржи: {note}" if ok else f"\n❔ Сеть по эксплореру: {note}" if note else "")
    return _contract_line(coin, cfg, res) + ex


def _contract_line(coin, cfg, res):
    st = res["contract_status"]
    if st == "native":
        return "Контракт: нативная монета сети (контракта нет)"
    line = f"Контракт MEXC: <code>{res['token']}</code>"
    if st == "ok":
        return line + "\n✅ Совпадает с контрактом на HTX"
    if contract_confirmed(cfg, res):
        return line + "\n✅ Подтверждён вручную (/arb_confirm)"
    return (line + "\n⚠️ HTX не отдал контракт — сверь его на странице депозита HTX в этой сети и, "
            f"если совпадает, подтверди: /arb_confirm {coin}. До этого реальных сделок по монете не будет.")


# ================= TELEGRAM: уведомления и спам =================

async def notify(text, kb=None):
    chat = ctx.chat_get()
    if not chat:
        print(f"[arb] нет chat_id, сообщение: {text}", flush=True)
        return
    for _ in range(3):
        try:
            await ctx.bot.send_message(chat, text, parse_mode="HTML", reply_markup=kb)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            print(f"[arb] ошибка отправки: {e}", flush=True)
            return


STOP_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🛑 Остановить спам", callback_data="arb_stop_spam")]])


def alarm_start(text, pause_coin=None):
    """pause_coin — монета, на которую бот «забивает», если спам остановили."""
    aid = uuid.uuid4().hex[:8]
    alarms[aid] = {"text": text, "acked": False, "pause_coin": pause_coin}
    return aid


def alarm_update(aid, text):
    if aid in alarms:
        alarms[aid]["text"] = text


def alarm_end(aid):
    alarms.pop(aid, None)


async def spam_loop():
    while True:
        active = [a["text"] for a in alarms.values() if not a["acked"]]
        chat = ctx.chat_get()
        if active and chat:
            text = "🚨🚨🚨 <b>ТРЕБУЕТСЯ ВНИМАНИЕ</b>\n\n" + "\n\n".join(active) + \
                   "\n\n<i>Остановить: кнопка ниже или /stop</i>"
            try:
                await ctx.bot.send_message(chat, text, parse_mode="HTML", reply_markup=STOP_KB)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                print(f"[arb] спам: {e}", flush=True)
            await asyncio.sleep(arb["spam_sec"])
        else:
            await asyncio.sleep(0.5)


# ================= ПЕРСИСТЕНТНОСТЬ =================

def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


async def save():
    if not ctx.redis:
        return
    await asyncio.gather(
        ctx.redis("SET", "arb:config", json.dumps(arb)),
        ctx.redis("SET", "arb:deals", json.dumps(deals, default=_json_default)),
        ctx.redis("SET", "arb:rescues", json.dumps(rescues, default=_json_default)),
        return_exceptions=True,
    )


async def load():
    global deals, rescues
    if not ctx.redis:
        return
    raw_cfg, raw_deal, raw_resc = await asyncio.gather(
        ctx.redis("GET", "arb:config"), ctx.redis("GET", "arb:deals"),
        ctx.redis("GET", "arb:rescues"), return_exceptions=True)
    try:
        if isinstance(raw_cfg, str):
            arb.update(json.loads(raw_cfg))
            for name, n in arb.get("networks", {}).items():
                register_net(name, n["rpc"], n["native"], n.get("aliases", []))
            for name, url in arb.get("rpc_override", {}).items():
                if name in RPC_URLS:
                    RPC_URLS[name] = url
        if isinstance(raw_deal, str):
            deals = json.loads(raw_deal) or {}
        if isinstance(raw_resc, str):
            rescues = json.loads(raw_resc) or []
        if not deals:
            old = await ctx.redis("GET", "arb:deal")  # формат прошлой версии: одна сделка
            if isinstance(old, str) and old not in ("null", ""):
                od = json.loads(old)
                if od and od.get("coin"):
                    deals[od["coin"]] = od
                await ctx.redis("SET", "arb:deal", "null")
    except Exception as e:
        print(f"[arb] не удалось восстановить состояние: {e}", flush=True)


# ================= ПОИСК ВОЗМОЖНОСТИ =================

async def check_opportunity(coin, cfg):
    """Спред HTX→MEXC по лучшим ценам. None, если ниже минимума."""
    sym = f"{coin}USDT"
    hpair = hsym(coin, cfg)
    try:
        (mbids, _), (hbids, hasks) = await asyncio.gather(mexc_depth(sym), htx_depth(hpair))
    except ExchangeError as e:
        # Чтобы из ошибки было видно, по какой именно паре спрашивали каждую биржу.
        raise ExchangeError(f"MEXC {sym} / HTX {hpair}: {e}")
    if not mbids or not hasks:
        return None
    mexc_bid = mbids[0][0]
    max_price = mexc_bid / (1 + D(cfg["pct"]) / 100)
    base = {"mexc_bid": mexc_bid, "max_price": max_price, "asks": hasks, "bids": mbids, "pct": D(cfg["pct"])}
    if hasks[0][0] <= max_price:
        # Продавцы в пределах процента — можно выкупать сразу.
        return dict(base, mode="taker", spread=(mexc_bid - hasks[0][0]) / hasks[0][0] * 100)
    # Своих ордеров на покупку в стакане не держим: покупаем только у продавцов.
    return None


def plan_buy(asks, max_price, budget, bids=None, pct=None):
    """Сколько монет купить по ордерам на продажу HTX не дороже max_price на
    сумму не больше budget. Если переданы bids (стакан покупателей MEXC) и pct,
    каждая порция берётся, только если на MEXC её прямо сейчас покупают с
    выгодой не ниже pct — чтобы не выкупить на HTX больше, чем MEXC реально
    примет по нужной цене. Возвращает (кол-во, стоимость, последняя цена HTX)."""
    qty, cost, last = D(0), D(0), None
    bi = 0
    bid_left = bids[0][1] if bids else D(0)
    for price, level_qty in asks:
        if price > max_price:
            break
        avail = level_qty
        if bids is not None:
            need = price * (1 + pct / 100)
            matched = D(0)
            while bi < len(bids) and bids[bi][0] >= need and matched < avail:
                t = min(bid_left, avail - matched)
                matched += t
                bid_left -= t
                if bid_left <= 0:
                    bi += 1
                    bid_left = bids[bi][1] if bi < len(bids) else D(0)
            avail = matched
        take = min(avail, (budget - cost) / price)
        if take <= 0:
            break
        qty += take
        cost += take * price
        last = price
        if take < level_qty:
            break
    return qty, cost, last


def ladder_price(breakeven, k, step_pct, floor_pct, best_other_ask, tick):
    """Цена лимитки на MEXC на шаге k: безубыток минус k шагов, но не выше, чем
    на тик ниже ближайшего чужого ордера на продажу, и не ниже пола."""
    floor = round_up(breakeven * (1 - D(floor_pct) / 100), tick)
    # Своя ступень округляется вверх (чтобы «безубыток» не оказался на тик в
    # минусе), а цена под чужим продавцом — вниз (чтобы встать строго ниже него).
    price = round_up(breakeven * (1 - D(step_pct) * k / 100), tick)
    if best_other_ask is not None and best_other_ask - tick < price:
        price = round_down(best_other_ask - tick, tick)
    return max(price, floor), floor


_skip_notes = {}


# Почему бот сейчас не покупает монету: {монета: (когда, текст)} — для /arb.
# В чат такие причины пишутся редко (note_once), а тут видна свежая.
why_not = {}
_INFO_KEYS = ("addr1",)  # это не отказ, а пояснение


def _html_to_text(t):
    return re.sub(r"<[^>]+>", "", t)


async def note_once(key, text, every=1800):
    """Сообщение не чаще раза в every секунд на ключ (чтобы не засыпать чат)."""
    now = time.time()
    kind, _, coin = key.partition(":")
    if coin and kind not in _INFO_KEYS:
        why_not[coin] = (now, _html_to_text(text).split("\n")[0])
    elif key == "mexc_perm":
        why_not["*"] = (now, _html_to_text(text).split("\n")[0])
    if now - _skip_notes.get(key, 0) >= every:
        _skip_notes[key] = now
        await notify(text)


engine_state = {"last_pass": 0.0}
PAUSE_SEC = 300
# Продажа по ордерам на покупку MEXC (если цели нет, но покупателей много):
# лимитка по лучшему покупателю, ждём SELL_HOLD_SEC; потом по следующему
# покупателю не ниже −step_pct%; ждём; снимаем и ждём SELL_PAUSE_SEC новых.
SELL_MIN_BIDS = 5      # сколько ордеров на покупку выше пола считать «много»
SELL_HOLD_SEC = 90
SELL_PAUSE_SEC = 30


def coin_paused(coin):
    until = arb.setdefault("paused", {}).get(coin)
    if until and time.time() < until:
        return True
    arb["paused"].pop(coin, None)
    return False

def _ago(ts):
    if not ts:
        return "—"
    sec = int(time.time() - ts)
    return f"{sec} сек" if sec < 120 else f"{sec // 60} мин"


async def engine_loop():
    while True:
        engine_state["last_pass"] = time.time()
        try:
            if arb["enabled"] and arb["coins"]:
                # Все монеты списка проверяются ПАРАЛЛЕЛЬНО: время прохода = одному
                # запросу, а не сумме по монетам. Первой пробуем монету с большим спредом.
                # Сначала — монеты, для которых ещё не найдена настоящая пара на HTX:
                # без этого спред считался бы по чужой монете с тем же тикером.
                for coin, cfg in list(arb["coins"].items()):
                    if not cfg.get("htx_symbol"):
                        try:
                            res = await resolve_coin(coin, cfg)
                            await save()
                            await notify(f"🔎 <b>{coin}</b>: на HTX это <b>{res['htx_ticker']}</b>, пара "
                                         f"<code>{res['htx_pair']}</code> — дальше считаю спред по ней.")
                        except Exception as e:
                            await note_once(f"res:{coin}", f"⚠️ <b>{coin}</b>: не удалось найти монету на HTX: {e}",
                                            every=1800)
                for c in list(arb.get("carry", {})):
                    if c in arb["coins"] and c not in deals and not coin_paused(c):
                        await flush_carry(c, arb["coins"][c])
                for c in arb["coins"]:
                    if coin_paused(c):
                        why_not[c] = (time.time(), "на паузе после /stop аварии — /arb_resume")
                coins = [(c, cfg) for c, cfg in arb["coins"].items()
                         if cfg.get("htx_symbol") and not coin_paused(c) and c not in deals]
                opps = await asyncio.gather(*[check_opportunity(c, cfg) for c, cfg in coins],
                                            return_exceptions=True)
                found = []
                for (coin, cfg), opp in zip(coins, opps):
                    if isinstance(opp, Exception):
                        await note_once(f"chk:{coin}", f"⚠️ <b>{coin}</b>: не удалось проверить спред: <code>{opp}</code>", every=1800)
                    elif opp:
                        found.append((opp["spread"], coin, cfg, opp))
                found.sort(key=lambda x: x[0], reverse=True)
                buyer = next((d for d in deals.values() if d.get("buying")), None)
                if buyer is None:
                    for _, coin, cfg, opp in found:
                        if await try_start(coin, cfg, opp):
                            break
                elif not buyer.get("preempt") and not buyer.get("ioc"):
                    # Покупатель занят другой монетой. Если у этой монеты сейчас реальный
                    # спред (есть продавцы) или он заметно лучше — перехватываем.
                    for spread, coin, cfg, opp in found:
                        if not should_preempt(buyer, opp):
                            break
                        if await try_start(coin, cfg, opp, check_only=True):
                            buyer["preempt"] = (f"переключаюсь на {coin}: спред {spread:.2f}%"
                                                + (" и есть продавцы по нужной цене" if opp["mode"] == "taker" else ""))
                            break
        except Exception as e:
            await note_once("engine_err", f"⚠️ Автоарбитраж: ошибка цикла: <code>{e}</code>", every=600)
            print(f"[arb] engine: {traceback.format_exc()}", flush=True)
        await asyncio.sleep(arb["poll_sec"])


def should_preempt(buyer, opp):
    """Отдать деньги другой монете: покупатель сейчас ничего не выкупает (ждёт
    продавцов), а у другой монеты продавцы по нужной цене есть."""
    return not buyer.get("ioc") and opp["mode"] == "taker"


async def try_start(coin, cfg, opp, check_only=False):
    try:
        res = await resolve_coin(coin, cfg)
        wallet_address(cfg["net"])
    except ContractMismatch as e:
        await note_once(f"ca:{coin}", f"⛔ <b>{coin}</b>: спред {opp['spread']:.2f}%, но {e}\nНе торгую.",
                        every=6 * 3600)
        return False
    except NetworkMissing as e:
        await note_once(f"nonet:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но {e} — пропускаю.",
                        every=3 * 3600)
        return False
    except Exception as e:
        await note_once(f"cfg:{coin}", f"⚠️ <b>{coin}</b>: спред {opp['spread']:.2f}% есть, но сделку начать нельзя:\n{e}")
        return False
    if res["htx_ticker_auto"]:
        # Тикер на HTX только что найден — этот спред считался по ЧУЖОЙ монете
        # с тем же тикером. Сохраняем и пересчитываем на следующем проходе.
        await save()
        await notify(f"🔎 <b>{coin}</b>: на HTX это <b>{res['htx_ticker']}</b>, пара "
                     f"<code>{res['htx_pair']}</code> — пересчитаю спред по ней.")
        return False
    if not res["htx_withdraw_ok"]:
        st = res.get("htx_withdraw_status")
        why = (f"HTX пишет, что вывод в сети {res['htx_chain']} закрыт" if st == "prohibited" or (st == "allowed") else
               f"статус вывода в сети {res['htx_chain']} неизвестен (HTX отдал: {st or 'ничего'})")
        await note_once(f"wd:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но {why} — не покупаю.")
        return False
    if not res["mexc_deposit_ok"]:
        await note_once(f"dep:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но на MEXC закрыт депозит в этой сети — пропускаю.")
        return False
    if not arb["dry_run"]:
        addr = wallet_address(cfg["net"])
        saved, how = await htx_address_status(res["htx_ticker"], res["htx_chain"], addr)
        if saved is None:
            await note_once(f"addr1:{coin}", f"ℹ️ <b>{coin}</b>: адрес бота для вывода с HTX {how}. "
                                             f"Если вывод не пройдёт — продам на HTX по цене MEXC и запомню.",
                            every=6 * 3600)
        if saved is False:
            why = f"адрес бота для вывода с HTX не принят ({how})"
            await note_once(f"book:{coin}", f"⛔ <b>{coin}</b>: спред {opp['spread']:.2f}%, но не покупаю — {why}.\n"
                                            f"{address_book_hint(res['htx_ticker'], res['htx_chain'], addr)}\n"
                                            f"Проверить: /arb_htx {res['htx_ticker']} (раздел 4)",
                            every=1800)
            return False
    if not arb["dry_run"] and not contract_confirmed(cfg, res):
        await note_once(f"unconf:{coin}", f"⚠️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но контракт не сверен.\n"
                                          f"{contract_line(coin, cfg, res)}", every=3 * 3600)
        return False

    if arb["dry_run"] and check_only:
        return False
    if arb["dry_run"]:
        usdt = "?"
        try:
            usdt = fmt((await htx_balance("usdt"))[0])
        except Exception:
            pass
        if opp["mode"] == "taker":
            _, can_cost, _ = plan_buy(opp["asks"], opp["max_price"], D(10) ** 9,
                                      opp["bids"] if arb.get("mexc_depth", True) else None, opp["pct"])
            how = (f"Выкупил бы продавцов на HTX по цене не выше {fmt(opp['max_price'])} — "
                   f"с соблюдением {cfg['pct']}% сейчас на ~{fmt(can_cost)}$")
        else:
            how = f"Продавцов по цене не выше {fmt(opp['max_price'])} нет — ждал бы их"
        await note_once(f"dry:{coin}", (
            f"🧪 <b>ТЕСТ</b> · <b>{coin}</b>: спред {opp['spread']:.2f}% (мин. {cfg['pct']}%)\n"
            f"{how}\nMEXC bid {fmt(opp['mexc_bid'])}, USDT на HTX: {usdt}\n"
            f"{'Проба ' + str(cfg['probe']) + '$, потом весь баланс' if cfg.get('probe') else 'Сразу на весь баланс'}\n"
            f"Сеть: HTX <code>{res['htx_chain']}</code> → кошелёк "
            f"<code>{wallet_address(cfg['net'])}</code> → MEXC "
            f"<code>{res['mexc_net'].get('netWork') or res['mexc_net'].get('network')}</code>\n"
            f"{contract_line(coin, cfg, res)}\n"
            f"<i>Реальные сделки: /arb_live on</i>"), every=300)
        return False

    # Комиссия вывода фиксированная: даже если выкупить на весь баланс, заданный %
    # должен остаться ЧИСТЫМИ, после комиссии вывода. Иначе покупать нет смысла.
    fee_usd = res["htx_fee"] * opp["mexc_bid"]
    try:
        usdt_free = (await htx_balance("usdt"))[0]
    except Exception:
        usdt_free = D(0)
    carry = arb.get("carry", {}).get(coin)
    have = usdt_free * D("0.995") + (D(carry["cost"]) if carry else 0)
    if have < D(arb["batch_usd"]):
        if not check_only:
            await note_once(f"nousdt:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но на HTX свободно только "
                                              f"{fmt(usdt_free)} USDT (деньги в другой сделке или ждут пополнения) — "
                                              f"пропускаю.", every=600)
        return False
    extra = fee_extra_pct(res["htx_fee"], opp["mexc_bid"], have)
    if D(str(opp["spread"])) < D(cfg["pct"]) + extra:
        if not check_only:
            await note_once(f"fee:{coin}", f"⛔ <b>{coin}</b>: спред {opp['spread']:.2f}%, но комиссия вывода с HTX "
                                           f"~{fmt(fee_usd)}$ даже на весь баланс {fmt(have)}$ съест {extra:.2f}% — "
                                           f"чистыми меньше {cfg['pct']}%. Не покупаю.", every=3 * 3600)
        return False
    # Ключ MEXC должен видеть балансы — иначе сделка встанет на первом же шаге.
    try:
        await mexc_free(coin)
    except ExchangeError as e:
        await note_once("mexc_perm", f"⛔ Не начинаю сделки: ключ MEXC не видит баланс — <code>{e}</code>\n"
                                     f"Включи ключу на MEXC право «Аккаунт: просмотр информации об аккаунте» "
                                     f"(и «Спот: торговля», «Кошелёк: просмотр депозитов»).", every=1800)
        return False
    if check_only:
        return True
    d = new_deal(coin, cfg, res)
    d["wd_fee"] = str(res["htx_fee"])
    await _take_carry(d)
    deals[coin] = d
    await save()
    spawn(run_deal(d))
    return True


# ================= СДЕЛКА (поток) =================
#
# Сделка по монете — это поток, а не одна покупка:
#   * покупатель на HTX: выкупает продавцов в пределах процента, а если их нет —
#     держит СВОЙ ордер на покупку первым в стакане (на тик выше лучшего чужого,
#     но не дороже цены, дающей заданный % к MEXC) и переставляет его, как только
#     меняется цена на MEXC или его перебивают; остаток ордера не снимается;
#   * партии: как только куплено на batch_usd (15$) — сразу вывод на кошелёк, ордер
#     при этом продолжает стоять и покупать; через check_sec сек. проверяем, ушла ли
#     партия (свободный баланс монеты на HTX не должен содержать её);
#   * кошелёк: каждую дошедшую партию пересылаем на депозит MEXC;
#   * MEXC: всё зачисленное сразу продаём по ордерам на покупку, пока цена даёт
#     заданный %; остаток — лесенка от безубытка вниз со спамом.
# Все шаги крутятся в одном цикле по очереди — без гонок между ними, и после
# рестарта цикл просто продолжает с сохранённого состояния.

def _dd(d, k):
    return D(d.get(k) or 0)


def _add(d, k, v):
    d[k] = str(_dd(d, k) + D(v))


def new_deal(coin, cfg, res):
    return {
        "v": 2, "id": uuid.uuid4().hex[:8], "coin": coin, "cfg": dict(cfg),
        "started": time.time(), "token": res["token"], "decimals": None,
        "buying": True, "idle_since": None,
        # Проба: сколько ещё $ можно купить, пока первая партия не подтвердилась.
        "probe_left": str(cfg["probe"]) if cfg.get("probe") else None,
        "order": None,                      # стоящий ордер на покупку на HTX
        "ioc": None,                        # «pending» / id ордера-выкупа (защита от двойной покупки)
        "bought_qty": "0", "bought_cost": "0",   # всего куплено на HTX
        "unw_qty": "0", "unw_cost": "0",         # куплено, ещё не выведено
        "batches": [],                      # партии: вывод подан, ждёт проверки / едет
        "qty": "0", "cost": "0",            # успешно выведено (для безубытка)
        "forwarded": "0", "forwarding": False,
        "mexc_baseline": None, "sold_qty": "0", "proceeds": "0",
        "sell": None, "ladder_since": None, "sell_manual": False,
        "last_wallet_check": 0.0, "last_mexc_check": 0.0,
    }


def deal_breakeven(d):
    return _dd(d, "cost") / _dd(d, "qty") if _dd(d, "qty") > 0 else None


async def run_deal(d):
    alarm = {"id": None}
    while True:
        if d.get("abandoned"):
            # По /stop аварии бот «забил» на монету: ордера не трогаем, сделку бросаем.
            if alarm["id"]:
                alarm_end(alarm["id"])
            deals.pop(d["coin"], None)
            await save()
            return
        try:
            if d["mexc_baseline"] is None:
                d["mexc_baseline"] = str(await mexc_free(d["coin"]))
            if d["decimals"] is None:
                d["decimals"] = await wallet_decimals(d["cfg"]["net"], d["token"])
            if d["buying"]:
                await buy_step(d)
            await check_batches(d)
            await transport_step(d)
            await sell_step(d, alarm)
            await save()
            if deal_finished(d):
                break
        except Exception as e:
            print(f"[arb] сделка {d['coin']}: {traceback.format_exc()}", flush=True)
            await note_once(f"deal_err:{d['id']}", f"⚠️ <b>{d['coin']}</b>: ошибка в сделке: <code>{e}</code>\n"
                                                    f"Повторю через 15 сек.", every=300)
            await asyncio.sleep(15)
            continue
        await asyncio.sleep(arb["poll_sec"] if d["buying"] or d["sell"] else 3)
    if alarm["id"]:
        alarm_end(alarm["id"])
    await finish_deal(d)
    deals.pop(d["coin"], None)
    await save()


def deal_finished(d):
    if d["buying"] or d["order"] or d["ioc"] or d["sell"] or d["forwarding"]:
        return False
    if any(not b["ok"] for b in d["batches"]):
        return False
    if d["sell_manual"]:
        return bool(d.get("manual_sold"))  # итог — только когда монеты ушли с MEXC
    if any(not b["fwd"] for b in d["batches"]):
        return False
    # Всё отправленное на MEXC продано (с допуском на комиссии) — сделка закончена.
    return _dd(d, "sold_qty") >= _dd(d, "forwarded") * D("0.97")


# ---------- покупка на HTX ----------

async def _sync_order(d):
    """Учитывает новые исполнения стоящего ордера на покупку."""
    o = d["order"]
    if not o:
        return
    st = await htx_order(o["id"])
    dq, dc = st["filled"] - D(o["filled"]), st["cash"] - D(o["cash"])
    if dq > 0:
        o["filled"], o["cash"] = str(st["filled"]), str(st["cash"])
        _add(d, "unw_qty", dq)
        _add(d, "unw_cost", dc)
        _add(d, "bought_qty", dq)
        _add(d, "bought_cost", dc)
        if d["probe_left"] is not None:
            d["probe_left"] = str(D(d["probe_left"]) - dc)
    if st["state"] in ("filled", "canceled", "partial-canceled"):
        d["order"] = None


async def _cancel_order(d):
    o = d["order"]
    if not o:
        return
    await htx_cancel(o["id"])
    for _ in range(10):
        st = await htx_order(o["id"])
        if st["state"] in ("filled", "canceled", "partial-canceled"):
            break
        await asyncio.sleep(0.5)
    await _sync_order(d)
    d["order"] = None


async def buy_step(d):
    coin, cfg = d["coin"], d["cfg"]
    if d.get("preempt"):
        why = d.pop("preempt")
        await _stop_buying(d, why)
        return
    hpair = hsym(coin, cfg)
    info = await htx_symbol(hpair)
    tick = info["tick"]

    # Дочитываем выкуп, прерванный сбоем, — второй раз не покупаем.
    if d["ioc"]:
        if d["ioc"] == "pending":
            d["ioc"] = None
            d["buying"] = False
            await notify(f"⚠️ <b>{coin}</b>: сбой в момент отправки ордера на покупку — проверь HTX вручную. "
                         f"Дальше бот не покупает.")
            return
        st = await htx_wait_final(d["ioc"])
        _add(d, "unw_qty", st["filled"])
        _add(d, "unw_cost", st["cash"])
        _add(d, "bought_qty", st["filled"])
        _add(d, "bought_cost", st["cash"])
        if d["probe_left"] is not None:
            d["probe_left"] = str(D(d["probe_left"]) - st["cash"])
        d["ioc"] = None

    await _sync_order(d)

    (mbids, _), (hbids, hasks) = await asyncio.gather(mexc_depth(f"{coin}USDT"), htx_depth(hpair))
    if not mbids:
        return
    free_usdt, _ = await htx_balance("usdt")
    o = d["order"]
    locked = (D(o["amount"]) - D(o["filled"])) * D(o["price"]) if o else D(0)
    budget = (free_usdt + locked) * D("0.995")
    if d["probe_left"] is not None:
        budget = min(budget, D(d["probe_left"]))

    # Покупаем только по цене, при которой после комиссии вывода (размазанной на
    # всю партию: уже купленное + оставшиеся деньги) чистыми остаётся заданный %.
    pct = D(cfg["pct"])
    if d.get("wd_fee") is not None:
        pct += fee_extra_pct(D(d["wd_fee"]), mbids[0][0], _dd(d, "unw_cost") + budget)
    max_price = round_down(mbids[0][0] / (1 + pct / 100), tick)

    # Партия набралась и вывод окупается — выводим, покупка продолжается.
    if _worth_withdrawing(d, mbids[0][0]):
        await withdraw_batch(d)
        if not d["buying"]:
            return

    if budget < 5:
        # Деньги кончились. Стоит ордер — ждём его исполнения; идёт проба — ждём,
        # пока подтвердится её вывод (тогда откроется весь баланс); иначе всё.
        if not d["order"] and d["probe_left"] is None:
            await _stop_buying(d, "весь баланс USDT на HTX потрачен")
        return

    # 1) Есть продавцы в пределах процента — выкупаем сразу.
    if hasks and hasks[0][0] <= max_price:
        await _cancel_order(d)
        free_usdt, _ = await htx_balance("usdt")
        budget = free_usdt * D("0.995")
        if d["probe_left"] is not None:
            budget = min(budget, D(d["probe_left"]))
        if arb.get("mexc_depth", True):
            qty, _, last = plan_buy(hasks, max_price, budget, mbids, pct)
            price = min(max_price, round_up(last, tick)) if last is not None else max_price
        else:
            qty, _, _ = plan_buy(hasks, max_price, budget)
            price = max_price
        qty = round_down(qty, info["step"])
        if qty > 0 and qty >= info["min_qty"] and qty * price >= info["min_value"]:
            d["ioc"] = "pending"
            await save()
            try:
                d["ioc"] = await htx_place(hpair, "buy-ioc", qty, price)
            except ExchangeError:
                d["ioc"] = None
                raise
            await save()
            st = await htx_wait_final(d["ioc"])
            d["ioc"] = None
            if st["filled"] > 0:
                _add(d, "unw_qty", st["filled"])
                _add(d, "unw_cost", st["cash"])
                _add(d, "bought_qty", st["filled"])
                _add(d, "bought_cost", st["cash"])
                if d["probe_left"] is not None:
                    d["probe_left"] = str(D(d["probe_left"]) - st["cash"])
                await notify(f"🟢 <b>{coin}</b>: выкупил на HTX {fmt(st['filled'])} шт. на {fmt(st['cash'])}$ "
                             f"(ср. {fmt(st['cash'] / st['filled'])}, MEXC bid {fmt(mbids[0][0])})")
            d["idle_since"] = None
            return

    # 2) Продавцов по цене с нужным % нет — ничего в стакан не ставим, ждём их.
    #    (Ордер, оставшийся от прошлой версии бота, снимаем.)
    if d["order"]:
        await _cancel_order(d)
    await _idle(d, "продавцов по нужной цене нет")


async def _idle(d, why):
    """Нет возможности купить: даём минуту подождать, потом заканчиваем покупку."""
    if d["idle_since"] is None:
        d["idle_since"] = time.time()
    elif time.time() - d["idle_since"] > 60:
        await _stop_buying(d, why)


FEE_NEGLIGIBLE_PCT = D("0.1")  # пока покупка идёт: вывод частями, только если комиссия ≤ 0.1% партии
FEE_SANE_PCT = D("10")          # после покупки: выводим всё, если комиссия не съедает > 10% партии


def _worth_withdrawing(d, mexc_bid, final=False):
    """Процент выгоды проверяется ПРИ ПОКУПКЕ (с учётом комиссии вывода). После покупки
    условия по % нет — монеты выводим, чтобы продать по той выгоде, что есть:
      * пока покупка идёт (final=False) — частями, только если комиссия мизерная (иначе
        платили бы её за каждую мелкую партию, а покупка рассчитана на один вывод);
      * покупка закончилась (final=True) — всё разом, если комиссия не абсурдная
        (≤ FEE_SANE_PCT партии; на HTX остаётся только совсем мелочь)."""
    unw_q, unw_c = _dd(d, "unw_qty"), _dd(d, "unw_cost")
    if unw_q <= 0 or unw_c < D(arb["batch_usd"]):
        return False
    if d.get("wd_fee") is None:  # сделка из старой версии
        return unw_c >= D(d.get("min_batch") or arb["batch_usd"])
    bid = D(str(mexc_bid))
    fee_usd = D(d["wd_fee"]) * (bid if bid > 0 else unw_c / unw_q)
    limit = FEE_SANE_PCT if final else FEE_NEGLIGIBLE_PCT
    return fee_usd <= unw_c * limit / 100


_carry_tried = {}


async def flush_carry(coin, cfg):
    """Отложенные на HTX монеты выводим сами, как только вывод имеет смысл (комиссия
    не съедает больше FEE_SANE_PCT), — не дожидаясь следующей сделки по монете."""
    if time.time() - _carry_tried.get(coin, 0) < 300 or arb["dry_run"]:
        return
    _carry_tried[coin] = time.time()
    try:
        res = await resolve_coin(coin, cfg)
        if not res["htx_withdraw_ok"] or not res["mexc_deposit_ok"]:
            return
        mbids, _ = await mexc_depth(f"{coin}USDT")
        if not mbids:
            return
        d = new_deal(coin, cfg, res)
        d["wd_fee"] = str(res["htx_fee"])
        d["buying"] = False
        await _take_carry(d)
        bid = mbids[0][0]
        if not _worth_withdrawing(d, bid, final=True):
            # комиссия всё ещё заметна — кладём обратно и ждём следующей сделки
            if _dd(d, "unw_qty") > 0:
                arb.setdefault("carry", {})[coin] = {"qty": d["unw_qty"], "cost": d["unw_cost"]}
            return
        await notify(f"📦 <b>{coin}</b>: вывожу отложенные на HTX {fmt(_dd(d, 'unw_qty'))} шт. "
                     f"(куплено на {fmt(_dd(d, 'unw_cost'))}$).")
        deals[coin] = d
        await withdraw_batch(d)
        if _dd(d, "unw_qty") > 0 and not d["batches"]:
            # HTX не принял (меньше минимума вывода и т.п.) — монеты остаются отложенными
            arb.setdefault("carry", {})[coin] = {"qty": d["unw_qty"], "cost": d["unw_cost"]}
            deals.pop(coin, None)
            await save()
            return
        await save()
        spawn(run_deal(d))
    except Exception as e:
        await note_once(f"carry:{coin}", f"⚠️ <b>{coin}</b>: не вывел отложенные монеты: <code>{e}</code>", every=3600)


async def _take_carry(d):
    """Новая сделка забирает монеты, оставшиеся на HTX от прошлой (не больше, чем есть)."""
    c = arb.get("carry", {}).pop(d["coin"], None)
    if not c:
        return
    q, cost = D(c["qty"]), D(c["cost"])
    try:
        free, _ = await htx_balance(hcoin(d["coin"], d["cfg"]))
    except Exception:
        free = q
    if free < q:
        cost, q = (cost * free / q if q > 0 else D(0)), free
    if q > 0:
        _add(d, "unw_qty", q)
        _add(d, "unw_cost", cost)


async def _stop_buying(d, why):
    await _cancel_order(d)
    d["buying"] = False
    unw_q, unw_c = _dd(d, "unw_qty"), _dd(d, "unw_cost")
    if unw_q > 0:
        try:
            mbids, _ = await mexc_depth(f"{d['coin']}USDT")
            mbid = mbids[0][0] if mbids else D(0)
        except Exception:
            mbid = D(0)
        if _worth_withdrawing(d, mbid, final=True):
            await withdraw_batch(d)
        elif unw_c < 1:
            d["unw_qty"] = d["unw_cost"] = "0"  # пыль после округлений — не откладываем
        else:
            # Вывод такой партии съест выгоду — не выводим, копим до следующей сделки.
            arb.setdefault("carry", {})[d["coin"]] = {"qty": str(unw_q), "cost": str(unw_c)}
            d["unw_qty"] = d["unw_cost"] = "0"
            fee = D(d.get("wd_fee") or 0) * mbid
            why_not = (f"меньше партии {fmt(D(arb['batch_usd']))}$" if unw_c < D(arb["batch_usd"]) else
                       f"комиссия вывода ~{fmt(fee)}$ — больше {FEE_SANE_PCT}% от суммы")
            await notify(f"ℹ️ <b>{d['coin']}</b>: покупка закончена ({why}). {fmt(unw_q)} шт. на {fmt(unw_c)}$ "
                         f"не вывожу: {why_not}. Лежат на HTX, следующая сделка по {d['coin']} докупит "
                         f"и выведет всё одной партией.")
            await save()
            return
    if _dd(d, "bought_qty") > 0:
        await notify(f"ℹ️ <b>{d['coin']}</b>: покупка закончена ({why}). Всего куплено "
                     f"{fmt(d['bought_qty'])} шт. на {fmt(d['bought_cost'])}$, дальше довожу до продажи.")


# ---------- вывод партий ----------

async def withdraw_batch(d):
    coin, cfg = d["coin"], d["cfg"]
    res = await resolve_coin(coin, cfg)
    hc = hcoin(coin, cfg)
    free, _ = await htx_balance(hc)
    unw_q, unw_c = _dd(d, "unw_qty"), _dd(d, "unw_cost")
    # Выводим только то, что купила эта сделка (HTX уже удержал из него торговую
    # комиссию монетами), а не весь баланс: чужие монеты без цены покупки
    # исказили бы безубыток. Комиссию вывода HTX берёт сверху суммы.
    base = min(free, unw_q) - res["htx_fee"]
    breakeven = unw_c / unw_q if unw_q > 0 else None
    wid, errors, amount = None, [], D(0)
    to_addr = await htx_book_address(hc, res["htx_chain"], wallet_address(cfg["net"]))
    # HTX часто не отдаёт (или отдаёт неверно) комиссию вывода — при отказе
    # пробуем ещё раз с запасом 1% / 3% / 5%.
    for share in ("1", "0.99", "0.97", "0.95"):
        amount = round_down(base * D(share), res["htx_withdraw_step"])
        if amount <= 0 or amount < res["htx_min_withdraw"]:
            break
        try:
            wid = await htx_withdraw(to_addr, hc, amount, res["htx_chain"], res["htx_fee"])
            break
        except ExchangeError as e:
            errors.append(str(e))
            await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(3)
            now_free, _ = await htx_balance(hc)
            if now_free < free * D("0.1"):
                wid = "unknown"
                break
            raise
    if wid is None:
        if not errors and amount < res["htx_min_withdraw"]:
            return  # меньше минимума HTX — копим дальше
        reason = "HTX отклонил заявку на вывод: " + (errors[-1] if errors else "?")
        if errors and "temp-addr" in errors[-1]:
            reason += "\n" + address_book_hint(hc, res["htx_chain"], wallet_address(cfg["net"]))
            remember_address(wallet_address(cfg["net"]), hc, False)
            await save()
        await _batch_failed(d, breakeven, reason)
        return
    # qty партии — сколько РЕАЛЬНО ушло с HTX: по нему считается безубыток.
    d["batches"].append({"id": str(wid), "at": time.time(), "amount": str(amount),
                         "qty": str(amount), "bought": str(unw_q), "cost": str(unw_c), "ok": False, "fwd": False})
    d["unw_qty"] = d["unw_cost"] = "0"
    await save()
    await notify(f"📤 <b>{coin}</b>: вывожу партию {fmt(amount)} шт. (куплено {fmt(unw_q)} шт. на {fmt(unw_c)}$, "
                 f"остальное — торговая комиссия и комиссия вывода {fmt(res['htx_fee'])} шт.; "
                 f"безубыток {fmt(unw_c / amount)}) "
                 f"в сети {res['htx_chain']}; проверю через {arb['check_sec']} сек."
                 + (" Ордер на покупку продолжает стоять." if d["order"] else ""))


async def _batch_failed(d, breakeven, reason):
    """Вывод не прошёл: покупку прекращаем, монеты на HTX продаём в ноль."""
    await _cancel_order(d)
    d["buying"] = False
    d["unw_qty"] = d["unw_cost"] = "0"
    r = {"id": uuid.uuid4().hex[:8], "coin": hcoin(d["coin"], d["cfg"]), "symbol": hsym(d["coin"], d["cfg"]),
         "mexc_coin": d["coin"], "breakeven": str(breakeven or 0), "reason": reason,
         "order_id": None, "price": None, "created": time.time()}
    rescues.append(r)
    await save()
    spawn(run_rescue(r))


async def check_batches(d):
    pending = [b for b in d["batches"] if not b["ok"]]
    if not pending:
        return
    coin = d["coin"]
    for b in pending:
        if time.time() < b["at"] + arb["check_sec"]:
            continue
        free, frozen = await htx_balance(hcoin(coin, d["cfg"]))
        # Свободно сейчас = новые покупки после вывода + то, что НЕ ушло.
        stuck = free - _dd(d, "unw_qty")
        amount = D(b["amount"])
        be = D(b["cost"]) / D(b["qty"]) if D(b["qty"]) > 0 else D(0)
        if stuck >= amount * D("0.05") and stuck * be >= 1:
            d["batches"].remove(b)
            await _batch_failed(d, be, f"через {arb['check_sec']} сек. партия {fmt(amount)} шт. всё ещё "
                                       f"свободна на HTX — вывод не прошёл")
            return
        b["ok"] = True
        remember_address(wallet_address(d["cfg"]["net"]), hcoin(coin, d["cfg"]), True)
        _add(d, "qty", b["qty"])
        _add(d, "cost", b["cost"])
        d["probe_left"] = None  # первая партия дошла до вывода — проба пройдена
        await notify(f"✅ <b>{coin}</b>: партия {fmt(amount)} шт. ушла с HTX (заморожено {fmt(frozen)}).")


# ---------- кошелёк → MEXC ----------

async def transport_step(d):
    if time.time() - d["last_wallet_check"] < 10:
        return
    d["last_wallet_check"] = time.time()
    waiting = [b for b in d["batches"] if b["ok"] and not b["fwd"]]
    if not waiting and not d["forwarding"]:
        return
    coin, net = d["coin"], d["cfg"]["net"]
    scale = D(10) ** d["decimals"]
    bal = D(await wallet_balance(net, d["token"])) / scale

    if d["forwarding"]:
        # Рестарт посреди пересылки: монет на кошельке почти нет — значит ушли.
        oldest = D(waiting[0]["amount"]) if waiting else D(0)
        if bal < oldest * D("0.1"):
            for b in waiting:
                b["fwd"] = True
                _add(d, "forwarded", b["amount"])
            d["forwarding"] = False
            return
        d["forwarding"] = False

    if bal < D(waiting[0]["amount"]) * D("0.9"):
        if time.time() - waiting[0]["at"] > 1800 and not waiting[0].get("warned"):
            waiting[0]["warned"] = True
            await notify(f"⏳ <b>{coin}</b>: за 30 минут партия так и не пришла на кошелёк "
                         f"<code>{wallet_address(net)}</code> ({NET_TITLES[net]}). Продолжаю ждать.")
        return

    res = await resolve_coin(coin, d["cfg"])
    address, memo = await mexc_deposit_address(coin, res["mexc_net"])
    if memo and net not in MEMO_NETS:
        raise ExchangeError(f"MEXC требует memo для {coin} в сети {NET_TITLES[net]} — эта сеть memo не поддерживает")
    d["forwarding"] = True
    await save()
    tx, sent_raw = await wallet_send_all(net, d["token"], address, memo)
    sent = D(sent_raw) / scale
    d["forwarding"] = False
    # Помечаем партии, которые покрывает эта отправка (могли прийти сразу несколько).
    covered = D(0)
    for b in waiting:
        if covered + D(b["amount"]) * D("0.9") <= sent:
            covered += D(b["amount"])
            b["fwd"] = True
    _add(d, "forwarded", sent)
    await notify(f"🚚 <b>{coin}</b>: отправил {fmt(sent)} шт. на MEXC\ntx: <code>{tx}</code>",
                 kb=None if d["sell_manual"] else InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                     text="✋ Не продавать на MEXC — продам сам", callback_data=f"arbmanual:{d['id']}")]]))


# ---------- продажа на MEXC ----------

async def sell_step(d, alarm):
    if d.pop("manual_request", False) and not d["sell_manual"]:
        # Кнопка «продам сам»: снимаем свою лимитку и больше не продаём.
        if d["sell"]:
            await _sell_cancel(d, f"{d['coin']}USDT")
        d["sell_manual"] = True
        await save()
        await notify(f"✋ <b>{d['coin']}</b>: продажу на MEXC остановил — продаёшь сам. "
                     f"Итог сделки пришлю, когда монеты уйдут с MEXC.")
    if d["sell_manual"]:
        if alarm["id"]:
            alarm_end(alarm["id"])
            alarm["id"] = None
        # Продаёшь сам: ждём, пока монеты сделки уйдут с MEXC, — тогда итог.
        if time.time() - d["last_mexc_check"] >= 30:
            d["last_mexc_check"] = time.time()
            fwd = _dd(d, "forwarded")
            left = await mexc_total(d["coin"]) - D(d["mexc_baseline"])
            if fwd > 0 and left >= fwd * D("0.9"):
                d["manual_seen"] = True  # монеты дошли до MEXC
            elif d.get("manual_seen") and left <= fwd * D("0.03") and not any(
                    not b["fwd"] for b in d["batches"]):
                d["manual_sold"] = True  # и ушли с MEXC — продал
        return
    if _dd(d, "qty") <= 0:
        return
    if not d["sell"] and time.time() - d["last_mexc_check"] < 3:
        return
    d["last_mexc_check"] = time.time()
    coin, cfg = d["coin"], d["cfg"]
    sym = f"{coin}USDT"
    info = await mexc_symbol(sym)
    breakeven = deal_breakeven(d)
    target = breakeven * (1 + D(cfg["pct"]) / 100)

    # Учёт исполнений стоящей лесенки.
    s = d["sell"]
    if s:
        o = await mexc_order(sym, s["id"])
        dq, dc = o["filled"] - D(s["filled"]), o["quote"] - D(s["quote"])
        if dq > 0:
            s["filled"], s["quote"] = str(o["filled"]), str(o["quote"])
            _add(d, "sold_qty", dq)
            _add(d, "proceeds", dc)
        if o["status"] == "FILLED":
            d["sell"] = None
        elif o["status"] in ("CANCELED", "PARTIALLY_CANCELED") and not s.get("self_cancel"):
            d["sell"] = None
            d["sell_manual"] = True
            await notify(f"ℹ️ <b>{coin}</b>: ордер на продажу на MEXC отменён вручную — дальше продаёшь сам. "
                         f"Итог сделки пришлю, когда монеты будут проданы.")
            return

    free = await mexc_free(coin)
    new = round_down(free - D(d["mexc_baseline"]), info["step"])
    enough = lambda q, p: q > 0 and q * p >= info["min_value"]

    # 1) Новое зачисление — сразу по ордерам на покупку, пока держится процент.
    if enough(new, breakeven):
        bids, _ = await mexc_depth(sym)
        if bids and bids[0][0] >= target:
            price = round_up(target, info["tick"])
            oid = await mexc_place(sym, "SELL", "IMMEDIATE_OR_CANCEL", new, price)
            o = await mexc_wait_final(sym, oid)
            _add(d, "sold_qty", o["filled"])
            _add(d, "proceeds", o["quote"])
            new -= o["filled"]
            if o["filled"] > 0:
                await notify(f"💰 <b>{coin}</b>: продано на MEXC {fmt(o['filled'])} шт. на {fmt(o['quote'])}$ "
                             f"(≥ {fmt(price)}, цель +{cfg['pct']}%).")

    s = d["sell"]
    has_more = enough(new, breakeven)
    now = time.time()
    if not s and not has_more:
        if d["ladder_since"] is not None or d.get("sell_pause_until"):
            d["ladder_since"] = None
            d["sell_pause_until"] = None
            if alarm["id"]:
                alarm_end(alarm["id"])
                alarm["id"] = None
        return

    # 2) Цели нет. Стакан не давим: лимитка никогда не ниже лучшего покупателя,
    #    так что за раз исполняется только он, а остаток стоит и ждёт новых.
    bids, asks = await mexc_depth(sym)
    floor = round_up(breakeven * (1 - D(str(arb["floor_pct"])) / 100), info["tick"])
    live = [p for p, q in bids if p >= floor]
    many = len(live) >= SELL_MIN_BIDS

    async def replace(price, **meta):
        qty = new
        if d["sell"]:
            await _sell_cancel(d, sym)
            qty = round_down(await mexc_free(coin) - D(d["mexc_baseline"]), info["step"])
        if not enough(qty, price):
            return False
        oid = await mexc_place(sym, "SELL", "LIMIT", qty, price)
        d["sell"] = dict({"id": oid, "price": str(price), "qty": str(qty), "filled": "0", "quote": "0",
                          "placed": now}, **meta)
        await save()
        return True

    if many:
        # 2а) Покупателей много (пусть и мелких): продаём по их ценам.
        d["ladder_since"] = None
        hold = SELL_HOLD_SEC
        if s and s.get("mode") == "bid":
            if now - s.get("placed", now) < hold:
                if has_more:  # доехали ещё монеты — ставим их туда же
                    await replace(D(s["price"]), mode="bid", stage=s.get("stage", 0), placed=s.get("placed"))
                return
            if s.get("stage", 0) == 0:
                # Следующий актуальный покупатель, но не дешевле −step_pct% от нашей цены.
                lim = max(D(s["price"]) * (1 - D(str(arb["step_pct"])) / 100), floor)
                nxt = [p for p in live if p >= lim]
                if nxt and await replace(nxt[0], mode="bid", stage=1):
                    await _sell_alarm(d, alarm, target, breakeven, floor)
                    return
            # Постояли на обеих ценах — снимаем и ждём, пока появятся покупатели.
            await _sell_cancel(d, sym)
            d["sell_pause_until"] = now + SELL_PAUSE_SEC
            await save()
            return
        if s:  # стояла лесенка — переходим на продажу по покупателям
            await _sell_cancel(d, sym)
            new = round_down(await mexc_free(coin) - D(d["mexc_baseline"]), info["step"])
        if now < (d.get("sell_pause_until") or 0):
            return
        d["sell_pause_until"] = None
        if await replace(live[0], mode="bid", stage=0):
            await _sell_alarm(d, alarm, target, breakeven, floor)
        return

    # 2б) Покупателей мало: лесенка от безубытка, −step_pct% каждые step_sec до пола,
    #     но тоже не ниже лучшего покупателя (иначе ордер съест весь стакан).
    if now < (d.get("sell_pause_until") or 0):
        return
    d["sell_pause_until"] = None
    if s and s.get("mode") == "bid":
        await _sell_cancel(d, sym)
        s = None
        new = round_down(await mexc_free(coin) - D(d["mexc_baseline"]), info["step"])
        has_more = enough(new, breakeven)
    if d["ladder_since"] is None:
        d["ladder_since"] = now
    k = int((now - d["ladder_since"]) // arb["step_sec"])
    my_price = D(s["price"]) if s else None
    others = [p for p, q in asks if p != my_price]
    price, _ = ladder_price(breakeven, k, arb["step_pct"], arb["floor_pct"],
                            others[0] if others else None, info["tick"])
    if bids and price < bids[0][0]:
        price = bids[0][0]
    if s and D(s["price"]) == price and not has_more:
        return
    if await replace(price, mode="ladder"):
        await _sell_alarm(d, alarm, target, breakeven, floor)


async def _sell_cancel(d, sym):
    """Снимает свою лимитку продажи на MEXC и учитывает, что по ней успело продаться."""
    s = d["sell"]
    if not s:
        return
    s["self_cancel"] = True
    await save()  # чтобы после рестарта своя отмена не выглядела ручной
    await mexc_cancel(sym, s["id"])
    o = await mexc_wait_final(sym, s["id"], timeout=5)
    _add(d, "sold_qty", o["filled"] - D(s["filled"]))
    _add(d, "proceeds", o["quote"] - D(s["quote"]))
    d["sell"] = None


async def _sell_alarm(d, alarm, target, breakeven, floor):
    s = d["sell"]
    price = D(s["price"])
    how = ("по ордерам на покупку" + (" (следующий покупатель)" if s.get("stage") else "")
           if s.get("mode") == "bid" else "лесенкой")
    text = (f"<b>{d['coin']}</b>: не удалось продать на MEXC с плановой выгодой (цель {fmt(target)}). "
            f"Продаю {how}: лимитка {fmt(D(s['qty']))} шт. по {fmt(price)} "
            f"({(price / breakeven - 1) * 100:+.2f}% к безубытку {fmt(breakeven)})"
            + (" — это пол, ниже не опускаю." if price <= floor else "."))
    if alarm["id"] is None:
        alarm["id"] = alarm_start(text)
    else:
        alarm_update(alarm["id"], text)


async def finish_deal(d):
    cost = _dd(d, "cost")
    if _dd(d, "qty") <= 0:
        return  # ничего не вывели (или вывод не прошёл — тогда итог даст аварийная продажа)
    # Продано — по истории сделок MEXC (учитывает и ручные продажи).
    try:
        sold, proceeds = await mexc_sold_since(f"{d['coin']}USDT", d["started"])
        fwd = _dd(d, "forwarded")
        if sold > fwd > 0:  # продано больше, чем пришло по сделке: берём долю сделки
            sold, proceeds = fwd, proceeds * fwd / sold
    except Exception:
        sold, proceeds = _dd(d, "sold_qty"), _dd(d, "proceeds")
    if sold <= 0:
        await notify(f"ℹ️ <b>{d['coin']}</b>: монеты сделки ушли с MEXC, но продаж в истории MEXC не видно — "
                     f"итог не считаю. Куплено на HTX за {fmt(cost)}$.")
        return
    d["sold_qty"] = str(sold)
    pnl = proceeds - cost
    hist = arb.setdefault("history", [])
    hist.append({"t": time.time(), "coin": d["coin"], "cost": str(cost), "proceeds": str(proceeds)})
    del hist[:-500]
    await notify(
        f"🏁 <b>{d['coin']}</b>: сделка завершена.\n"
        f"Куплено на HTX: {fmt(sum(D(b.get('bought', b['qty'])) for b in d['batches'] if b['ok']))} шт. "
        f"за {fmt(cost)}$\n"
        f"Выведено с HTX (после комиссий): {fmt(d['qty'])} шт., дошло до MEXC: {fmt(d['forwarded'])} шт.\n"
        f"Продано на MEXC: {fmt(d['sold_qty'])} шт. за {fmt(proceeds)}$\n"
        f"Итог: <b>{pnl:+.2f}$</b> ({(pnl / cost * 100 if cost else 0):+.2f}%)")


# ---------- аварийная продажа на HTX ----------

# Аварийная продажа на HTX, если вывод не прошёл: (сек. от начала, скидка от цены
# MEXC в %). Со скидкой продаём, только если цена всё ещё ≥ закупки + RESCUE_MIN_PROFIT%;
# после последней ступени — в безубыток.
RESCUE_STEPS = [(0, 0), (300, 1.5), (480, 2.0), (780, 2.5)]
RESCUE_BREAKEVEN_AFTER = 1080
RESCUE_MIN_PROFIT = 1.0


def rescue_price(mexc_bid, breakeven, elapsed, tick):
    """Цена аварийной продажи на HTX: (цена, описание ступени)."""
    if breakeven > 0 and elapsed >= RESCUE_BREAKEVEN_AFTER:
        return round_up(breakeven, tick), "в безубыток"
    disc = 0
    for start, pct in RESCUE_STEPS:
        if elapsed >= start:
            disc = pct
    price = round_down(mexc_bid * (1 - D(disc) / 100), tick)
    if disc and breakeven > 0:
        min_price = round_up(breakeven * (1 + D(RESCUE_MIN_PROFIT) / 100), tick)
        if price < min_price:
            return min_price, f"MEXC −{disc:g}% ниже закупки +{RESCUE_MIN_PROFIT:g}% — держу закупка +{RESCUE_MIN_PROFIT:g}%"
    return price, ("= цена покупки на MEXC" if not disc else f"= MEXC −{disc:g}%")


async def run_rescue(r):
    """Вывод не прошёл: продаём монету на HTX вслед за ценой покупки на MEXC —
    5 мин. по цене MEXC, потом 3 мин. −1,5%, 5 мин. −2%, 5 мин. −2,5% (но не ниже
    закупки +1%), дальше — в безубыток. Спам — сразу. Если спам
    остановили (/stop или кнопка) — бот «забивает» на монету: ордер не трогает и
    не торгует ею PAUSE_SEC сек. или до /arb_resume."""
    coin, sym = r["coin"], r.get("symbol") or f"{r['coin']}usdt"
    mcoin = r.get("mexc_coin") or coin
    aid = None
    try:
        info = await htx_symbol(sym)
        free, _ = await htx_balance(coin)
        aid = alarm_start(f"<b>{mcoin}</b>: куплено {fmt(free)} шт. на HTX, но вывод не прошёл: {r['reason']}\n"
                          f"Продаю на HTX по цене MEXC.", pause_coin=mcoin)
        while True:
            if alarms.get(aid, {}).get("acked"):
                return  # спам остановлен — «забили» на монету, ордер оставляем как есть
            if r["order_id"]:
                o = await htx_order(r["order_id"])
                if o["state"] == "filled":
                    await notify(f"✅ <b>{mcoin}</b>: монеты, которые не удалось вывести, проданы на HTX: "
                                 f"{fmt(o['filled'])} шт. на {fmt(o['cash'])}$.")
                    return
                if o["state"] in ("canceled", "partial-canceled") and not r.get("self_cancel"):
                    await notify(f"ℹ️ <b>{mcoin}</b>: ордер на продажу на HTX отменён вручную "
                                 f"(продано {fmt(o['filled'])} шт.). Дальше — вручную.")
                    return
            # Цена продажи = текущая лучшая цена покупки на MEXC.
            bids, _ = await mexc_depth(f"{mcoin}USDT")
            if not bids:
                await asyncio.sleep(5)
                continue
            price, stage = rescue_price(bids[0][0], D(r.get("breakeven") or 0),
                                        time.time() - r["created"], info["tick"])
            if price != (D(r["price"]) if r["price"] else None):
                if r["order_id"]:
                    r["self_cancel"] = True
                    await htx_cancel(r["order_id"])
                    await htx_wait_final(r["order_id"], timeout=5)
                    r["self_cancel"] = False
                free, _ = await htx_balance(coin)
                qty = round_down(free, info["step"])
                if qty <= 0 or qty * price < info["min_value"]:
                    await notify(f"✅ <b>{mcoin}</b>: на HTX больше нечего продавать.")
                    return
                r["order_id"] = await htx_place(sym, "sell-limit", qty, price)
                r["price"] = str(price)
                await save()
                alarm_update(aid, f"<b>{mcoin}</b>: куплено {fmt(qty)} шт. на HTX, но вывод не прошёл: {r['reason']}\n"
                                  f"Ордер на продажу на HTX: {fmt(qty)} шт. по {fmt(price)} ({stage}), "
                                  f"закупка {fmt(D(r.get('breakeven') or 0))}. Переставляю вслед за MEXC.")
            await asyncio.sleep(5)
    except Exception as e:
        print(f"[arb] rescue {coin}: {traceback.format_exc()}", flush=True)
        text = (f"<b>{mcoin}</b>: вывод с HTX не прошёл, и продать на HTX не получилось: <code>{e}</code>. "
                f"Нужны ручные действия!")
        if aid is None or aid not in alarms:
            aid = alarm_start(text, pause_coin=mcoin)
        else:
            alarm_update(aid, text)
        while aid in alarms and not alarms[aid]["acked"]:
            await asyncio.sleep(1)
    finally:
        if aid:
            alarm_end(aid)
        if r in rescues:
            rescues.remove(r)
        await save()


# ================= ПОИСК НОД (RPC) =================
#
# EVM: открытый реестр сетей Chainlist (ethereum-lists) — там у каждой сети
# список публичных RPC. Cosmos Hub: официальный Cosmos Chain Registry (REST).
# Каждую ноду проверяем: отвечает ли, та ли сеть (chain id), высота блока, пинг.

CHAINLIST_URL = "https://chainid.network/chains.json"
EVM_CHAIN_IDS = {"eth": 1, "bsc": 56, "monad": 143}
_chainlist = {"ts": 0.0, "data": []}


async def chainlist():
    if time.time() - _chainlist["ts"] > 86400 or not _chainlist["data"]:
        async with http().get(CHAINLIST_URL, timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json(content_type=None)
        _chainlist.update(ts=time.time(), data=data if isinstance(data, list) else [])
    return _chainlist["data"]


_TESTNET_RE = re.compile(r"test|devnet|rinkeby|goerli|sepolia|holesky|hoodi|kovan|ropsten|fuji|mumbai|amoy|"
                         r"chiado|makalu|staging", re.I)


async def pick_chain_for_coin(cands, coin):
    """Из нескольких похожих сетей выбирает ту, где живёт монета с MEXC:
      * токен — его контракт с MEXC реально есть в сети (eth_getCode через её ноду);
      * иначе — название сети совпадает с названием сети монеты на MEXC
        («Arbitrum One» ↔ «Arbitrum One(ARB)»), а для родной монеты — газ сети.
    Возвращает сеть или None, если однозначно выбрать нельзя."""
    nets = await mexc_networks(coin)
    contracts = {(n.get("contract") or "").strip() for n in nets}
    contracts = [c for c in contracts if re.fullmatch(r"0x[0-9a-fA-F]{40}", c)]

    async def has_contract(chain):
        cid = chain.get("chainId")
        for u in evm_rpc_candidates(chain)[:5]:
            if not (await probe_evm(u, cid))["ok"]:
                continue
            for ca in contracts:
                try:
                    code = await _rpc_once(u, "?", "eth_getCode", [ca, "latest"])
                except Exception:
                    continue
                if code and code not in ("0x", "0x0"):
                    return True
            return False
        return False

    if contracts:
        found = await asyncio.gather(*[has_contract(c) for c in cands])
        hits = [c for c, ok in zip(cands, found) if ok]
        if len(hits) == 1:
            return hits[0]
        cands = hits or cands
    squash = lambda x: re.sub(r"[^a-z0-9]", "", str(x or "").lower())
    mexc_names = [squash(n.get("name")) + " " + squash(n.get("netWork")) + " " + squash(n.get("network"))
                  for n in nets]
    by_name = [c for c in cands if squash(c.get("name")) and any(squash(c.get("name")) in m for m in mexc_names)]
    if len(by_name) == 1:
        return by_name[0]
    native = [c for c in cands if str((c.get("nativeCurrency") or {}).get("symbol", "")).upper() == coin.upper()]
    return native[0] if len(native) == 1 else None


async def find_evm_chain(query, chain_id=None):
    """Сеть из Chainlist по chain id или по названию/символу монеты газа. Тестнеты
    пропускаем. Возвращает (сеть, похожие) — сеть None, если однозначно не нашлась."""
    q = str(query or "").lower()
    chains = [c for c in await chainlist() if not _TESTNET_RE.search(str(c.get("name", "")))]
    if chain_id is not None:
        hit = [c for c in chains if c.get("chainId") == chain_id]
        return (hit[0] if hit else None), hit
    # По убыванию точности: у тестнета поле chain часто то же («MAPO»), что у основной.
    exact = [c for c in chains if str(c.get("shortName", "")).lower() == q]
    if not exact:
        exact = [c for c in chains if str(c.get("name", "")).lower() == q]
    if not exact:
        exact = [c for c in chains if str(c.get("chain", "")).lower() == q]
    if not exact:
        exact = [c for c in chains if str((c.get("nativeCurrency") or {}).get("symbol", "")).lower() == q]
    if not exact:  # «map» → «MAP Protocol» (целое слово), а не «MAPO Makalu»
        exact = [c for c in chains if q in re.findall(r"[a-z0-9]+", str(c.get("name", "")).lower())]
    if not exact:
        exact = [c for c in chains if re.sub(r"[^a-z0-9]", "", str(c.get("name", "")).lower()) == q]
    if not exact:
        exact = [c for c in chains if str(c.get("name", "")).lower().startswith(q)]
    return (exact[0] if len(exact) == 1 else None), exact


def evm_rpc_candidates(chain):
    """Публичные https-RPC из реестра (без тех, что требуют API-ключ)."""
    return [u for u in chain.get("rpc", []) if u.startswith("https://") and "${" not in u and "{" not in u]


async def probe_evm(url, expect_chain_id=None):
    t0 = time.time()
    try:
        body = [{"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                {"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []}]
        async with http().post(url, json=body, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json(content_type=None)
        res = {x.get("id"): x.get("result") for x in data} if isinstance(data, list) else {}
        cid, block = int(res[1], 16), int(res[2], 16)
        ok = expect_chain_id is None or cid == expect_chain_id
        return {"url": url, "ok": ok, "chain_id": cid, "block": block, "ms": int((time.time() - t0) * 1000),
                "err": None if ok else f"другая сеть (chain id {cid})"}
    except Exception as e:
        return {"url": url, "ok": False, "err": str(e)[:80] or type(e).__name__, "ms": None}


async def discover_network(query, coin=None):
    """Находит EVM-сеть по названию сети или монеты газа и добавляет её с
    проверенными нодами. Защита от «не той» ноды:
      1) сеть в реестре Chainlist должна найтись однозначно (тестнеты отброшены);
      2) каждая нода сама должна вернуть тот же chain id, что в реестре;
      3) ноды должны сходиться по высоте блока (отстающие/чужие отбрасываются);
      4) сверка с биржей: для токена его контракт с MEXC должен существовать в
         этой сети (eth_getCode), для родной монеты — совпасть монета газа.
    Возвращает код сети или бросает ExchangeError с понятной причиной."""
    q = re.sub(r"[^a-z0-9_]", "", str(query).lower())
    try:
        chain, similar = await find_evm_chain(q)
    except Exception as e:
        raise ExchangeError(f"реестр сетей недоступен: {e}")
    picked = False
    if not chain and similar and coin:
        try:
            chain = await pick_chain_for_coin(similar[:8], coin)
            picked = chain is not None
        except Exception:
            chain = None
    if not chain:
        if similar:
            opts = "; ".join(f"{c.get('name')} (газ {c.get('nativeCurrency', {}).get('symbol')})" for c in similar[:6])
            raise ExchangeError(f"под «{query}» подходит несколько сетей: {opts}. Напиши название целиком, "
                                f"можно с пробелом, например «{similar[0].get('name')}»")
        raise ExchangeError(f"сеть «{q}» не нашлась в реестре EVM-сетей")
    cid = chain.get("chainId")
    native = str((chain.get("nativeCurrency") or {}).get("symbol") or "").upper()
    probes = await asyncio.gather(*[probe_evm(u, cid) for u in evm_rpc_candidates(chain)[:12]])
    good = [p for p in probes if p["ok"]]
    top = max((p["block"] for p in good), default=0)
    good = sorted([p for p in good if p["block"] >= top - 50], key=lambda p: p["ms"])
    if not good:
        raise ExchangeError(f"нашёл сеть {chain.get('name')}, но ни одна её публичная нода не ответила правильно")

    # Выбрали из нескольких похожих — называем сеть по ней самой («arbitrumone»), а не по запросу.
    name = re.sub(r"[^a-z0-9]", "", str(chain.get("name", q)).lower()) if picked else q
    if name in NET_TITLES:
        name = re.sub(r"[^a-z0-9_]", "", str(chain.get("shortName", name)).lower())
    aliases = sorted({a.upper() for a in (q, chain.get("chain"), chain.get("shortName"), native) if a})
    RPC_URLS[name] = good[0]["url"]
    # Сверка с биржей: та ли это сеть, где живёт монета.
    if coin:
        coin = coin.upper()
        if coin != native:
            nets = await mexc_networks(coin)
            register_net(name, good[0]["url"], native, aliases)
            mx = [n for n in nets if matches_net(name, n.get("netWork"), n.get("network"))]
            token = (mx[0].get("contract") or "").strip() if mx else ""
            if not token:
                unregister_net(name)
                raise ExchangeError(f"у {coin} на MEXC нет сети {chain.get('name')} с контрактом — не добавляю")
            code = await rpc(name, "eth_getCode", [token, "latest"])
            if not code or code in ("0x", "0x0"):
                unregister_net(name)
                raise ExchangeError(f"контракт {coin} с MEXC ({token}) не найден в сети {chain.get('name')} — "
                                    f"значит, это не та сеть, не добавляю")
    arb.setdefault("networks", {})[name] = {"rpc": good[0]["url"], "rpcs": [p["url"] for p in good[:5]],
                                            "native": native, "aliases": aliases, "chain_id": cid}
    register_net(name, good[0]["url"], native, aliases)
    NET_TITLES[name] = chain.get("name") or name.upper()
    await save()
    return name, chain, good


# ================= КОМАНДЫ =================

def _is_admin(user_id):
    return user_id in ADMIN_IDS


async def _guard(message: types.Message):
    if not ADMIN_IDS:
        await message.answer(
            "🔒 Автоарбитраж выключен: не задана переменная окружения <code>ARB_ADMIN_IDS</code>.\n"
            f"Твой Telegram id: <code>{message.from_user.id}</code> — впиши его туда.",
            parse_mode="HTML")
        return False
    if not _is_admin(message.from_user.id):
        await message.answer("🔒 Нет доступа.")
        return False
    ctx.chat_set(message.chat.id)
    return True


def _safe_addr(net):
    try:
        return f"<code>{wallet_address(net)}</code>"
    except Exception as e:
        return f"не задан ({e})"


def _keys_text():
    def mark(ok):
        return "✅" if ok else "❌"
    evm, sui = "не задан", "не задан"
    try:
        evm = f"<code>{evm_account().address}</code>" if EVM_PRIVATE_KEY else evm
    except Exception as e:
        evm = f"ошибка ключа: {e}"
    try:
        sui = f"<code>{sui_keys()[2]}</code>" if SUI_PRIVATE_KEY else sui
    except Exception as e:
        sui = f"ошибка ключа: {e}"
    try:
        cosmos = f"<code>{cosmos_keys()[2]}</code>" if (COSMOS_PRIVATE_KEY or EVM_PRIVATE_KEY) else "не задан"
    except Exception as e:
        cosmos = f"ошибка ключа: {e}"
    return (f"{mark(HTX_API_KEY and HTX_API_SECRET)} ключ HTX · "
            f"{mark(MEXC_API_KEY and MEXC_API_SECRET)} ключ MEXC\n"
            f"👛 EVM (ETH/BNB/Monad): {evm}\n"
            f"👛 Sui: {sui}\n"
            f"👛 Cosmos Hub: {cosmos}\n"
            f"👛 Solana: {_safe_addr('sol')}\n"
            f"👛 Tron: {_safe_addr('trx')}")


@router.message(Command("arb"))
async def cmd_arb(message: types.Message):
    """Всё об автоарбитраже одной командой: живой спред по каждой монете,
    текущая сделка, балансы, тревоги и настройки."""
    if not await _guard(message):
        return
    lines = [
        "🤖 <b>Автоарбитраж HTX → кошелёк → MEXC</b>",
        f"{'▶️ ВКЛ' if arb['enabled'] else '⏸ ВЫКЛ'} · "
        f"{'🧪 тест' if arb['dry_run'] else '💸 реальные сделки'} · "
        f"цикл был {_ago(engine_state['last_pass'])} назад (каждые {arb['poll_sec']:g} сек)",
    ]

    # --- Спреды сейчас (стаканы тянем заново, параллельно по всем монетам) ---
    async def spread_now(coin, cfg):
        if not cfg.get("htx_symbol"):
            return coin, cfg, None, "пара на HTX ещё не найдена"
        try:
            (mbids, _), (_, hasks) = await asyncio.gather(mexc_depth(f"{coin}USDT"), htx_depth(hsym(coin, cfg)))
            if not mbids or not hasks:
                return coin, cfg, None, "пустой стакан"
            return coin, cfg, (mbids[0][0] - hasks[0][0]) / hasks[0][0] * 100, None
        except Exception as e:
            return coin, cfg, None, str(e)[:80]

    lines.append("\n<b>Спред HTX→MEXC сейчас:</b>")
    if not arb["coins"]:
        lines.append("— список пуст (/arb_add)")
    for coin, cfg, sp, err in await asyncio.gather(*[spread_now(c, cfg) for c, cfg in arb["coins"].items()]):
        pair = f" ({cfg['htx_coin']} на HTX)" if cfg.get("htx_coin") and cfg["htx_coin"] != coin else ""
        if sp is None:
            lines.append(f"• <b>{coin}</b>{pair}: ⚠️ {err}")
        else:
            mark = "🟢" if sp >= cfg["pct"] else "⚪"
            lines.append(f"• {mark} <b>{coin}</b>{pair}: <b>{sp:+.2f}%</b> (порог {cfg['pct']}%, "
                         f"{NET_TITLES.get(cfg['net'], cfg['net'])}"
                         + (f", проба {cfg['probe']:g}$" if cfg.get("probe") else "") + ")")
        # Почему не покупал в последние 10 минут (причины, которые в чат приходят редко).
        for key in (coin, "*"):
            w = why_not.get(key)
            if w and time.time() - w[0] < 600 and coin not in deals:
                lines.append(f"   ↳ не купил {_ago(w[0])} назад: {html.escape(w[1][:300])}")

    # --- Сделка ---
    lines.append("\n<b>Сделки:</b>")
    if not deals:
        lines.append("нет — ждёт спред" if arb["enabled"] else "нет")
    for d in list(deals.values()):
        lines.append(f"\n<b>{d['coin']}</b> · идёт {_ago(d.get('started'))}")
        o = d.get("order")
        if o:
            lines.append(f"📌 ордер на покупку HTX: {fmt(o['amount'])} шт. по {fmt(o['price'])}, "
                         f"исполнено {fmt(o['filled'])}")
        elif d.get("buying"):
            lines.append("покупка: ждёт возможности" + (f" (проба, осталось {fmt(d['probe_left'])}$)"
                                                        if d.get("probe_left") is not None else ""))
        else:
            lines.append("покупка закончена")
        lines.append(f"куплено всего: {fmt(d['bought_qty'])} шт. на {fmt(d['bought_cost'])}$; "
                     f"ещё не выведено {fmt(d['unw_qty'])} шт.")
        checking = [b for b in d["batches"] if not b["ok"]]
        on_way = [b for b in d["batches"] if b["ok"] and not b["fwd"]]
        if checking or on_way:
            lines.append(f"партии: на проверке вывода {len(checking)}, идут на кошелёк {len(on_way)}")
        be = deal_breakeven(d)
        if be:
            lines.append(f"выведено {fmt(d['qty'])} шт. (безубыток {fmt(be)}), на MEXC отправлено {fmt(d['forwarded'])}")
        if _dd(d, "sold_qty") > 0 or d.get("sell"):
            lines.append(f"продано на MEXC: {fmt(d['sold_qty'])} шт. на {fmt(d['proceeds'])}$"
                         + (f"; лесенка {fmt(d['sell']['qty'])} шт. по {fmt(d['sell']['price'])}" if d.get("sell") else ""))
    for c in [c for c, v in arb.get("carry", {}).items() if D(v["cost"]) < 1]:
        arb["carry"].pop(c)  # пыль — не показываем и не храним
    for c, v in arb.get("carry", {}).items():
        lines.append(f"📦 {c}: на HTX лежит {fmt(D(v['qty']))} шт. (куплено на {fmt(D(v['cost']))}$) — "
                     f"вывод пока не окупается, уйдёт со следующей сделкой")
    now_paused = {c: u for c, u in arb.get("paused", {}).items() if u > time.time()}
    if now_paused:
        lines.append("⏸ На паузе: " + ", ".join(f"{c} (ещё {int((u - time.time()) // 60) + 1} мин.)"
                                                for c, u in now_paused.items()) + " — /arb_resume")
    if rescues:
        lines.append(f"⚠️ Аварийных продаж на HTX: {len(rescues)} ({', '.join(r['coin'] for r in rescues)})")
    active_alarms = [a for a in alarms.values() if not a["acked"]]
    if alarms:
        lines.append(f"🚨 Тревог: {len(alarms)} (спамит: {len(active_alarms)})")

    # --- Балансы ---
    lines.append("\n<b>Балансы:</b>")
    try:
        lines.append(f"HTX: {fmt((await htx_balance('usdt'))[0])} USDT")
    except Exception as e:
        lines.append(f"HTX: ошибка — {str(e)[:80]}")
    try:
        lines.append(f"MEXC: {fmt(await mexc_free('USDT'))} USDT")
    except Exception as e:
        lines.append(f"MEXC: ошибка — {str(e)[:80]}")

    # --- Настройки ---
    lines += [
        "",
        "<b>Настройки:</b>",
        f"продажа на MEXC без цели: если покупателей ≥{SELL_MIN_BIDS} — по их ценам "
        f"({SELL_HOLD_SEC} сек. на лучшем, потом на следующем не ниже −{arb['step_pct']}%, "
        f"пауза {SELL_PAUSE_SEC} сек.); иначе лесенка −{arb['step_pct']}% каждые {arb['step_sec']} сек.; "
        f"пол −{arb['floor_pct']}% от безубытка, ниже лучшего покупателя не ставит",
        f"проверка вывода HTX через {arb['check_sec']} сек. · стакан MEXC при покупке: "
        f"{'учитывается' if arb.get('mexc_depth', True) else 'только лучшая цена'}",
        f"покупка на HTX только у продавцов по цене с твоим %, своих ордеров в стакане нет · "
        f"твой % проверяется при покупке (с комиссией вывода); после покупки — вывод без условий по %: "
        f"во время покупки частями, если комиссия ≤{FEE_NEGLIGIBLE_PCT}%, после — всё разом (от "
        f"{arb.get('batch_usd', 15):g}$, если комиссия ≤{FEE_SANE_PCT}%)",
        (f"пополнение HTX: держу ≥{arb.get('topup_usd'):g} USDT (BNB / Arbitrum), проверка раз в минуту; "
         + (f"последняя {_ago(topup_status['t'])} назад: " if topup_status["t"] else "")
         + html.escape(topup_status["text"])
         if arb.get("topup_usd") else "пополнение HTX с кошелька: выкл"),
        "",
        _keys_text(),
        "",
        "Команды: /arb_help",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")


# ================= СТАТИСТИКА (/arb_stats) =================
#
# Считаем по истории САМИХ бирж, а не по памяти бота: так попадают и ручные
# покупки/продажи, и всё, что бот мог «потерять» при перезапуске.
#
# Учёт — единый по монете на ОБЕИХ биржах (купил на HTX, продал на MEXC или наоборот —
# неважно где): покупка добавляет монеты, продажа и комиссии их списывают, перевод
# между биржами (HTX → кошелёк → MEXC) — лишь перемещение, только его потери (комиссия
# вывода, газ) списываются как расход. Каждая продажа списывает последние купленные
# до неё монеты (арбитраж продаёт то, что только что купил; старые запасы не трогает).
# Затем сверка: по каждой монете куплено = продано + комиссии + остаток, а остаток
# по истории сверяется с реальными балансами бирж — так видно, если что-то не учтено.

STATS_DAYS = 40   # история грузится с запасом: продажам в начале месяца нужны их покупки
STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDE", "USD1", "PYUSD", "USDD", "UST", "HUSD"}

_hist_lock = asyncio.Lock()
_hist_next = {"t": 0.0, "gap": 0.6}  # следующий запрос истории не раньше t; пауза gap подстраивается
_hist_cache = {}          # "htx|пара|начало|конец" → строки; только окна, закончившиеся давно
_hist_loaded = {"ok": False}
_hist_progress = {"done": 0, "total": 0}
_HIST_FIELDS = ("id", "created-at", "type", "filled-amount", "price", "filled-fees", "fee-currency")


def _is_rate_limit(e):
    t = str(e).lower()
    return "rate" in t or "too many" in t or "429" in t


async def _hist_req(fn, *args):
    """Запрос истории через общую очередь: не чаще раза в gap сек. При отказе
    «слишком часто» замолкает ВСЯ очередь (а не один запрос) и ждёт всё дольше —
    лимит у биржи общий с торговлей, долбить его бесполезно."""
    for attempt in range(10):
        async with _hist_lock:
            wait = _hist_next["t"] - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            _hist_next["t"] = time.time() + _hist_next["gap"]
        try:
            res = await fn(*args)
            _hist_next["gap"] = max(0.6, _hist_next["gap"] * 0.97)  # проходит — понемногу ускоряемся
            return res
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            if attempt >= 4:
                raise ExchangeError(f"сеть: {type(e).__name__} {e}".strip())
            await asyncio.sleep(2 * (attempt + 1))  # обрыв/таймаут — повторяем
            continue
        except ExchangeError as e:
            if not _is_rate_limit(e) or attempt == 9:
                raise
            pause = min(5 * (attempt + 1), 30)
            _hist_next["t"] = max(_hist_next["t"], time.time() + pause)
            _hist_next["gap"] = min(_hist_next["gap"] * 1.3, 5.0)  # биржа против — дальше реже


async def _hist_cache_load():
    if _hist_loaded["ok"] or not ctx.redis:
        return
    _hist_loaded["ok"] = True
    try:
        raw = await ctx.redis("GET", "arb:hist")
        if raw:
            _hist_cache.update(json.loads(raw))
    except Exception:
        pass


async def _hist_cache_save():
    if not ctx.redis:
        return
    try:
        await ctx.redis("SET", "arb:hist", json.dumps(_hist_cache))
    except Exception:
        pass


# ---------- загрузка истории ----------

async def htx_trades(symbol, since, until):
    """Все исполнения по паре на HTX за [since, until] (сек.). HTX отдаёт окнами по 48 ч."""
    out, seen = [], set()
    # Окна выравниваем по сетке 48 ч — тогда прошедшие окна одинаковые от запуска к запуску
    # и их можно не качать заново.
    step = 48 * 3600
    t = since - since % step
    while t < until:
        end = t + step  # окна встык (без зазора), повторы отсеиваем по id
        key = f"htx|{symbol}|{int(t)}|{int(end)}"
        _hist_progress["done"] += 1
        if key in _hist_cache:
            rows_all = _hist_cache[key]
        else:
            rows_all = await _htx_window(symbol, t, end)
            if end < time.time() - 600:  # окно закончилось — больше не изменится
                _hist_cache[key] = [{k: r.get(k) for k in _HIST_FIELDS} for r in rows_all]
        for r in rows_all:
            if r.get("id") not in seen and since <= r.get("created-at", 0) / 1000 <= until:
                seen.add(r.get("id"))
                out.append(r)
        t = end
    return out


async def _htx_window(symbol, t, end):
    """Одно окно истории HTX (до 48 ч) со всеми страницами по 500."""
    out, seen = [], set()
    # Окно [t, end) в мс: следующее начинается ровно там, где кончилось это, — без пропусков.
    params = {"symbol": symbol, "start-time": int(t * 1000), "end-time": int(end * 1000) - 1, "size": 500}
    while True:
        rows = await _hist_req(htx_req, "GET", "/v1/order/matchresults", params) or []
        new = [r for r in rows if r.get("id") not in seen]
        for r in new:
            seen.add(r.get("id"))
        out += new
        if len(rows) < 500 or not new:
            break
        params["from"] = min(r["id"] for r in rows)
        params["direct"] = "prev"
    return out


async def mexc_trades(sym, since, until):
    """Все исполнения по паре на MEXC за [since, until] (сек.), окнами по 7 дней."""
    out, seen = [], set()
    t = since
    while t < until:
        end = min(t + 7 * 86400, until)
        start = int(t * 1000)
        while True:
            rows = await _hist_req(mexc_req, "GET", "/api/v3/myTrades", {"symbol": sym, "startTime": start,
                                                                         "endTime": int(end * 1000), "limit": 100}) or []
            new = [r for r in rows if (r.get("id"), r.get("orderId")) not in seen]
            for r in new:
                seen.add((r.get("id"), r.get("orderId")))
            out += new
            if len(rows) < 100 or not new:
                break
            start = max(int(r["time"]) for r in rows)  # та же мс могла не влезть — повторы отсеем
        t = end
    return out


_HTX_BAD_STATES = {"canceled", "reject", "wallet-reject", "repealed", "failed", "confirm-error", "orphan",
                   "unknown", "verify-reject"}


async def htx_transfers(since, currencies=()):
    """Депозиты и выводы HTX с момента since: общий запрос по всем монетам плюс
    отдельно по каждой из currencies — общий HTX отдаёт не всегда целиком.
    Повторы (одна запись из обоих запросов) отсеиваем по id."""
    seen, out = set(), []

    async def query(kind, currency):
        frm = None
        while True:
            params = {"type": kind, "size": 500, "direct": "prev"}
            if currency:
                params["currency"] = currency.lower()
            if frm:
                params["from"] = frm
            rows = await _hist_req(htx_req, "GET", "/v1/query/deposit-withdraw", params) or []
            for r in rows:
                t = r.get("created-at", 0) / 1000
                key = (kind, r.get("id"))
                if key in seen or t < since or str(r.get("state", "")).lower() in _HTX_BAD_STATES:
                    continue
                seen.add(key)
                amt, fee = D(str(r.get("amount") or 0)), D(str(r.get("fee") or 0))
                # С HTX списывается сумма + комиссия сверху (так выводит и бот).
                out.append({"ex": "HTX", "kind": kind, "asset": str(r.get("currency", "")).upper(), "t": t,
                            "amount": amt + fee if kind == "withdraw" else amt,
                            "fee": fee if kind == "withdraw" else D(0)})
            if len(rows) < 500 or not rows or min(r.get("created-at", 0) for r in rows) / 1000 < since:
                break
            frm = min(r["id"] for r in rows) - 1

    for kind in ("withdraw", "deposit"):
        try:
            await query(kind, None)
        except ExchangeError:
            pass  # без currency HTX может и отказать — тогда только по монетам
        for c in currencies:
            try:
                await query(kind, c)
            except ExchangeError:
                pass  # «currency not open» и т.п. — монеты нет на HTX, у неё и выводов нет
    return out


async def mexc_transfers(since, until):
    """Все депозиты и выводы MEXC (по всем монетам) за [since, until], окнами по 7 дней."""
    out = []
    t = since
    while t < until:
        end = min(t + 7 * 86400, until)
        p = {"startTime": int(t * 1000), "endTime": int(end * 1000), "limit": 1000}
        for r in await _hist_req(mexc_req, "GET", "/api/v3/capital/deposit/hisrec", dict(p)) or []:
            if int(r.get("status", 0)) == 5:  # 5 — зачислен
                out.append({"ex": "MEXC", "kind": "deposit", "asset": str(r.get("coin", "")).upper(),
                            "t": int(r.get("insertTime", 0)) / 1000, "amount": D(str(r.get("amount") or 0))})
        for r in await _hist_req(mexc_req, "GET", "/api/v3/capital/withdraw/history", dict(p)) or []:
            if int(r.get("status", 0)) not in (8, 9):  # 8 — ошибка, 9 — отменён
                # На MEXC комиссия вывода входит в сумму: списывается amount.
                out.append({"ex": "MEXC", "kind": "withdraw", "asset": str(r.get("coin", "")).upper(),
                            "t": int(r.get("applyTime", 0)) / 1000, "amount": D(str(r.get("amount") or 0)),
                            "fee": D(str(r.get("transactionFee") or 0))})
        t = end
    return out


async def htx_all_balances():
    acc = await htx_account_id()
    data = await htx_req("GET", f"/v1/account/accounts/{acc}/balance")
    out = {}
    for item in (data or {}).get("list", []):
        v = D(str(item.get("balance") or 0))
        if v > 0:
            c = str(item.get("currency", "")).upper()
            out[c] = out.get(c, D(0)) + v
    return out


async def mexc_all_balances():
    data = await mexc_req("GET", "/api/v3/account")
    out = {}
    for b in data.get("balances", []):
        v = D(str(b.get("free") or 0)) + D(str(b.get("locked") or 0))
        if v > 0:
            out[str(b.get("asset", "")).upper()] = v
    return out


async def htx_recent_assets():
    """Монеты, которыми торговали на HTX за последние 48 ч (по всем парам сразу)."""
    now = time.time()
    rows = await htx_req("GET", "/v1/order/history", {"start-time": int((now - 48 * 3600 + 60) * 1000),
                                                      "end-time": int(now * 1000), "size": 1000}) or []
    out = set()
    for r in rows:
        sym = str(r.get("symbol", "")).lower()
        if sym.endswith("usdt") and float(r.get("field-amount") or r.get("filled-amount") or 0) > 0:
            out.add(sym[:-4].upper())
    return out


async def track_assets_loop():
    """Раз в час запоминает монеты, которыми торговали на HTX, — чтобы /arb_stats не
    пропустил ручные сделки по монетам вне автоарбитража (HTX по всем парам сразу
    отдаёт историю только за 48 ч)."""
    while True:
        try:
            htx2asset = {cfg["htx_coin"].upper(): c.upper() for c, cfg in arb["coins"].items() if cfg.get("htx_coin")}
            found = {htx2asset.get(a, a) for a in await htx_recent_assets()}
            known = arb.setdefault("stats_coins", {})
            new = [a for a in found if a not in known and a not in STABLES]
            for a in new:
                known[a] = None
            if new:
                await save()
        except Exception as e:
            print(f"[arb] track assets: {e}", flush=True)
        await asyncio.sleep(3600)


# ---------- учёт ----------

def _trade_events(asset, htx_rows, htx_coin, mexc_rows):
    """Сделки обеих бирж в едином виде: (время, биржа, сторона, монет, $, комиссия монетами)."""
    ev = []
    for r in htx_rows:
        q, p = D(str(r["filled-amount"])), D(str(r["price"]))
        fee, fc = D(str(r.get("filled-fees") or 0)), str(r.get("fee-currency") or "").upper()
        side = "buy" if str(r.get("type", "")).startswith("buy") else "sell"
        usd, coin_fee = q * p, D(0)
        if fc == "USDT":
            usd = usd + fee if side == "buy" else usd - fee
        elif fc == htx_coin.upper():
            coin_fee = fee
        ev.append((r["created-at"] / 1000, "HTX", side, q, usd, coin_fee))
    for r in mexc_rows:
        q, usd = D(str(r["qty"])), D(str(r["quoteQty"]))
        fee, fc = D(str(r.get("commission") or 0)), str(r.get("commissionAsset") or "").upper()
        side = "buy" if r.get("isBuyer") else "sell"
        coin_fee = D(0)
        if fc == "USDT":
            usd = usd + fee if side == "buy" else usd - fee
        elif fc == asset.upper():
            coin_fee = fee
        ev.append((int(r["time"]) / 1000, "MEXC", side, q, usd, coin_fee))
    return ev


def asset_ledger(trades, transfer_ops):
    """Единый учёт монеты на обеих биржах. Партии: [кол-во, цена|None, время].
    Покупка → партия (кол-во уже без комиссии монетами). Продажа, комиссия, вывод
    «насовсем» списывают ПОСЛЕДНИЕ партии до этого момента; если партий нет — это
    монеты, бывшие до начала истории («старые запасы», цена неизвестна).
    Возвращает (списания, партии-остаток, сколько списано из старых запасов)."""
    items = []
    for t, ex, side, q, usd, coin_fee in trades:
        if side == "buy":
            items.append((t, 0, {"kind": "buy", "ex": ex, "q": q - coin_fee, "gross": q, "usd": usd}))
        else:
            items.append((t, 2, {"kind": "sell", "ex": ex, "q": q, "usd": usd}))
            if coin_fee > 0:
                items.append((t, 3, {"kind": "fee", "q": coin_fee, "why": "торговая комиссия монетой"}))
    for o in transfer_ops:
        if o["kind"] == "in":
            items.append((o["t"], 1, {"kind": "in", "q": o["q"]}))
        elif o["kind"] in ("fee", "out"):
            items.append((o["t"], 3, dict(o)))
    items.sort(key=lambda x: (x[0], x[1]))
    lots, uses, opening = [], [], D(0)
    for t, _, o in items:
        if o["kind"] in ("buy", "in"):
            if o["q"] > 0:
                lots.append([o["q"], (o["usd"] / o["q"]) if o["kind"] == "buy" else None, t])
            continue
        need = o["q"]
        while need > 0 and lots:
            lot = lots[-1]
            k = min(need, lot[0])
            uses.append({"t": t, "kind": o["kind"], "ex": o.get("ex"), "q": k, "lot_t": lot[2],
                         "cost": None if lot[1] is None else k * lot[1],
                         "usd": o["usd"] * k / o["q"] if o["kind"] == "sell" else D(0)})
            lot[0] -= k
            need -= k
            if lot[0] <= 0:
                lots.pop()
        if need > 0:
            opening += need
            uses.append({"t": t, "kind": o["kind"], "ex": o.get("ex"), "q": need, "lot_t": None, "cost": None,
                         "usd": o["usd"] * need / o["q"] if o["kind"] == "sell" else D(0)})
    return uses, lots, opening


def asset_period(trades, uses, since):
    """Итог монеты за период + сверка количества купленного за период."""
    r = {"buy_q": D(0), "buy_usd": D(0), "buy_n": 0, "sell_q": D(0), "sell_usd": D(0), "sell_n": 0,
         "buy_ex": set(), "sell_ex": set(), "pnl": D(0), "cost": D(0), "fee_cost": D(0),
         "unm_q": D(0), "unm_usd": D(0), "early_q": D(0),
         # куда делось купленное ЗА ПЕРИОД (по количеству):
         "p_bought": D(0), "p_sold": D(0), "p_fee": D(0), "p_out": D(0)}
    for t, ex, side, q, usd, coin_fee in trades:
        if t < since:
            continue
        k = "buy" if side == "buy" else "sell"
        r[k + "_q"] += q
        r[k + "_usd"] += usd
        r[k + "_n"] += 1
        r[k + "_ex"].add(ex)
        if side == "buy":
            r["p_bought"] += q - coin_fee
    for u in uses:
        if u["t"] >= since:
            if u["kind"] == "sell":
                if u["cost"] is None:
                    r["unm_q"] += u["q"]
                    r["unm_usd"] += u["usd"]
                else:
                    r["pnl"] += u["usd"] - u["cost"]
                    r["cost"] += u["cost"]
                    if u["lot_t"] < since:
                        r["early_q"] += u["q"]
            elif u["kind"] == "fee" and u["cost"] is not None:
                r["pnl"] -= u["cost"]
                r["fee_cost"] += u["cost"]
        if u["lot_t"] is not None and u["lot_t"] >= since and u["cost"] is not None:
            r["p_" + {"sell": "sold", "fee": "fee", "out": "out"}[u["kind"]]] += u["q"]
    r["p_left"] = r["p_bought"] - r["p_sold"] - r["p_fee"] - r["p_out"]
    return r


def _money(x):
    return f"{x:,.2f}".replace(",", " ")


def _qty(x):
    return f"{x:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


@router.message(Command("arb_stats"))
async def cmd_stats(message: types.Message):
    """Сделки и прибыль за сутки / неделю / месяц по истории бирж — по всем монетам."""
    if not await _guard(message):
        return
    wait = await message.answer("⏳ Собираю историю сделок, депозитов и выводов HTX и MEXC по всем монетам… "
                                "Первый раз — несколько минут (биржи ограничивают частоту запросов), дальше быстрее.")
    now = time.time()
    since_all = now - STATS_DAYS * 86400
    periods = [("Сутки", now - 86400), ("Неделя", now - 7 * 86400), ("Месяц", now - 30 * 86400)]
    await _hist_cache_load()
    problems = []

    # --- какие монеты: всё, что встречается на биржах ---
    htx2asset = {}  # тикер на HTX → тикер на MEXC (MONAD → MON)
    for c, cfg in arb["coins"].items():
        if cfg.get("htx_coin"):
            htx2asset[cfg["htx_coin"].upper()] = c.upper()
    asset2htx = {v: k for k, v in htx2asset.items()}

    async def safe(name, coro):
        try:
            return await coro
        except Exception as e:
            problems.append(f"{name}: {(str(e) or type(e).__name__)[:100]}")
            return None

    mexc_tr, hb, mb, recent = await asyncio.gather(
        safe("MEXC депозиты/выводы", mexc_transfers(since_all, now)),
        safe("HTX балансы", htx_all_balances()), safe("MEXC балансы", mexc_all_balances()),
        safe("HTX сделки за 48 ч", htx_recent_assets()))
    norm = lambda a: htx2asset.get(str(a).upper(), str(a).upper())  # PROPY → PRO, MONAD → MON
    balances = {}
    for c, v in (hb or {}).items():
        balances[norm(c)] = balances.get(norm(c), D(0)) + v
    for c, v in (mb or {}).items():
        balances[c] = balances.get(c, D(0)) + v
    known = arb.setdefault("stats_coins", {})
    for k in [k for k in known if norm(k) != k.upper()]:
        known.pop(k)  # тикер HTX у монеты с другим тикером на MEXC — это та же монета
    assets = {norm(c) for c in arb["coins"]} | {norm(c) for c in known}
    assets |= {norm(x["asset"]) for x in (mexc_tr or [])} | set(balances) | {norm(c) for c in (recent or set())}
    assets = {a for a in assets if a and a not in STABLES}
    htx_tr = await safe("HTX депозиты/выводы",
                        htx_transfers(since_all, sorted(asset2htx.get(a, a) for a in assets)))
    transfers = []
    for x in (htx_tr or []) + (mexc_tr or []):
        x["asset"] = norm(x["asset"]) if x["ex"] == "HTX" else x["asset"]
        transfers.append(x)
    assets |= {x["asset"] for x in transfers if x["asset"] not in STABLES}
    assets = sorted(a for a in assets if a)
    for a in assets:
        known.setdefault(a, None)

    step = 48 * 3600
    _hist_progress.update(done=0, total=len(assets) * (int((now - (since_all - since_all % step)) // step) + 1))

    async def load(asset):
        hc = asset2htx.get(asset, asset)
        hsymb = (arb["coins"].get(asset) or {}).get("htx_symbol")
        if not hsymb:
            try:
                hsymb = await htx_usdt_pair(hc)
            except Exception as e:
                problems.append(f"{asset}: не узнал пару на HTX ({type(e).__name__} {str(e)[:60]})")
                return asset, None
        hrows, mrows = [], []
        if hsymb:
            hrows = await safe(f"{asset} HTX сделки", htx_trades(hsymb, since_all, now))
            if hrows is None:
                return asset, None
        try:
            mrows = await mexc_trades(f"{asset}USDT", since_all, now)
        except ExchangeError as e:
            if "symbol" not in str(e).lower():  # пары нет на MEXC — просто нет сделок
                problems.append(f"{asset} MEXC сделки: {str(e)[:100]}")
                return asset, None
        return asset, _trade_events(asset, hrows, hc, mrows)

    async def progress():
        last = ""
        while True:
            await asyncio.sleep(10)
            txt = (f"⏳ Собираю историю… монет: {len(assets)}, загружено {_hist_progress['done']} из "
                   f"~{_hist_progress['total']} кусков истории HTX. Прошлые дни запоминаются — повторно быстро.")
            if txt != last:
                last = txt
                try:
                    await wait.edit_text(txt)
                except Exception:
                    pass

    prog = asyncio.ensure_future(progress())
    try:
        loaded = await asyncio.gather(*[load(a) for a in assets])
    finally:
        prog.cancel()
    await _hist_cache_save()

    books = {}
    for asset, trades in loaded:
        if trades is None:
            continue
        # Переводы между биржами монет не меняют — учитываем только комиссии вывода (монетами).
        tops = [{"t": x["t"], "kind": "fee", "q": x["fee"]} for x in transfers
                if x["asset"] == asset and x["kind"] == "withdraw" and x.get("fee", 0) > 0]
        if not trades:
            continue
        uses, lots, opening = asset_ledger(trades, tops)
        books[asset] = (trades, tops, uses, lots, opening)

    blocks = ["📊 <b>Итоги по всем сделкам HTX и MEXC</b>"]
    if problems:
        blocks.append("⚠️ <b>Не удалось получить</b> (итог может быть неполным): " + "; ".join(problems))
    for title, since in periods:
        rows, tot = [], {"pnl": D(0), "cost": D(0), "buy_usd": D(0), "sell_usd": D(0)}
        for asset, (trades, tops, uses, lots, opening) in books.items():
            r = asset_period(trades, uses, since)
            if not (r["buy_n"] or r["sell_n"]):
                continue
            for k in ("pnl", "cost", "buy_usd", "sell_usd"):
                tot[k] += r[k]
            pct = f" ({r['pnl'] / r['cost'] * 100:+.2f}%)" if r["cost"] else ""
            line = (f"• <b>{asset}</b> <b>{r['pnl']:+.2f}$</b>{pct}: куплено {_qty(r['buy_q'])} шт. за "
                    f"{_money(r['buy_usd'])}$, продано {_qty(r['sell_q'])} шт. за {_money(r['sell_usd'])}$")
            notes = []
            if r["unm_q"] > r["sell_q"] * D("0.001"):
                notes.append(f"⚠️ {_qty(r['unm_q'])} шт. продано без покупки за {STATS_DAYS} дн. (старые) — не в прибыли")
            if r["p_left"] > r["p_bought"] * D("0.002") and r["buy_q"]:
                notes.append(f"⏳ не продано {_qty(r['p_left'])} шт.")
            if notes:
                line += "\n   " + " · ".join(notes)
            rows.append((abs(r["pnl"]), line))
        block = f"\n📅 <b>{title}</b>"
        if not rows:
            blocks.append(block + ": сделок не было")
            continue
        pct = f" ({tot['pnl'] / tot['cost'] * 100:+.2f}%)" if tot["cost"] > 0 else ""
        block += (f": прибыль <b>{tot['pnl']:+.2f}$</b>{pct} · куплено на {_money(tot['buy_usd'])}$ · "
                  f"продано на {_money(tot['sell_usd'])}$")
        blocks.append(block)
        blocks += [line for _, line in sorted(rows, key=lambda x: -x[0])]
    blocks.append("\n<i>Продажа (на любой бирже) списывает купленные до неё монеты по их цене, комиссия "
                  "вывода — расход. Пары к USDT.</i>")

    # Телеграм — до 4096 символов в сообщении: режем по блокам.
    msgs, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 1 > 3900:
            msgs.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + b
    if cur:
        msgs.append(cur)
    try:
        await wait.edit_text(msgs[0], parse_mode="HTML")
    except Exception:
        await message.answer(msgs[0], parse_mode="HTML")
    for m in msgs[1:]:
        await message.answer(m, parse_mode="HTML")


@router.message(Command("arb_list"))
async def cmd_list(message: types.Message):
    """Монеты в автоарбитраже с настройками — мгновенно, без запросов к биржам."""
    if not await _guard(message):
        return
    if not arb["coins"]:
        await message.answer("Список автоарбитража пуст. Добавить: /arb_add PEPE 3 bsc")
        return
    lines = [f"📋 <b>Монеты в автоарбитраже ({len(arb['coins'])})</b> · "
             f"{'▶️ ВКЛ' if arb['enabled'] else '⏸ ВЫКЛ'} · {'🧪 тест' if arb['dry_run'] else '💸 реальные сделки'}", ""]
    for coin, c in sorted(arb["coins"].items()):
        net = c["net"]
        flags = []
        if c.get("htx_coin") and c["htx_coin"] != coin:
            flags.append(f"на HTX {c['htx_coin']}")
        if c.get("probe"):
            flags.append(f"проба {c['probe']:g}$")
        if coin_paused(coin):
            flags.append(f"⏸ пауза ещё {int((arb['paused'][coin] - time.time()) // 60) + 1} мин.")
        if coin in deals:
            flags.append("🔄 покупает" if deals[coin].get("buying") else "🔄 доводит сделку")
        try:
            key = _addr_key(wallet_address(net))
            if coin.upper() in arb.get("addr_bad", {}).get(key, []) or \
                    (c.get("htx_coin") or "").upper() in arb.get("addr_bad", {}).get(key, []):
                flags.append("⛔ HTX отклонил адрес")
        except Exception:
            pass
        lines.append(f"• <b>{coin}</b> — от {c['pct']:g}% · {NET_TITLES.get(net, net)}"
                     + (f" · {', '.join(flags)}" if flags else ""))
        lines.append(f"   <code>/arb_add {coin} {c['pct']:g} {net}{' ' + format(c['probe'], 'g') if c.get('probe') else ''}</code>")
    lines.append("\nИзменить — отправь строку с новым процентом · убрать: /arb_del МОНЕТА · спред сейчас: /arb")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("arb_help"))
async def cmd_help(message: types.Message):
    if not await _guard(message):
        return
    await message.answer(
        "📖 <b>Команды автоарбитража</b>\n"
        "/arb — статус: спред по монетам сейчас, сделка, балансы, настройки\n"
        "/arb_add PEPE 3 bsc — торговать PEPE от 3% в сети BNB, сразу на весь баланс\n"
        "/arb_add PEPE 3 bsc 150 — то же, но сперва проба на 150$, после успешного вывода — весь баланс\n"
        "/arb_add ATOM 2 — сеть можно не писать, если монета родная для сети (ATOM, SUI, MON, BNB, ETH…)\n"
        f"   сети: {nets_list_text()} (свои EVM-сети — /arb_net)\n"
        "   если бот не нашёл сеть сам: добавь <code>htx=код</code> и/или <code>mexc=имя</code> (см. /arb_chains)\n"
        "   если тикер на HTX другой: <code>htxcoin=ТИКЕР</code>, напр. /arb_add MON 1.5 monad htxcoin=MONAD\n"
        "/arb_del PEPE — убрать монету\n"
        "/arb_list — список монет в автоарбитраже\n"
        "/arb_stats — сделки и прибыль за сутки / неделю / месяц (по истории HTX и MEXC)\n"
        "/arb_confirm PEPE — подтвердить контракт вручную, если HTX его не отдал\n"
        "/arb_htx PEPE — что HTX реально отдаёт: сети, статусы, комиссии, контракты, лимиты, адресная книга\n"
        "/arb_chains PEPE — сети монеты на HTX и MEXC и что выбрал бот\n"
        "/arb_on · /arb_off — включить/выключить автоторговлю\n"
        "/arb_live on · /arb_live off — реальные сделки / тестовый режим\n"
        "/arb_set step 0.3 · interval 120 · floor 1.2 · check 60 · poll 1 — параметры\n"
        "/arb_set depth on|off — учитывать стакан MEXC при покупке (по умолчанию on)\n"
        "/arb_set batch 15 — с какой суммы купленного сразу выводить партию\n"
        "/arb_set topup 600 — держать на HTX не меньше 600 USDT: недостающее бот сам переводит с кошелька "
        "из сетей BNB / Arbitrum (0 — выкл)\n"
        "/arb_wallet — адреса и балансы кошельков бота\n"
        "/arb_net add mapo https://rpc.maplabs.io MAPO — добавить любую EVM-сеть "
        "(имя, адрес ноды, монета на газ; можно ещё названия сети на биржах через запятую: MAPO,MAP)\n"
        "/arb_net — список сетей · /arb_net del mapo — удалить свою сеть\n"

        "/arb_reset — забыть зависшую сделку (после ручного разбора)\n"
        "/stop — остановить спам (при аварии вывода — ещё и пауза по монете на 5 мин.)\n"
        "/arb_resume PEPE — снять эту паузу раньше\n\n"
        "<b>Как идёт сделка</b>\n"
        "1. Покупка на HTX: если есть продавцы по цене, дающей твой % к MEXC, — выкупает их сразу. "
        "Своих ордеров на покупку в стакане не держит: покупает ордером «исполнить сразу» точно по ценам "
        "продавцов и не больше, чем MEXC сейчас заберёт с твоим %.\n"
        "2. Твой % проверяется при покупке (с учётом комиссии вывода). Купленное выводится на кошелёк бота "
        "без условий по %: во время покупки — частями, если комиссия мизерная, после — всё разом. "
        "HTX отклонил вывод или через минуту партия всё ещё на балансе — покупка останавливается, монеты "
        "продаются на HTX вслед за ценой MEXC, спам до /stop.\n"
        "3. Каждая дошедшая партия пересылается на депозит MEXC.\n"
        "4. На MEXC: сразу по ордерам на покупку, пока выгода ≥ твоего %. Иначе — по ценам покупателей, "
        "не давя стакан (или лесенкой от безубытка), не ниже −1,2%; спам до /stop или до продажи.\n"
        "5. Минуту нет продавцов по нужной цене — покупка заканчивается, сделка доводится до продажи.",
        parse_mode="HTML")


@router.message(Command("arb_add"))
async def cmd_add(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    args = (command.args or "").split()
    try:
        coin = re.sub(r"[^A-Z0-9]", "", args[0].upper())
        if coin.endswith("USDT"):
            coin = coin[:-4]
        pct = abs(float(args[1].replace(",", ".")))
        rest = args[2:]
        # Сеть можно не писать, если монета — родная монета сети и называется так
        # же (ATOM → atom, SUI → sui, MON → monad, MAPO → mapo…): /arb_add ATOM 2
        net = parse_net(rest[0]) if rest else None
        if net:
            rest = rest[1:]
        elif rest and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,20}", rest[0]) and "=" not in rest[0]:
            # Незнакомая сеть (можно в несколько слов: «Arbitrum One») — ищем её сами
            # и сверяем с контрактом монеты на MEXC.
            words = []
            while rest and re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", rest[0]) and "=" not in rest[0]:
                words.append(rest.pop(0))
            query = " ".join(words)
            try:
                net, chain, _ = await discover_network(query, coin)
            except ExchangeError as e:
                await message.answer(f"❌ Сеть «{query}»: {e}")
                return
            await message.answer(f"🔎 Добавил сеть <b>{net}</b>: {chain.get('name')} (chain id {chain.get('chainId')}), "
                                 f"ноду нашёл и проверил сам.", parse_mode="HTML")
        else:
            net = net_for_coin(coin)
            if not net:
                # Может, это родная монета ещё не добавленной сети (MAPO, AVAX…).
                try:
                    net, chain, _ = await discover_network(coin, coin)
                    await message.answer(f"🔎 Добавил сеть <b>{net}</b>: {chain.get('name')} "
                                         f"(chain id {chain.get('chainId')}), ноду нашёл и проверил сам.",
                                         parse_mode="HTML")
                except ExchangeError:
                    await message.answer(
                        f"❌ Для <b>{coin}</b> укажи сеть: /arb_add {coin} {pct:g} bsc\n"
                        f"Сети: {nets_list_text()} — или любое название EVM-сети, бот найдёт её сам.",
                        parse_mode="HTML")
                    return
        if not coin or pct <= 0:
            raise ValueError
        cfg = {"pct": pct, "net": net, "probe": 0, "htx_chain": None, "mexc_net": None}
        for a in rest:
            if a.lower().startswith("htxcoin="):
                cfg["htx_coin"] = re.sub(r"[^A-Z0-9]", "", a[8:].upper()) or None
                cfg["htx_coin_manual"] = bool(cfg["htx_coin"])
            elif a.lower().startswith("htx="):
                cfg["htx_chain"] = a[4:]
            elif a.lower().startswith("mexc="):
                cfg["mexc_net"] = a[5:]
            else:
                cfg["probe"] = abs(float(a.replace(",", ".")))
    except Exception:
        await message.answer("❌ Пример: /arb_add PEPE 3 bsc  ·  /arb_add PEPE 3 bsc 150  ·  /arb_add ATOM 2\n"
                             f"Сети: {nets_list_text()}", parse_mode="HTML")
        return
    await message.answer(await add_coin(coin, cfg), parse_mode="HTML")


async def add_coin(coin, cfg):
    """Добавляет монету в автоарбитраж (или обновляет её настройки) и возвращает
    отчёт: пара на HTX, сети, контракт, адресная книга. Общая для /arb_add и
    кнопки «В автоарбитраж» под сигналом сканера."""
    net, pct = cfg["net"], cfg["pct"]
    arb.setdefault("stats_coins", {}).setdefault(coin, None)
    text = f"✅ <b>{coin}</b>: от {pct:g}% · {NET_TITLES[net]} · " + \
           (f"проба {cfg['probe']:g}$ → весь баланс" if cfg["probe"] else "сразу весь баланс")
    # Тикер пишется как на MEXC (там продаём). Нет пары на MEXC — не добавляем.
    try:
        await mexc_symbol(f"{coin}USDT")
    except ExchangeError:
        return (f"❌ <b>{coin}</b> не добавлена: пары {coin}/USDT нет на MEXC.\n"
                f"Пиши тикер как на MEXC — если на HTX он другой, бот найдёт его сам.")
    arb["coins"][coin] = cfg
    # Повторный /arb_add — «попробуй снова»: забываем прошлый отказ HTX по адресу.
    for bad in arb.get("addr_bad", {}).values():
        for c in (coin, (cfg.get("htx_coin") or coin)):
            if c.upper() in bad:
                bad.remove(c.upper())
    await save()
    try:
        res = await resolve_coin(coin, cfg)
        await save()  # сопоставление сетей/тикера, найденное при проверке
        if res["htx_ticker_auto"]:
            if res["htx_ticker"] != coin:
                text += f"\n🔎 На HTX эта монета — <b>{res['htx_ticker']}</b> (нашёл сам: {res['htx_ticker_auto']})"
        text += f"\nПара на HTX: <code>{res['htx_pair']}</code> · на MEXC: <code>{coin}USDT</code>"
        text += (f"\nHTX сеть: <code>{res['htx_chain']}</code> (вывод {'открыт' if res['htx_withdraw_ok'] else 'ЗАКРЫТ'} по API)"
                 f"\nMEXC сеть: <code>{res['mexc_net'].get('netWork') or res['mexc_net'].get('network')}</code>"
                 f"\n{contract_line(coin, cfg, res)}")
        try:
            addr = wallet_address(net)
            saved, how = await htx_address_status(res["htx_ticker"], res["htx_chain"], addr)
            if saved is True:
                text += f"\n✅ Адрес бота для вывода с HTX подтверждён: {how}"
            elif saved is False:
                text += "\n⛔ " + address_book_hint(res["htx_ticker"], res["htx_chain"], addr) + \
                        f" ({how}). Пока так, бот по этой монете не покупает."
            else:
                text += f"\n❔ Адрес для вывода с HTX {how}"
        except Exception as e:
            text += f"\n❔ Адрес бота: {e}"
    except Exception as e:
        text += f"\n⚠️ Проверка сетей: {e}"
    return text


def net_from_names(*names):
    """Код сети бота по названиям сети на биржах ('ERC20', 'BEP20(BSC)', 'SUI'…) или None."""
    for net in NET_TITLES:
        if matches_net(net, *names):
            return net
    return None


# Кнопка под сигналом сканера: монета и сеть уже известны, процент (и пробу)
# пишешь сам — ответом на сообщение бота. {chat_id: {"coin", "net", "ts"}}
pending_add = {}


@router.callback_query(F.data.startswith("arbmanual:"))
async def cb_manual_sell(callback: types.CallbackQuery):
    """Кнопка под «отправил на MEXC»: бот не продаёт эти монеты, продаёшь сам."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    did = callback.data.split(":", 1)[1]
    d = next((x for x in deals.values() if x["id"] == did), None)
    if not d:
        await callback.answer("Сделка уже закончена")
        return
    d["manual_request"] = True  # обработает цикл сделки: снимет лимитку и перестанет продавать
    await callback.answer("Ок, продажу на MEXC останавливаю")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("arbadd:"))
async def cb_add(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    try:
        coin, net = callback.data.split(":")[1:3]
        if net not in NET_TITLES:
            raise ValueError
    except Exception:
        await callback.answer("Не разобрал кнопку")
        return
    chat_id = callback.message.chat.id
    ctx.chat_set(chat_id)
    pending_add[chat_id] = {"coin": coin, "net": net, "ts": time.time()}
    await callback.answer()
    await ctx.bot.send_message(
        chat_id,
        f"➕ <b>{coin}</b> в автоарбитраж, сеть {NET_TITLES[net]}.\n"
        f"Напиши процент арбитража (и пробу в $, если нужна): <code>2.5</code> или <code>2.5 20</code>\n"
        f"<i>= /arb_add {coin} &lt;процент&gt; {net}</i>",
        parse_mode="HTML",
        reply_markup=types.ForceReply(input_field_placeholder=f"{coin}: процент, например 2.5"))


@router.message(F.text.regexp(r"^\s*\d+(?:[.,]\d+)?%?(?:\s+\d+(?:[.,]\d+)?\$?)?\s*$"))
async def pending_add_reply(message: types.Message):
    """Ответ на кнопку «в автоарбитраж»: «2.5» или «2.5 20» (процент и проба)."""
    p = pending_add.get(message.chat.id)
    if not p or time.time() - p["ts"] > 600 or not _is_admin(message.from_user.id):
        return
    parts = message.text.replace(",", ".").replace("%", "").replace("$", "").split()
    pct = abs(float(parts[0]))
    probe = abs(float(parts[1])) if len(parts) > 1 else 0
    if pct <= 0:
        await message.answer("❌ Процент должен быть больше нуля")
        return
    pending_add.pop(message.chat.id, None)
    ctx.chat_set(message.chat.id)
    cfg = {"pct": pct, "net": p["net"], "probe": probe, "htx_chain": None, "mexc_net": None}
    await message.answer(await add_coin(p["coin"], cfg), parse_mode="HTML")


@router.message(Command("arb_htx"))
async def cmd_htx_check(message: types.Message, command: CommandObject):
    """Диагностика: что HTX реально отдаёт по монете (публично и через ключ)."""
    if not await _guard(message):
        return
    coin = re.sub(r"[^A-Z0-9]", "", (command.args or "").upper())
    if not coin:
        await message.answer("Пример: /arb_htx PEPE  (тикер как на HTX, например MONAD)")
        return
    c = coin.lower()
    out = [f"🔬 <b>HTX отдаёт по {coin}</b>"]

    def contract_keys(d):
        return {k: v for k, v in d.items() if v and ("contract" in k.lower() or k.lower() in ("ca", "address"))}

    try:
        data = await htx_req("GET", "/v2/reference/currencies", {"currency": c}, signed=False)
        item = next((x for x in data or [] if str(x.get("currency", "")).lower() == c), None)
        out.append("\n<b>1. Сети и статусы (публично):</b>")
        if not item:
            out.append("монета не найдена")
        for ch in (item or {}).get("chains", []):
            fee = htx_chain_fee(ch)
            out.append(
                f"• <code>{ch.get('chain')}</code> {ch.get('displayName') or ''} [{ch.get('baseChain') or ''}] "
                f"вывод: {ch.get('withdrawStatus')}, ввод: {ch.get('depositStatus')}, "
                f"комиссия: {ch.get('withdrawFeeType')} {fmt(fee) if fee else 'НЕТ ДАННЫХ'}, "
                f"мин. {ch.get('minWithdrawAmt') or '—'}"
                + (f", контракт: {contract_keys(ch)}" if contract_keys(ch) else ", контракта в ответе нет"))
    except Exception as e:
        out.append(f"1. ошибка: {e}")

    try:
        rows = await htx_req("GET", "/v1/settings/common/chains", {"currency": c}, signed=False)
        out.append("\n<b>2. Контракты (публично, /v1/settings/common/chains):</b>")
        if not rows:
            out.append("пусто")
        for row in rows or []:
            ca = row.get("ca") or row.get("contractAddress") or row.get("contract")
            out.append(f"• <code>{row.get('chain')}</code>: " + (f"контракт <code>{ca}</code>" if ca else "контракта нет"))
            # Все поля как есть — чтобы увидеть, отдаёт ли HTX тут статусы и комиссии.
            raw = ", ".join(f"{k}={v}" for k, v in row.items() if k not in ("currency", "chain", "ca"))
            if raw:
                out.append(f"   <code>{raw[:600].replace('<', '')}</code>")
    except Exception as e:
        out.append(f"\n2. ошибка: {e}")

    try:
        q = await htx_req("GET", "/v2/account/withdraw/quota", {"currency": c})
        out.append("\n<b>3. Лимиты вывода по твоему ключу:</b>")
        for ch in (q or {}).get("chains", []):
            out.append(f"• <code>{ch.get('chain')}</code>: макс. за раз {ch.get('maxWithdrawAmt')}, "
                       f"осталось на сегодня {ch.get('remainWithdrawQuotaPerDay')}")
        if not (q or {}).get("chains"):
            out.append("пусто")
    except Exception as e:
        out.append(f"\n3. ключ: {e}")

    try:
        addrs = await htx_req("GET", "/v2/account/withdraw/address", {"currency": c})
        out.append("\n<b>4. Адресная книга вывода:</b>")
        mine = set()
        for net in NET_TITLES:
            try:
                mine.add(wallet_address(net).lower())
            except Exception:
                pass
        for a in addrs or []:
            addr = str(a.get("address", ""))
            out.append(f"• <code>{a.get('chain')}</code> {addr} {'✅ кошелёк бота' if addr.lower() in mine else ''}")
            raw = ", ".join(f"{k}={v}" for k, v in a.items() if k not in ("address",))
            out.append(f"   <code>{raw[:300].replace('<', '')}</code>")
        if not addrs:
            out.append("у самой монеты адресов нет")
        try:
            evm = evm_account().address
            found, kind = await htx_find_saved(coin, None, evm)
            out.append(f"EVM-адрес бота в книге: " + (f"✅ найден ({kind}): <code>{found}</code>" if found else
                                                      "❌ через API не виден ни у монеты, ни как общий"))
            if not found:
                chains = await htx_chains(coin)
                ch = chains[0] if chains else {}
                ok, how = await htx_address_status(coin, ch.get("chain"), evm)
                out.append(("✅ " if ok else "❌ " if ok is False else "❔ ") + how)
        except Exception as e:
            out.append(f"EVM-адрес бота: проверить не удалось: {e}")
    except Exception as e:
        out.append(f"\n4. ключ: {e}")

    # Режем по строкам, а не посреди строки — иначе разорвётся HTML-тег.
    chunk = ""
    for line in out:
        if len(chunk) + len(line) > 3800:
            await message.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk, parse_mode="HTML")


@router.message(Command("arb_confirm"))
async def cmd_confirm(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    coin = re.sub(r"[^A-Z0-9]", "", (command.args or "").upper())
    cfg = arb["coins"].get(coin)
    if not cfg:
        await message.answer("Пример: /arb_confirm PEPE (монета должна быть в /arb)")
        return
    try:
        res = await resolve_coin(coin, cfg)
    except Exception as e:
        await message.answer(f"❌ {e}", parse_mode="HTML")
        return
    if res["contract_status"] != "unknown":
        await message.answer(contract_line(coin, cfg, res) + "\nРучное подтверждение не нужно.", parse_mode="HTML")
        return
    # Запоминаем именно этот адрес: если MEXC когда-нибудь сменит контракт,
    # подтверждение перестанет действовать само.
    cfg["confirmed_contract"] = res["token"]
    await save()
    await message.answer(f"✅ <b>{coin}</b>: контракт <code>{res['token']}</code> подтверждён.", parse_mode="HTML")


@router.message(Command("arb_del"))
async def cmd_del(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    coin = re.sub(r"[^A-Z0-9]", "", (command.args or "").upper())
    if coin.endswith("USDT"):
        coin = coin[:-4]
    if arb["coins"].pop(coin, None):
        await save()
        await message.answer(f"🗑 <b>{coin}</b> убрана из автоарбитража", parse_mode="HTML")
    else:
        await message.answer(f"ℹ️ <b>{coin}</b> и так нет в списке", parse_mode="HTML")


@router.message(Command("arb_chains"))
async def cmd_chains(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    parts = [re.sub(r"[^A-Z0-9]", "", p) for p in (command.args or "").upper().split()]
    coin = parts[0] if parts else ""
    if not coin:
        await message.answer("Пример: /arb_chains PEPE  ·  если на HTX тикер другой: /arb_chains MON MONAD")
        return
    htx_ticker = parts[1] if len(parts) > 1 else hcoin(coin, arb["coins"].get(coin, {}))
    lines = [f"🔗 <b>{coin}</b>" + (f" (на HTX: {htx_ticker})" if htx_ticker != coin else "")]
    try:
        lines.append("<b>HTX:</b>")
        for c in await htx_chains(htx_ticker):
            lines.append(f"• <code>{c.get('chain')}</code> {c.get('displayName') or ''} "
                         f"[{c.get('baseChain') or ''} {c.get('baseChainProtocol') or ''}] "
                         f"вывод: {c.get('withdrawStatus')}, комиссия ~{fmt(htx_chain_fee(c))}, "
                         f"мин. {c.get('minWithdrawAmt')}")
    except Exception as e:
        lines.append(f"ошибка: {e}")
    try:
        lines.append("<b>MEXC:</b>")
        for n in await mexc_networks(coin):
            lines.append(f"• <code>{n.get('netWork')}</code> / {n.get('network')} "
                         f"депозит: {'да' if n.get('depositEnable') else 'нет'}, "
                         f"контракт: <code>{n.get('contract') or '-'}</code>")
    except Exception as e:
        lines.append(f"ошибка: {e}")
    cfg = arb["coins"].get(coin)
    if cfg:
        try:
            res = await resolve_coin(coin, cfg)
            lines.append(f"\n✅ Бот использует: HTX <code>{res['htx_chain']}</code> → MEXC "
                         f"<code>{res['mexc_net'].get('netWork') or res['mexc_net'].get('network')}</code>")
        except Exception as e:
            lines.append(f"\n⚠️ {e}")
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("arb_net"))
async def cmd_net(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    args = (command.args or "").split()
    action = args[0].lower() if args else "list"

    if action == "add" and len(args) >= 2 and (len(args) == 2 or not args[2].startswith("http")):
        # /arb_net add mapo — сеть и ноду бот находит сам.
        try:
            name, chain, good = await discover_network(" ".join(args[1:]))
        except ExchangeError as e:
            await message.answer(f"❌ {e}")
            return
        await message.answer(
            f"✅ Сеть <b>{name}</b> добавлена: {chain.get('name')}, chain id {chain.get('chainId')}, "
            f"газ в {arb['networks'][name]['native']}. Проверенных нод: {len(good)} "
            f"(если одна отвалится — перейду на другую).\nТеперь: /arb_add МОНЕТА 3 {name}",
            parse_mode="HTML")
        return

    if action == "add":
        try:
            name = args[1].lower()
            rpc_url = args[2]
            native = args[3].upper()
            if not re.fullmatch(r"[a-z0-9_]{2,20}", name) or not rpc_url.startswith("http"):
                raise ValueError
            aliases = [a for a in (args[4].upper().split(",") if len(args) > 4 else []) if a]
        except Exception:
            await message.answer(
                "Пример: /arb_net add mapo — сеть и ноду бот найдёт сам\n"
                "или вручную: /arb_net add mapo https://rpc.maplabs.io MAPO\n"
                "или с названиями сети на биржах: /arb_net add mapo https://rpc.maplabs.io MAPO MAPO,MAP")
            return
        if name in BUILTIN_NETS:
            await message.answer("❌ Это встроенная сеть, её менять не нужно.")
            return
        # Проверяем, что по адресу реально отвечает EVM-нода, до сохранения.
        RPC_URLS[name] = rpc_url
        try:
            chain_id = int(await rpc(name, "eth_chainId", []), 16)
        except Exception as e:
            if name not in arb["networks"]:
                RPC_URLS.pop(name, None)
            else:
                RPC_URLS[name] = arb["networks"][name]["rpc"]
            await message.answer(f"❌ Нода не отвечает как EVM-сеть: {e}")
            return
        arb["networks"][name] = {"rpc": rpc_url, "native": native, "aliases": aliases, "chain_id": chain_id}
        register_net(name, rpc_url, native, aliases)
        await save()
        addr = ""
        try:
            addr = f"\nАдрес кошелька бота в ней: <code>{evm_account().address}</code> (тот же, что в ETH/BNB)"
        except Exception:
            pass
        await message.answer(
            f"✅ Сеть <b>{name}</b> добавлена (chain id {chain_id}, газ в {native}).\n"
            f"Ищу её на биржах по названиям: {', '.join(sorted(NET_ALIASES[name]))}{addr}\n"
            f"Теперь: /arb_add МОНЕТА 3 {name}  ·  проверить сети монеты: /arb_chains МОНЕТА",
            parse_mode="HTML")
        return

    if action == "del":
        name = args[1].lower() if len(args) > 1 else ""
        if name not in arb["networks"]:
            await message.answer("❌ Такой своей сети нет. Список: /arb_net")
            return
        used = [c for c, cfg in arb["coins"].items() if cfg["net"] == name]
        if used:
            await message.answer(f"❌ Сеть используют монеты: {', '.join(used)}. Сначала /arb_del.")
            return
        arb["networks"].pop(name)
        unregister_net(name)
        await save()
        await message.answer(f"🗑 Сеть <b>{name}</b> удалена", parse_mode="HTML")
        return

    lines = ["🌐 <b>Сети</b>"]
    for name in NET_TITLES:
        kind = "встроенная" if name in BUILTIN_NETS else "своя EVM"
        lines.append(f"• <code>{name}</code> — {kind}, газ {NATIVE_COIN[name]}, нода {RPC_URLS[name]}")
    lines.append("\nДобавить: /arb_net add mapo https://rpc.maplabs.io MAPO")
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("arb_on"))
async def cmd_on(message: types.Message):
    if not await _guard(message):
        return
    arb["enabled"] = True
    await save()
    await message.answer(f"▶️ Автоарбитраж ВКЛЮЧЁН · режим: "
                         f"{'🧪 ТЕСТ' if arb['dry_run'] else '💸 РЕАЛЬНЫЕ СДЕЛКИ'}")


@router.message(Command("arb_off"))
async def cmd_off(message: types.Message):
    if not await _guard(message):
        return
    arb["enabled"] = False
    await save()
    await message.answer("⏸ Автоарбитраж ВЫКЛЮЧЕН (новые сделки не начинаются; текущая, если есть, "
                         "доводится до конца)")


@router.message(Command("arb_live"))
async def cmd_live(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    arg = (command.args or "").strip().lower()
    if arg == "on":
        arb["dry_run"] = False
        await message.answer("💸 Режим РЕАЛЬНЫХ СДЕЛОК. Бот будет покупать и выводить.")
    elif arg == "off":
        arb["dry_run"] = True
        await message.answer("🧪 Тестовый режим: бот только сообщает, что сделал бы.")
    else:
        await message.answer("Использование: /arb_live on — реальные сделки, /arb_live off — тест")
        return
    await save()


@router.message(Command("arb_set"))
async def cmd_set(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    keys = {"step": ("step_pct", float), "interval": ("step_sec", int),
            "floor": ("floor_pct", float), "check": ("check_sec", int),
            "spam": ("spam_sec", float), "poll": ("poll_sec", float),
            "batch": ("batch_usd", float), "topup": ("topup_usd", float)}
    args = (command.args or "").split()
    if len(args) == 2 and args[0].lower() == "depth" and args[1].lower() in ("on", "off"):
        arb["mexc_depth"] = args[1].lower() == "on"
        await save()
        await message.answer(f"✅ Стакан MEXC при покупке: {'учитывается' if arb['mexc_depth'] else 'только лучшая цена'}")
        return
    try:
        key, conv = keys[args[0].lower()]
        arb[key] = abs(conv(float(args[1].replace(",", "."))))
        if key == "poll_sec":
            arb[key] = max(0.5, arb[key])
    except Exception:
        await message.answer("Пример: /arb_set step 0.3 · /arb_set interval 120 · "
                             "/arb_set floor 1.2 · /arb_set check 60 · /arb_set spam 1 · /arb_set poll 1")
        return
    await save()
    await message.answer(f"✅ {args[0]} = {arb[key]}")


@router.message(Command("arb_wallet"))
async def cmd_wallet(message: types.Message):
    if not await _guard(message):
        return
    lines = [_keys_text(), ""]
    for net in list(NET_TITLES):
        try:
            bal = await wallet_balance(net, None)
            dec = {"sui": 9, "atom": 6, "sol": 9, "trx": 6}.get(net, 18)
            lines.append(f"{NET_TITLES[net]}: {fmt(D(bal) / (D(10) ** dec))} {NATIVE_COIN[net]} (на газ)")
        except Exception as e:
            lines.append(f"{NET_TITLES[net]}: {e}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("arb_reset"))
async def cmd_reset(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    if (command.args or "").strip().lower() != "да":
        await message.answer("Бот забудет сделки (монеты/ордера останутся как есть — разбирать вручную).\n"
                             "Подтверди: /arb_reset да  (или только одну: /arb_reset да PEPE)")
        return
    coin = re.sub(r"[^A-Z0-9]", "", ((command.args or "").split()[1:] or [""])[0].upper())
    if coin and coin in deals:
        deals[coin]["abandoned"] = True
        text = f"🧹 Сделка {coin} сброшена."
    else:
        for d in deals.values():
            d["abandoned"] = True
        text = "🧹 Все сделки сброшены."
    await save()
    await message.answer(text + " Ордера и монеты остались как есть — разбирать вручную.")


async def _stop_spam():
    """Гасит спам. Для аварий «вывод не прошёл» бот ещё и «забивает» на монету:
    бросает её сделку, не трогает её ордера и не торгует ею PAUSE_SEC сек. или до /arb_resume."""
    n, paused = 0, []
    for a in alarms.values():
        if not a["acked"]:
            a["acked"] = True
            n += 1
            c = a.get("pause_coin")
            if c:
                arb.setdefault("paused", {})[c] = time.time() + PAUSE_SEC
                paused.append(c)
                if c in deals:
                    deals[c]["abandoned"] = True
    if paused:
        await save()
    return n, sorted(set(paused))


@router.message(Command("arb_resume"))
async def cmd_resume(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    coin = re.sub(r"[^A-Z0-9]", "", (command.args or "").upper())
    paused = arb.setdefault("paused", {})
    if not coin or coin == "ALL":
        coins = list(paused)
        paused.clear()
    else:
        coins = [coin] if paused.pop(coin, None) else []
    await save()
    await message.answer(f"▶️ Снова торгую: {', '.join(coins)}" if coins else "ℹ️ На паузе ничего нет.")


@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if not await _guard(message):
        return
    n, paused = await _stop_spam()
    text = f"🔕 Спам остановлен ({n})."
    if paused:
        text += (f"\n⏸ Автоарбитраж по {', '.join(paused)} отключён на {PAUSE_SEC // 60} мин.: ордера не трогаю, "
                 f"сделку бросил — дальше вручную. Вернуть раньше: /arb_resume {paused[0]}")
    await message.answer(text)


@router.callback_query(F.data == "arb_stop_spam")
async def cb_stop(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    n, paused = await _stop_spam()
    await callback.answer(f"Спам остановлен ({n})")
    if paused:
        await notify(f"⏸ Автоарбитраж по {', '.join(paused)} отключён на {PAUSE_SEC // 60} мин.: ордера не трогаю, "
                     f"сделку бросил — дальше вручную. Вернуть раньше: /arb_resume {paused[0]}")


# ================= ЗАПУСК =================

# ================= ПОПОЛНЕНИЕ HTX USDT С КОШЕЛЬКА =================
#
# Только из этих сетей (chain id, как искать сеть, контракт USDT). Из других — никогда.
TOPUP_NETS = [
    (56, "bsc", "0x55d398326f99059fF775485246999027B3197955"),
    (42161, "Arbitrum One", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
]
TOPUP_MIN = D(10)            # меньше не переводим
TOPUP_COOLDOWN = 2 * 60      # после перевода столько не переводим снова (деньги в пути)


async def _evm_net_by_chain_id(cid, query):
    """Наша сеть с этим chain id; если такой нет — находим и добавляем сами."""
    for name, n in list(EVM_CHAIN_IDS.items()):
        if n == cid:
            return name
    for name, n in arb.get("networks", {}).items():
        if n.get("chain_id") == cid:
            return name
    name, chain, _ = await discover_network(query)
    if chain.get("chainId") != cid:
        raise ExchangeError(f"нашлась сеть {chain.get('name')} с chain id {chain.get('chainId')}, а нужна {cid}")
    return name


async def htx_usdt_deposit_address(net, contract):
    """Адрес депозита USDT на HTX в нашей сети: сеть HTX ищем по контракту USDT."""
    chains = await htx_chains("usdt")
    hit = await _htx_chain_by_contract("usdt", chains, contract)
    if not hit:
        cand = _net_chains(chains, net, {})
        hit = cand[0] if len(cand) == 1 else None
    if not hit:
        raise ExchangeError(f"на HTX нет депозита USDT в сети {NET_TITLES[net]}")
    if hit.get("depositStatus") not in (None, "allowed"):
        raise ExchangeError(f"депозит USDT в сети {hit.get('chain')} на HTX закрыт")
    rows = await htx_req("GET", "/v2/account/deposit/address", {"currency": "usdt"})
    row = next((r for r in rows or [] if r.get("chain") == hit.get("chain")), None)
    if not row or not row.get("address"):
        raise ExchangeError(f"HTX не дал адрес депозита USDT в сети {hit.get('chain')} — "
                            f"открой один раз страницу депозита USDT в этой сети на HTX")
    if row.get("addressTag"):
        raise ExchangeError("HTX требует memo для депозита USDT — на EVM так не бывает, не перевожу")
    return row["address"], hit.get("chain")


topup_status = {"t": 0.0, "text": "ещё не проверял"}


def _topup_say(text):
    topup_status.update(t=time.time(), text=text)


async def topup_step():
    """USDT на HTX меньше порога — переводим недостающее с кошелька бота (BNB / Arbitrum)."""
    need_level = D(str(arb.get("topup_usd") or 0))
    if need_level <= 0 or not arb["enabled"] or arb["dry_run"]:
        _topup_say("выключено: нужно /arb_on и /arb_live on, и порог /arb_set topup > 0")
        return
    free, frozen = await htx_balance("usdt")
    have = free + frozen
    _topup_say(f"на HTX {fmt(have)} USDT — пополнять не нужно")
    # Зачисление на HTX не отслеживаем — только пауза после перевода, чтобы, пока
    # деньги идут по сети, не отправить ту же сумму второй раз.
    arb.pop("topup_inflight", None)
    arb.pop("topup_pending", None)
    last = arb.get("topup_last") or 0
    if time.time() - last < TOPUP_COOLDOWN:
        _topup_say(f"на HTX {fmt(have)} USDT; перевёл {_ago(last)} назад — пауза {TOPUP_COOLDOWN // 60} мин., "
                   f"пока деньги идут")
        return
    need = need_level - have
    if need < TOPUP_MIN:
        return
    # Где на кошельке есть USDT: сначала сеть, где его больше.
    options = []
    for cid, query, token in TOPUP_NETS:
        try:
            net = await _evm_net_by_chain_id(cid, query)
            dec = await evm_decimals(net, token)
            bal = D(await evm_balance(net, token)) / D(10) ** dec
            gas = await evm_balance(net, None)
        except Exception as e:
            _topup_say(f"сеть {query} недоступна: {str(e)[:100]}")
            await note_once(f"topupnet:{cid}", f"⚠️ Пополнение HTX: сеть {query} недоступна: <code>{e}</code>",
                            every=6 * 3600)
            continue
        if bal >= TOPUP_MIN:
            if gas <= 0:
                _topup_say(f"на кошельке {fmt(bal)} USDT в сети {NET_TITLES[net]}, но нет {NATIVE_COIN[net]} на газ")
                await note_once(f"topupgas:{cid}", f"⚠️ Пополнение HTX: на кошельке {fmt(bal)} USDT в сети "
                                                   f"{NET_TITLES[net]}, но нет {NATIVE_COIN[net]} на газ.", every=6 * 3600)
                continue
            options.append((bal, net, token, dec))
    if not options:
        _topup_say(f"на HTX {fmt(have)} USDT (< {fmt(need_level)}), но на кошельке нет USDT "
                   f"(от {fmt(TOPUP_MIN)}) с газом в сетях BNB / Arbitrum")
        await note_once("topup_empty", f"ℹ️ На HTX {fmt(have)} USDT (меньше {fmt(need_level)}), "
                                       f"а на кошельке бота нет USDT в сетях BNB / Arbitrum для пополнения.",
                        every=6 * 3600)
        return
    bal, net, token, dec = max(options, key=lambda o: o[0])
    amount = min(need, bal)
    amount = amount.quantize(D("0.01"), rounding=ROUND_DOWN)
    address, chain = await htx_usdt_deposit_address(net, token)
    t_send = time.time()
    tx, sent_raw = await evm_send_all(net, token, address, int(amount * D(10) ** dec))
    sent = D(sent_raw) / D(10) ** dec
    arb["topup_last"] = t_send
    await save()
    _topup_say(f"перевёл {fmt(sent)} USDT ({NET_TITLES[net]})")
    await notify(f"💵 На HTX {fmt(have)} USDT — меньше {fmt(need_level)}. Перевёл {fmt(sent)} USDT "
                 f"с кошелька бота в сети {NET_TITLES[net]} на депозит HTX ({chain}).\ntx: <code>{tx}</code>")


async def topup_loop():
    while True:
        try:
            await topup_step()
        except Exception as e:
            print(f"[arb] topup: {traceback.format_exc()}", flush=True)
            _topup_say(f"ошибка: {str(e)[:150]}")
            await note_once("topup_err", f"⚠️ Пополнение HTX с кошелька: ошибка <code>{e}</code>", every=1800)
        await asyncio.sleep(60)


async def start():
    """Восстанавливает состояние и запускает фоновые задачи. Возвращает их список."""
    await load()
    tasks = [asyncio.create_task(spam_loop()), asyncio.create_task(engine_loop()),
             asyncio.create_task(topup_loop()), asyncio.create_task(track_assets_loop())]
    for coin, d in list(deals.items()):
        if d.get("v") != 2:
            await notify(f"⚠️ Незавершённая сделка <b>{coin}</b> из прошлой версии бота сброшена — "
                         f"проверь HTX/кошелёк/MEXC вручную.")
            deals.pop(coin)
            continue
        await notify(f"♻️ Бот перезапущен посреди сделки <b>{coin}</b> — продолжаю.")
        tasks.append(spawn(run_deal(d)))
    await save()
    for r in list(rescues):
        tasks.append(spawn(run_rescue(r)))
    return tasks


BOT_COMMANDS = [
    ("arb", "Автоарбитраж: статус, спред, сделка, балансы"),
    ("arb_help", "Автоарбитраж: помощь"),
    ("arb_list", "Список монет в автоарбитраже"),
    ("arb_stats", "Сделки и прибыль за сутки / неделю / месяц"),
    ("arb_add", "Добавить монету: PEPE 3 bsc [проба$]"),
    ("arb_del", "Убрать монету из автоарбитража"),
    ("arb_chains", "Сети монеты на HTX и MEXC"),
    ("arb_confirm", "Подтвердить контракт монеты вручную"),
    ("arb_htx", "Что HTX отдаёт по монете (проверка API)"),
    ("arb_net", "Свои EVM-сети: список / add / del"),
    ("arb_on", "Включить автоарбитраж"),
    ("arb_off", "Выключить автоарбитраж"),
    ("arb_live", "Реальные сделки on / тест off"),
    ("arb_set", "Параметры лесенки и проверок"),
    ("arb_wallet", "Кошельки бота"),
    ("arb_resume", "Снять паузу с монеты после /stop"),
    ("stop", "Остановить спам"),
]
