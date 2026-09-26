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
}
NET_TITLES = {"eth": "Ethereum", "bsc": "BNB Chain", "monad": "Monad", "sui": "Sui"}
NATIVE_COIN = {"eth": "ETH", "bsc": "BNB", "monad": "MON", "sui": "SUI"}
BUILTIN_NETS = ("eth", "bsc", "monad", "sui")
# Как сеть может называться у бирж. Сравниваем по отдельным словам названия
# ("BEP20(BSC)" → BEP20, BSC), а не подстрокой — иначе ETH совпал бы с ETHW.
NET_ALIASES = {
    "eth": {"ERC20", "ETH", "ETHEREUM"},
    "bsc": {"BEP20", "BSC", "BNB SMART CHAIN", "BNBSMARTCHAIN", "BSC20"},
    "monad": {"MONAD", "MON"},
    "sui": {"SUI"},
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
    # Если продавцов в пределах % нет — ставить свой ордер на покупку первым в
    # стакане HTX и держать его, переставляя под цену MEXC (/arb_set maker on|off).
    "maker": True,
    # С какой суммы купленного сразу выводить партию, не снимая ордер (/arb_set batch).
    "batch_usd": 15.0,
}

deal = None       # активная сделка (одна за раз: покупка идёт на весь баланс)
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
    """Приводит адреса к одному виду: регистр EVM не важен, а у Sui 0x2 == 0x000…02."""
    x = str(x or "").strip().lower()
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

async def mexc_req(method, path, params=None, signed=True):
    params = dict(params or {})
    if signed:
        if not MEXC_API_KEY or not MEXC_API_SECRET:
            raise ExchangeError("не заданы MEXC_API_KEY / MEXC_API_SECRET")
        params["timestamp"] = int(time.time() * 1000)
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
    if row.get("memo") or row.get("tag"):
        raise ExchangeError(f"MEXC требует memo для {coin} — такие сети бот не поддерживает")
    return row["address"]


# ================= КОШЕЛЬКИ =================

async def rpc(net, method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with http().post(RPC_URLS[net], json=body, timeout=TIMEOUT) as r:
        data = await r.json(content_type=None)
    if data.get("error"):
        raise ExchangeError(f"RPC {net} {method}: {data['error']}")
    return data.get("result")


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


async def evm_send_all(net, token, to):
    """Отправляет ВЕСЬ баланс токена (или нативной монеты за вычетом газа).
    Возвращает (tx_hash, отправлено_в_минимальных_единицах)."""
    from eth_utils import to_checksum_address
    acct = evm_account()
    to = to_checksum_address(to)
    chain_id = int(await rpc(net, "eth_chainId", []), 16)
    nonce = int(await rpc(net, "eth_getTransactionCount", [acct.address, "pending"]), 16)
    gas_price = int(int(await rpc(net, "eth_gasPrice", []), 16) * 1.2)
    balance = await evm_balance(net, token)
    if token:
        amount = balance
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


def wallet_address(net):
    return sui_keys()[2] if net == "sui" else evm_account().address


async def wallet_balance(net, token):
    return await (sui_balance(token) if net == "sui" else evm_balance(net, token))


async def wallet_decimals(net, token):
    return await (sui_decimals(token) if net == "sui" else evm_decimals(net, token))


async def wallet_send_all(net, token, to):
    return await (sui_send_all(token, to) if net == "sui" else evm_send_all(net, token, to))


async def htx_address_saved(currency, chain, address):
    """Есть ли адрес в адресной книге вывода HTX для этой монеты и сети.
    Через API HTX выводит ТОЛЬКО на сохранённые адреса (иначе ошибка
    api-not-support-temp-addr). None — проверить не удалось (ключ/ошибка API)."""
    try:
        rows = await htx_req("GET", "/v2/account/withdraw/address", {"currency": currency.lower()})
    except Exception as e:
        print(f"[arb] адресная книга HTX {currency}: {e}", flush=True)
        return None
    addr = address.lower()
    return any(str(r.get("address", "")).lower() == addr and (not chain or r.get("chain") == chain)
               for r in rows or [])


def address_book_hint(coin_htx, chain, address):
    return (f"Добавь адрес бота в адресную книгу вывода HTX: монета <b>{coin_htx}</b>, сеть <code>{chain}</code>, "
            f"адрес <code>{address}</code>. Через API HTX выводит только на сохранённые адреса.")


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
        if not cand:
            return chains, None, None, "нет сети"
        if len(cand) > 1:
            return chains, None, None, "много сетей"
        ca = await htx_contract(ticker, cand[0]) if token else None
        if token and ca and norm_contract(ca) != norm_contract(token):
            return chains, cand[0], ca, "другой контракт"
        return chains, cand[0], ca, None

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

    # Запоминаем найденную монету и НАСТОЯЩЕЕ имя пары на HTX — дальше стакан и
    # ордера идут строго по ним, а не по тикеру с MEXC.
    pair = await htx_usdt_pair(hc)
    if cfg.get("htx_symbol") != pair or (hc != coin.upper() and cfg.get("htx_coin") != hc):
        cfg["htx_symbol"] = pair
        if hc != coin.upper():
            cfg["htx_coin"] = hc
        auto_how = auto_how or "обновлена пара"

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
    }


def contract_confirmed(cfg, res):
    return res["contract_status"] in ("ok", "native") or \
        (cfg.get("confirmed_contract") and norm_contract(cfg["confirmed_contract"]) == norm_contract(res["token"]))


def contract_line(coin, cfg, res):
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

async def notify(text):
    chat = ctx.chat_get()
    if not chat:
        print(f"[arb] нет chat_id, сообщение: {text}", flush=True)
        return
    for _ in range(3):
        try:
            await ctx.bot.send_message(chat, text, parse_mode="HTML")
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            print(f"[arb] ошибка отправки: {e}", flush=True)
            return


STOP_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🛑 Остановить спам", callback_data="arb_stop_spam")]])


def alarm_start(text):
    aid = uuid.uuid4().hex[:8]
    alarms[aid] = {"text": text, "acked": False}
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
        ctx.redis("SET", "arb:deal", json.dumps(deal, default=_json_default)),
        ctx.redis("SET", "arb:rescues", json.dumps(rescues, default=_json_default)),
        return_exceptions=True,
    )


async def load():
    global deal, rescues
    if not ctx.redis:
        return
    raw_cfg, raw_deal, raw_resc = await asyncio.gather(
        ctx.redis("GET", "arb:config"), ctx.redis("GET", "arb:deal"),
        ctx.redis("GET", "arb:rescues"), return_exceptions=True)
    try:
        if isinstance(raw_cfg, str):
            arb.update(json.loads(raw_cfg))
            for name, n in arb.get("networks", {}).items():
                register_net(name, n["rpc"], n["native"], n.get("aliases", []))
        if isinstance(raw_deal, str):
            deal = json.loads(raw_deal)
        if isinstance(raw_resc, str):
            rescues = json.loads(raw_resc) or []
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
    if arb.get("maker", True) and hbids:
        # Своим ордером первым в стакане покупателей: на тик выше лучшего чужого,
        # но не дороже цены с нужным % и ниже лучшего продавца.
        tick = (await htx_symbol(hpair))["tick"]
        want = hbids[0][0] + tick
        if want <= min(round_down(max_price, tick), hasks[0][0] - tick):
            return dict(base, mode="maker", maker_price=want, spread=(mexc_bid - want) / want * 100)
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


async def note_once(key, text, every=1800):
    """Сообщение не чаще раза в every секунд на ключ (чтобы не засыпать чат)."""
    now = time.time()
    if now - _skip_notes.get(key, 0) >= every:
        _skip_notes[key] = now
        await notify(text)


engine_state = {"last_pass": 0.0}

def _ago(ts):
    if not ts:
        return "—"
    sec = int(time.time() - ts)
    return f"{sec} сек" if sec < 120 else f"{sec // 60} мин"


async def engine_loop():
    while True:
        engine_state["last_pass"] = time.time()
        try:
            if arb["enabled"] and deal is None and not rescues and arb["coins"]:
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
                coins = [(c, cfg) for c, cfg in arb["coins"].items() if cfg.get("htx_symbol")]
                opps = await asyncio.gather(*[check_opportunity(c, cfg) for c, cfg in coins],
                                            return_exceptions=True)
                found = []
                for (coin, cfg), opp in zip(coins, opps):
                    if isinstance(opp, Exception):
                        await note_once(f"chk:{coin}", f"⚠️ <b>{coin}</b>: не удалось проверить спред: <code>{opp}</code>", every=1800)
                    elif opp:
                        found.append((opp["spread"], coin, cfg, opp))
                for _, coin, cfg, opp in sorted(found, key=lambda x: x[0], reverse=True):
                    if await try_start(coin, cfg, opp):
                        break
        except Exception as e:
            await note_once("engine_err", f"⚠️ Автоарбитраж: ошибка цикла: <code>{e}</code>", every=600)
            print(f"[arb] engine: {traceback.format_exc()}", flush=True)
        await asyncio.sleep(arb["poll_sec"])


async def try_start(coin, cfg, opp):
    global deal
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
        await note_once(f"wd:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но HTX явно пишет, что вывод в сети {res['htx_chain']} закрыт — пропускаю.")
        return False
    if not res["mexc_deposit_ok"]:
        await note_once(f"dep:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но на MEXC закрыт депозит в этой сети — пропускаю.")
        return False
    if not arb["dry_run"]:
        addr = wallet_address(cfg["net"])
        saved = await htx_address_saved(res["htx_ticker"], res["htx_chain"], addr)
        if saved is False:
            await note_once(f"book:{coin}", f"⛔ <b>{coin}</b>: спред {opp['spread']:.2f}%, но не покупаю — "
                                            f"HTX не даст вывести.\n{address_book_hint(res['htx_ticker'], res['htx_chain'], addr)}",
                            every=3 * 3600)
            return False
    if not arb["dry_run"] and not contract_confirmed(cfg, res):
        await note_once(f"unconf:{coin}", f"⚠️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но контракт не сверен.\n"
                                          f"{contract_line(coin, cfg, res)}", every=3 * 3600)
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
            how = (f"Поставил бы ордер на покупку на HTX первым в стакане по {fmt(opp['maker_price'])} "
                   f"(макс. {fmt(opp['max_price'])}) и держал его под цену MEXC; "
                   f"вывод партиями от {arb['batch_usd']:g}$")
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

    deal = new_deal(coin, cfg, res)
    await save()
    spawn(run_deal())
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


async def run_deal():
    global deal
    d = deal
    alarm = {"id": None}
    while True:
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
    deal = None
    await save()


def deal_finished(d):
    if d["buying"] or d["order"] or d["ioc"] or d["sell"] or d["forwarding"]:
        return False
    if any(not b["ok"] for b in d["batches"]):
        return False
    if d["sell_manual"]:
        return True
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
    pct = D(cfg["pct"])
    max_price = round_down(mbids[0][0] / (1 + pct / 100), tick)

    free_usdt, _ = await htx_balance("usdt")
    o = d["order"]
    locked = (D(o["amount"]) - D(o["filled"])) * D(o["price"]) if o else D(0)
    budget = (free_usdt + locked) * D("0.995")
    if d["probe_left"] is not None:
        budget = min(budget, D(d["probe_left"]))

    # Партия набралась — выводим сразу, ордер при этом продолжает стоять.
    if _dd(d, "unw_cost") >= D(arb["batch_usd"]):
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

    # 2) Свой ордер первым в стакане покупателей.
    if not arb.get("maker", True):
        await _idle(d, "спред ушёл")
        return
    my_price = D(d["order"]["price"]) if d["order"] else None
    my_rest = (D(d["order"]["amount"]) - D(d["order"]["filled"])) if d["order"] else D(0)
    # Чужие ордера: всё, кроме нашего; если на нашей цене объёма больше нашего —
    # там стоит ещё кто-то, и эта цена тоже «чужая» (встанем на тик выше).
    others = [p for p, q in hbids if p != my_price or q > my_rest * D("1.001")]
    cap = max_price
    if hasks:
        cap = min(cap, hasks[0][0] - tick)
    want = (others[0] + tick) if others else None
    if want is None or want > cap:
        # Первым с нужным % встать нельзя — снимаем ордер и ждём.
        if d["order"]:
            await _cancel_order(d)
            await notify(f"⏸ <b>{coin}</b>: первым в стакане HTX с {cfg['pct']}% к MEXC уже не встать — "
                         f"ордер снят (MEXC bid {fmt(mbids[0][0])}, макс. цена {fmt(max_price)}).")
        await _idle(d, "спред ушёл")
        return
    d["idle_since"] = None
    if my_price == want:
        return  # стоим первыми по нужной цене
    await _cancel_order(d)
    free_usdt, _ = await htx_balance("usdt")
    budget = free_usdt * D("0.995")
    if d["probe_left"] is not None:
        budget = min(budget, D(d["probe_left"]))
    amount = round_down(budget / want, info["step"])
    if amount <= 0 or amount < info["min_qty"] or amount * want < info["min_value"]:
        return
    oid = await htx_place(hpair, "buy-limit-maker", amount, want)
    first = my_price is None
    d["order"] = {"id": oid, "price": str(want), "amount": str(amount), "filled": "0", "cash": "0"}
    await save()
    spread = (mbids[0][0] - want) / want * 100
    if first:
        await notify(f"📌 <b>{coin}</b>: поставил ордер на покупку на HTX первым: {fmt(amount)} шт. по {fmt(want)} "
                     f"(на {fmt(amount * want)}$, спред к MEXC {spread:.2f}%). Слежу за ценой.")


async def _idle(d, why):
    """Нет возможности купить: даём минуту подождать, потом заканчиваем покупку."""
    if d["idle_since"] is None:
        d["idle_since"] = time.time()
    elif time.time() - d["idle_since"] > 60:
        await _stop_buying(d, why)


async def _stop_buying(d, why):
    await _cancel_order(d)
    d["buying"] = False
    unw_q, unw_c = _dd(d, "unw_qty"), _dd(d, "unw_cost")
    if unw_q > 0:
        if unw_c >= D(arb["batch_usd"]):
            await withdraw_batch(d)
        else:
            await notify(f"ℹ️ <b>{d['coin']}</b>: покупка закончена ({why}); остаток {fmt(unw_q)} шт. "
                         f"на {fmt(unw_c)}$ меньше {arb['batch_usd']}$ — остаётся на HTX, уйдёт со следующим выводом.")
            d["unw_qty"] = d["unw_cost"] = "0"
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
    base = free - res["htx_fee"]
    unw_q, unw_c = _dd(d, "unw_qty"), _dd(d, "unw_cost")
    breakeven = unw_c / unw_q if unw_q > 0 else None
    wid, errors, amount = None, [], D(0)
    # HTX часто не отдаёт (или отдаёт неверно) комиссию вывода — при отказе
    # пробуем ещё раз с запасом 1% / 3% / 5%.
    for share in ("1", "0.99", "0.97", "0.95"):
        amount = round_down(base * D(share), res["htx_withdraw_step"])
        if amount <= 0 or amount < res["htx_min_withdraw"]:
            break
        try:
            wid = await htx_withdraw(wallet_address(cfg["net"]), hc, amount, res["htx_chain"], res["htx_fee"])
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
        await _batch_failed(d, breakeven, reason)
        return
    d["batches"].append({"id": str(wid), "at": time.time(), "amount": str(amount),
                         "qty": str(unw_q), "cost": str(unw_c), "ok": False, "fwd": False})
    d["unw_qty"] = d["unw_cost"] = "0"
    await save()
    await notify(f"📤 <b>{coin}</b>: вывожу партию {fmt(amount)} шт. (куплено на {fmt(unw_c)}$) "
                 f"в сети {res['htx_chain']}; проверю через {arb['check_sec']} сек."
                 + (" Ордер на покупку продолжает стоять." if d["order"] else ""))


async def _batch_failed(d, breakeven, reason):
    """Вывод не прошёл: покупку прекращаем, монеты на HTX продаём в ноль."""
    await _cancel_order(d)
    d["buying"] = False
    d["unw_qty"] = d["unw_cost"] = "0"
    r = {"id": uuid.uuid4().hex[:8], "coin": hcoin(d["coin"], d["cfg"]), "symbol": hsym(d["coin"], d["cfg"]),
         "breakeven": str(breakeven or 0), "reason": reason, "order_id": None, "created": time.time()}
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
    address = await mexc_deposit_address(coin, res["mexc_net"])
    d["forwarding"] = True
    await save()
    tx, sent_raw = await wallet_send_all(net, d["token"], address)
    sent = D(sent_raw) / scale
    d["forwarding"] = False
    # Помечаем партии, которые покрывает эта отправка (могли прийти сразу несколько).
    covered = D(0)
    for b in waiting:
        if covered + D(b["amount"]) * D("0.9") <= sent:
            covered += D(b["amount"])
            b["fwd"] = True
    _add(d, "forwarded", sent)
    await notify(f"🚚 <b>{coin}</b>: отправил {fmt(sent)} шт. на MEXC\ntx: <code>{tx}</code>")


# ---------- продажа на MEXC ----------

async def sell_step(d, alarm):
    if d["sell_manual"] or _dd(d, "qty") <= 0:
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
            await notify(f"ℹ️ <b>{coin}</b>: ордер на продажу на MEXC отменён вручную — дальше продаёшь сам.")
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
    if not s and not has_more:
        if d["ladder_since"] is not None:
            d["ladder_since"] = None
            if alarm["id"]:
                alarm_end(alarm["id"])
                alarm["id"] = None
        return

    # 2) Лесенка: безубыток, потом −step_pct% каждые step_sec, до пола; спам.
    if d["ladder_since"] is None:
        d["ladder_since"] = time.time()
    k = int((time.time() - d["ladder_since"]) // arb["step_sec"])
    _, asks = await mexc_depth(sym)
    my_price = D(s["price"]) if s else None
    others = [p for p, q in asks if p != my_price]
    price, floor = ladder_price(breakeven, k, arb["step_pct"], arb["floor_pct"],
                                others[0] if others else None, info["tick"])
    if s and D(s["price"]) == price and not has_more:
        return
    qty = new
    if s:
        s["self_cancel"] = True
        await mexc_cancel(sym, s["id"])
        o = await mexc_wait_final(sym, s["id"], timeout=5)
        dq, dc = o["filled"] - D(s["filled"]), o["quote"] - D(s["quote"])
        _add(d, "sold_qty", dq)
        _add(d, "proceeds", dc)
        d["sell"] = None
        qty = round_down(await mexc_free(coin) - D(d["mexc_baseline"]), info["step"])
    if not enough(qty, price):
        return
    oid = await mexc_place(sym, "SELL", "LIMIT", qty, price)
    d["sell"] = {"id": oid, "price": str(price), "qty": str(qty), "filled": "0", "quote": "0"}
    await save()
    text = (f"<b>{coin}</b>: не удалось продать на MEXC с плановой выгодой (цель {fmt(target)}). "
            f"Лимитка {fmt(qty)} шт. по {fmt(price)} ({(price / breakeven - 1) * 100:+.2f}% к безубытку "
            f"{fmt(breakeven)})" + (" — это пол, ниже не опускаю." if price <= floor else "."))
    if alarm["id"] is None:
        alarm["id"] = alarm_start(text)
    else:
        alarm_update(alarm["id"], text)


async def finish_deal(d):
    cost, proceeds = _dd(d, "cost"), _dd(d, "proceeds")
    if _dd(d, "qty") <= 0:
        return  # ничего не вывели (или вывод не прошёл — тогда итог даст аварийная продажа)
    pnl = proceeds - cost
    await notify(
        f"🏁 <b>{d['coin']}</b>: сделка завершена.\n"
        f"Куплено на HTX и выведено: {fmt(d['qty'])} шт. за {fmt(cost)}$\n"
        f"Продано на MEXC: {fmt(d['sold_qty'])} шт. за {fmt(proceeds)}$\n"
        f"Итог: <b>{pnl:+.2f}$</b> ({(pnl / cost * 100 if cost else 0):+.2f}%)")


# ---------- аварийная продажа на HTX ----------

async def run_rescue(r):
    coin, sym = r["coin"], r.get("symbol") or f"{r['coin']}usdt"
    aid = None
    try:
        info = await htx_symbol(sym)
        price = round_up(D(r["breakeven"]), info["tick"])
        if not r["order_id"]:
            free, _ = await htx_balance(coin)
            qty = round_down(free, info["step"])
            await notify(f"⚠️ <b>{coin}</b>: {r['reason']}.\nПродаю {fmt(qty)} шт. на HTX в ноль по {fmt(price)}.")
            if qty <= 0 or qty * price < info["min_value"]:
                raise ExchangeError(f"на HTX нечего продавать ({fmt(qty)} шт.)")
            r["order_id"] = await htx_place(sym, "sell-limit", qty, price)
            await save()
        started = time.time()
        while True:
            o = await htx_order(r["order_id"])
            if o["state"] == "filled":
                await notify(f"✅ <b>{coin}</b>: аварийная продажа на HTX исполнена, "
                             f"{fmt(o['filled'])} шт. на {fmt(o['cash'])}$.")
                break
            if o["state"] in ("canceled", "partial-canceled"):
                await notify(f"ℹ️ <b>{coin}</b>: аварийный ордер на HTX отменён (продано {fmt(o['filled'])} шт.). "
                             f"Дальше — вручную.")
                break
            if aid is None and time.time() - started > 10:
                aid = alarm_start(f"<b>{coin}</b>: вывод с HTX не прошёл, ордер на продажу в ноль "
                                  f"по {fmt(price)} на HTX не исполняется (продано {fmt(o['filled'])}).")
            await asyncio.sleep(5)
    except Exception as e:
        print(f"[arb] rescue {coin}: {traceback.format_exc()}", flush=True)
        text = f"<b>{coin}</b>: вывод с HTX не прошёл, и продать на HTX не получилось: <code>{e}</code>. Нужны ручные действия!"
        if aid is None:
            aid = alarm_start(text)
        # Ждём, пока спам остановят вручную — дальше решает человек.
        while aid in alarms and not alarms[aid]["acked"]:
            await asyncio.sleep(1)
    finally:
        if aid:
            alarm_end(aid)
        if r in rescues:
            rescues.remove(r)
        await save()


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
    return (f"{mark(HTX_API_KEY and HTX_API_SECRET)} ключ HTX · "
            f"{mark(MEXC_API_KEY and MEXC_API_SECRET)} ключ MEXC\n"
            f"👛 EVM (ETH/BNB/Monad): {evm}\n"
            f"👛 Sui: {sui}")


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

    # --- Сделка ---
    lines.append("\n<b>Сделка:</b>")
    if not deal:
        lines.append("нет — ждёт спред" if arb["enabled"] else "нет")
    else:
        d = deal
        lines.append(f"<b>{d['coin']}</b> · идёт {_ago(d.get('started'))}")
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
        f"продажа на MEXC: шаг −{arb['step_pct']}% каждые {arb['step_sec']} сек., пол −{arb['floor_pct']}% от безубытка",
        f"проверка вывода HTX через {arb['check_sec']} сек. · стакан MEXC при покупке: "
        f"{'учитывается' if arb.get('mexc_depth', True) else 'только лучшая цена'}",
        f"свой ордер первым в стакане HTX: {'вкл' if arb.get('maker', True) else 'выкл'} · "
        f"вывод партиями от {arb.get('batch_usd', 15):g}$",
        "",
        _keys_text(),
        "",
        "Команды: /arb_help",
    ]
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
        f"   сети: {nets_list_text()} (свои EVM-сети — /arb_net)\n"
        "   если бот не нашёл сеть сам: добавь <code>htx=код</code> и/или <code>mexc=имя</code> (см. /arb_chains)\n"
        "   если тикер на HTX другой: <code>htxcoin=ТИКЕР</code>, напр. /arb_add MON 1.5 monad htxcoin=MONAD\n"
        "/arb_del PEPE — убрать монету\n"
        "/arb_confirm PEPE — подтвердить контракт вручную, если HTX его не отдал\n"
        "/arb_htx PEPE — что HTX реально отдаёт: сети, статусы, комиссии, контракты, лимиты, адресная книга\n"
        "/arb_chains PEPE — сети монеты на HTX и MEXC и что выбрал бот\n"
        "/arb_on · /arb_off — включить/выключить автоторговлю\n"
        "/arb_live on · /arb_live off — реальные сделки / тестовый режим\n"
        "/arb_set step 0.3 · interval 120 · floor 1.2 · check 60 · poll 1 — параметры\n"
        "/arb_set depth on|off — учитывать стакан MEXC при покупке (по умолчанию on)\n"
        "/arb_set maker on|off — ставить свой ордер первым в стакане HTX (по умолчанию on)\n"
        "/arb_set batch 15 — с какой суммы купленного сразу выводить партию\n"
        "/arb_wallet — адреса и балансы кошельков бота\n"
        "/arb_net add mapo https://rpc.maplabs.io MAPO — добавить любую EVM-сеть "
        "(имя, адрес ноды, монета на газ; можно ещё названия сети на биржах через запятую: MAPO,MAP)\n"
        "/arb_net — список сетей · /arb_net del mapo — удалить свою сеть\n"
        "/arb_reset — забыть зависшую сделку (после ручного разбора)\n"
        "/stop — остановить спам\n\n"
        "<b>Как идёт сделка</b>\n"
        "1. Покупка на HTX: если есть продавцы по цене, дающей твой % к MEXC, — выкупает их сразу. "
        "Если нет — ставит свой ордер на покупку первым в стакане (на тик выше лучшего чужого, но не дороже "
        "цены с твоим %) и переставляет его, как только меняется цена на MEXC или его перебивают.\n"
        "2. Как только куплено на 15$ (/arb_set batch) — сразу вывод этой партии на кошелёк бота; "
        "ордер при этом продолжает стоять и покупать. HTX отклонил вывод или через минуту партия всё ещё "
        "на балансе — покупка останавливается, монеты продаются на HTX в ноль, не продалось — спам.\n"
        "3. Каждая дошедшая партия пересылается на депозит MEXC.\n"
        "4. На MEXC: сразу по ордерам на покупку, пока выгода ≥ твоего %. Остаток — лимитка в безубыток "
        "(или на тик ниже ближайшего продавца), каждые 2 мин. −0,3%, до −1,2%; спам до /stop или до продажи.\n"
        "5. Ордер снимается, если первым с твоим % встать уже нельзя; через минуту без возможности покупка "
        "заканчивается, сделка доводится до продажи.",
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
        net = args[2].lower()
        if net in ("bnb", "bep20"):
            net = "bsc"
        if net in ("erc20", "ethereum"):
            net = "eth"
        if net == "mon":
            net = "monad"
        if net not in NET_TITLES or not coin or pct <= 0:
            raise ValueError
        cfg = {"pct": pct, "net": net, "probe": 0, "htx_chain": None, "mexc_net": None}
        for a in args[3:]:
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
        await message.answer("❌ Пример: /arb_add PEPE 3 bsc  или  /arb_add PEPE 3 bsc 150\n"
                             f"Сети: {nets_list_text()}", parse_mode="HTML")
        return
    text = f"✅ <b>{coin}</b>: от {pct}% · {NET_TITLES[net]} · " + \
           (f"проба {cfg['probe']}$ → весь баланс" if cfg["probe"] else "сразу весь баланс")
    try:
        # Тикер пишется как на MEXC (там продаём). Нет пары на MEXC — не добавляем.
        pair_ok = True
        try:
            await mexc_symbol(f"{coin}USDT")
        except ExchangeError:
            pair_ok = False
        if not pair_ok:
            raise CoinNotOnMexc(f"пары {coin}/USDT нет на MEXC")
    except CoinNotOnMexc as e:
        await message.answer(f"❌ <b>{coin}</b> не добавлена: {e}.\n"
                             f"Пиши тикер как на MEXC — если на HTX он другой, бот найдёт его сам.",
                             parse_mode="HTML")
        return
    arb["coins"][coin] = cfg
    await save()
    try:
        res = await resolve_coin(coin, cfg)
        if res["htx_ticker_auto"]:
            await save()
            if res["htx_ticker"] != coin:
                text += f"\n🔎 На HTX эта монета — <b>{res['htx_ticker']}</b> (нашёл сам: {res['htx_ticker_auto']})"
        text += f"\nПара на HTX: <code>{res['htx_pair']}</code> · на MEXC: <code>{coin}USDT</code>"
        text += (f"\nHTX сеть: <code>{res['htx_chain']}</code> (вывод {'открыт' if res['htx_withdraw_ok'] else 'ЗАКРЫТ'} по API)"
                 f"\nMEXC сеть: <code>{res['mexc_net'].get('netWork') or res['mexc_net'].get('network')}</code>"
                 f"\n{contract_line(coin, cfg, res)}")
        try:
            addr = wallet_address(net)
            saved = await htx_address_saved(res["htx_ticker"], res["htx_chain"], addr)
            if saved is True:
                text += f"\n✅ Адрес бота есть в адресной книге вывода HTX"
            elif saved is False:
                text += "\n⛔ " + address_book_hint(res["htx_ticker"], res["htx_chain"], addr) + \
                        " Пока адреса нет, бот по этой монете не покупает."
            else:
                text += "\n❔ Адресную книгу HTX проверить не удалось (ключ HTX?)"
        except Exception as e:
            text += f"\n❔ Адрес бота: {e}"
    except Exception as e:
        text += f"\n⚠️ Проверка сетей: {e}"
    await message.answer(text, parse_mode="HTML")


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
        if not addrs:
            out.append("пусто — добавь адреса бота (/arb_wallet) в адресную книгу HTX")
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
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("arb_net"))
async def cmd_net(message: types.Message, command: CommandObject):
    if not await _guard(message):
        return
    args = (command.args or "").split()
    action = args[0].lower() if args else "list"

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
                "Пример: /arb_net add mapo https://rpc.maplabs.io MAPO\n"
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
        arb["networks"][name] = {"rpc": rpc_url, "native": native, "aliases": aliases}
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
            "batch": ("batch_usd", float)}
    args = (command.args or "").split()
    if len(args) == 2 and args[0].lower() == "maker" and args[1].lower() in ("on", "off"):
        arb["maker"] = args[1].lower() == "on"
        await save()
        await message.answer(f"✅ Свой ордер на покупку первым в стакане HTX: {'вкл' if arb['maker'] else 'выкл'}")
        return
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
            dec = 9 if net == "sui" else 18
            lines.append(f"{NET_TITLES[net]}: {fmt(D(bal) / (D(10) ** dec))} {NATIVE_COIN[net]} (на газ)")
        except Exception as e:
            lines.append(f"{NET_TITLES[net]}: {e}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("arb_reset"))
async def cmd_reset(message: types.Message, command: CommandObject):
    global deal
    if not await _guard(message):
        return
    if (command.args or "").strip().lower() != "да":
        await message.answer("Бот забудет текущую сделку (монеты/ордера останутся как есть — разбирать вручную).\n"
                             "Подтверди: /arb_reset да")
        return
    deal = None
    await save()
    await message.answer("🧹 Сделка сброшена. Перезапусти бота, если она ещё выполнялась.")


async def _stop_spam():
    n = 0
    for a in alarms.values():
        if not a["acked"]:
            a["acked"] = True
            n += 1
    return n


@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if not await _guard(message):
        return
    n = await _stop_spam()
    await message.answer(f"🔕 Спам остановлен ({n}). Как только ситуация разрешится, пришлю итог.")


@router.callback_query(F.data == "arb_stop_spam")
async def cb_stop(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    n = await _stop_spam()
    await callback.answer(f"Спам остановлен ({n})")


# ================= ЗАПУСК =================

async def start():
    """Восстанавливает состояние и запускает фоновые задачи. Возвращает их список."""
    await load()
    tasks = [asyncio.create_task(spam_loop()), asyncio.create_task(engine_loop())]
    global deal
    if deal and deal.get("v") != 2:
        await notify(f"⚠️ Незавершённая сделка <b>{deal.get('coin')}</b> из прошлой версии бота сброшена — "
                     f"проверь HTX/кошелёк/MEXC вручную.")
        deal = None
        await save()
    if deal:
        await notify(f"♻️ Бот перезапущен посреди сделки <b>{deal['coin']}</b> — продолжаю.")
        tasks.append(spawn(run_deal()))
    for r in list(rescues):
        tasks.append(spawn(run_rescue(r)))
    return tasks


BOT_COMMANDS = [
    ("arb", "Автоарбитраж: статус, спред, сделка, балансы"),
    ("arb_help", "Автоарбитраж: помощь"),
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
    ("stop", "Остановить спам"),
]
