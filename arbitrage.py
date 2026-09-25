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
    "poll_sec": 2.0,       # как часто проверять спред по монетам из списка
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
                         {"symbol": sym.lower(), "type": "step0", "depth": 20}, signed=False)
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


async def htx_contract(coin, chain):
    """Адрес контракта монеты в сети на HTX или None, если HTX его не отдал.
    Основной источник — /v1/settings/common/chains (поле ca), запасной — поля
    самой сети из /v2/reference/currencies."""
    key = (coin.lower(), chain.get("chain"))
    hit = _htx_ca_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    ca = None
    try:
        rows = await htx_req("GET", "/v1/settings/common/chains",
                             {"currency": coin.lower()}, signed=False)
        for row in rows or []:
            if row.get("chain") == chain.get("chain"):
                ca = row.get("ca") or row.get("contractAddress") or row.get("contract")
                break
    except Exception as e:
        print(f"[arb] HTX chains {coin}: {e}", flush=True)
    ca = ca or chain.get("contractAddress") or chain.get("contract") or chain.get("ca")
    ca = str(ca).strip() if ca else None
    _htx_ca_cache[key] = (time.time(), ca)
    return ca


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
    data = await mexc_req("GET", "/api/v3/depth", {"symbol": sym, "limit": 20}, signed=False)
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


# ================= СОПОСТАВЛЕНИЕ СЕТЕЙ =================

def hcoin(coin, cfg):
    """Тикер монеты на HTX. Обычно совпадает с MEXC, но не всегда: Monad на HTX —
    MONAD, на MEXC — MON (а MON на HTX — вообще другая монета, PixelMon)."""
    return (cfg.get("htx_coin") or coin).upper()


async def resolve_coin(coin, cfg):
    """Находит сеть монеты на обеих биржах и контракт токена.
    Возвращает dict или бросает ExchangeError с понятной причиной."""
    net = cfg["net"]
    hc = hcoin(coin, cfg)
    chains = await htx_chains(hc)
    if not chains:
        raise NetworkMissing(f"монеты {hc} нет на HTX — проверь тикер (как в паре {hc}/USDT на бирже); "
                             f"если на HTX тикер другой: /arb_add {coin} {cfg['pct']} {net} htxcoin=ТИКЕР")
    if cfg.get("htx_chain"):
        htx = [c for c in chains if c.get("chain") == cfg["htx_chain"]]
    else:
        htx = [c for c in chains if matches_net(
            net, c.get("baseChain"), c.get("baseChainProtocol"), c.get("displayName"), c.get("chain"))]
    found = ", ".join(c.get("chain", "?") for c in chains) or "нет ни одной"
    if not htx:
        raise NetworkMissing(f"на HTX у {hc} нет сети {NET_TITLES[net]} (есть: {found}). Если это не та монета, "
                             f"укажи тикер HTX: /arb_add {coin} {cfg['pct']} {net} htxcoin=ТИКЕР")
    if len(htx) > 1:
        raise ExchangeError(
            f"HTX: не удалось однозначно найти сеть {NET_TITLES[net]} для {hc} "
            f"(сети на HTX: {found}). Укажи вручную: /arb_add {coin} {cfg['pct']} {net} htx=<код>")
    htx = htx[0]

    nets = await mexc_networks(coin)
    if not nets:
        raise NetworkMissing(f"монеты {coin} нет на MEXC — проверь тикер (как в паре {coin}/USDT на бирже)")
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
    htx_ca = None
    if coin.upper() != NATIVE_COIN[net]:
        token = (mx.get("contract") or "").strip()
        if not token:
            raise ExchangeError(f"MEXC не отдал контракт {coin} в сети {NET_TITLES[net]}")
        # Главная защита от «один тикер — две разные монеты»: сверяем контракт.
        htx_ca = await htx_contract(hc, htx)
        if htx_ca and norm_contract(htx_ca) != norm_contract(token):
            raise ContractMismatch(
                f"контракты разные — это РАЗНЫЕ монеты!\nHTX ({hc}): <code>{htx_ca}</code>\n"
                f"MEXC ({coin}): <code>{token}</code>")
        contract_status = "ok" if htx_ca else "unknown"
    else:
        contract_status = "native"
        if net == "sui":
            token = SUI_NATIVE_TYPE

    return {
        "htx_chain": htx.get("chain"),
        "htx_withdraw_ok": htx.get("withdrawStatus") == "allowed",
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
    (mbids, _), (_, hasks) = await asyncio.gather(mexc_depth(sym), htx_depth(f"{hcoin(coin, cfg)}USDT"))
    if not mbids or not hasks:
        return None
    mexc_bid = mbids[0][0]
    max_price = mexc_bid / (1 + D(cfg["pct"]) / 100)
    if hasks[0][0] > max_price:
        return None
    spread = (mexc_bid - hasks[0][0]) / hasks[0][0] * 100
    return {"mexc_bid": mexc_bid, "max_price": max_price, "asks": hasks, "spread": spread}


def plan_buy(asks, max_price, budget):
    """Сколько монет можно купить по ордерам на продажу не дороже max_price на
    сумму не больше budget. Возвращает (кол-во, примерная стоимость)."""
    qty, cost = D(0), D(0)
    for price, level_qty in asks:
        if price > max_price:
            break
        can = (budget - cost) / price
        take = min(level_qty, can)
        if take <= 0:
            break
        qty += take
        cost += take * price
    return qty, cost


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


async def engine_loop():
    while True:
        try:
            if arb["enabled"] and deal is None and not rescues and arb["coins"]:
                for coin, cfg in list(arb["coins"].items()):
                    opp = await check_opportunity(coin, cfg)
                    if opp and await try_start(coin, cfg, opp):
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
    if not res["htx_withdraw_ok"]:
        await note_once(f"wd:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но HTX явно пишет, что вывод в сети {res['htx_chain']} закрыт — пропускаю.")
        return False
    if not res["mexc_deposit_ok"]:
        await note_once(f"dep:{coin}", f"ℹ️ <b>{coin}</b>: спред {opp['spread']:.2f}%, но на MEXC закрыт депозит в этой сети — пропускаю.")
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
        await note_once(f"dry:{coin}", (
            f"🧪 <b>ТЕСТ</b> · <b>{coin}</b>: спред {opp['spread']:.2f}% (мин. {cfg['pct']}%)\n"
            f"Купил бы на HTX по цене не выше {fmt(opp['max_price'])} "
            f"(MEXC bid {fmt(opp['mexc_bid'])}), USDT на HTX: {usdt}\n"
            f"{'Проба ' + str(cfg['probe']) + '$, потом весь баланс' if cfg.get('probe') else 'Сразу на весь баланс'}\n"
            f"Сеть: HTX <code>{res['htx_chain']}</code> → кошелёк "
            f"<code>{wallet_address(cfg['net'])}</code> → MEXC "
            f"<code>{res['mexc_net'].get('netWork') or res['mexc_net'].get('network')}</code>\n"
            f"{contract_line(coin, cfg, res)}\n"
            f"<i>Реальные сделки: /arb_live on</i>"), every=300)
        return False

    deal = {
        "id": uuid.uuid4().hex[:8], "coin": coin, "cfg": dict(cfg),
        "phase": "probe" if cfg.get("probe") else "full",
        "stage": "buy", "started": time.time(),
        "qty": "0", "cost": "0",        # купленное, чей вывод прошёл
        "expected": "0",               # сколько монет ждём на кошельке
        "wallet_baseline": None, "decimals": None, "token": res["token"],
    }
    await save()
    spawn(run_deal())
    return True


# ================= СДЕЛКА =================

async def run_deal():
    global deal
    d = deal
    handlers = {
        "buy": stage_buy, "withdraw": stage_withdraw, "withdraw_check": stage_withdraw_check,
        "wallet_wait": stage_wallet_wait, "forwarding": stage_forward, "mexc_wait": stage_mexc_wait,
        "sell": stage_sell,
    }
    while d["stage"] != "done":
        try:
            await handlers[d["stage"]](d)
            await save()
        except Exception as e:
            print(f"[arb] сделка {d['coin']} этап {d['stage']}: {traceback.format_exc()}", flush=True)
            await note_once(f"deal_err:{d['id']}:{d['stage']}",
                            f"⚠️ <b>{d['coin']}</b>, этап «{d['stage']}»: <code>{e}</code>\nПовторю через 15 сек.",
                            every=300)
            await asyncio.sleep(15)
    deal = None
    await save()


async def stage_buy(d):
    coin, cfg = d["coin"], d["cfg"]
    sym = f"{hcoin(coin, cfg)}USDT"
    budget, _ = await htx_balance("usdt")
    if d["phase"] == "probe":
        budget = min(budget, D(cfg["probe"]))
    budget *= D("0.995")  # запас на округления
    opp = await check_opportunity(coin, cfg)
    if budget < 5 or not opp:
        if d["phase"] == "full" and D(d["expected"]) > 0:
            await notify(f"ℹ️ <b>{coin}</b>: докупка на весь баланс не состоялась "
                         f"({'спред ушёл' if not opp else 'мало USDT'}), везу только пробу.")
            d["stage"] = "wallet_wait"
        else:
            await notify(f"ℹ️ <b>{coin}</b>: покупка отменена — {'спред ушёл' if not opp else 'на HTX меньше 5 USDT'}.")
            d["stage"] = "done"
        return

    info = await htx_symbol(sym)
    price = round_down(opp["max_price"], info["tick"])
    qty, _ = plan_buy(opp["asks"], price, budget)
    qty = round_down(qty, info["step"])
    if qty <= 0 or qty < info["min_qty"] or qty * price < info["min_value"]:
        d["stage"] = "wallet_wait" if D(d["expected"]) > 0 else "done"
        return

    order_id = await htx_place(sym, "buy-ioc", qty, price)
    o = await htx_wait_final(order_id)
    if o["filled"] <= 0:
        await notify(f"ℹ️ <b>{coin}</b>: ордер на HTX не исполнился (цены ушли).")
        d["stage"] = "wallet_wait" if D(d["expected"]) > 0 else "done"
        return
    avg = o["cash"] / o["filled"]
    d["cur_qty"], d["cur_cost"] = str(o["filled"]), str(o["cash"])
    await notify(
        f"🟢 <b>{coin}</b> {'проба' if d['phase'] == 'probe' else 'покупка'} на HTX: "
        f"{fmt(o['filled'])} шт. на {fmt(o['cash'])}$ · ср. цена {fmt(avg)} "
        f"(спред был {opp['spread']:.2f}%)")
    d["stage"] = "withdraw"


async def stage_withdraw(d):
    coin, cfg = d["coin"], d["cfg"]
    res = await resolve_coin(coin, cfg)
    if d["decimals"] is None:
        d["decimals"] = await wallet_decimals(cfg["net"], res["token"])
    if d["wallet_baseline"] is None:
        d["wallet_baseline"] = await wallet_balance(cfg["net"], res["token"])

    free, _ = await htx_balance(hcoin(coin, cfg))
    amount = round_down(free - res["htx_fee"], res["htx_withdraw_step"])
    if amount <= 0 or amount < res["htx_min_withdraw"]:
        await start_rescue(d, f"сумма к выводу {fmt(amount)} меньше минимума HTX {fmt(res['htx_min_withdraw'])}")
        return
    try:
        wid = await htx_withdraw(wallet_address(cfg["net"]), hcoin(coin, cfg), amount, res["htx_chain"], res["htx_fee"])
    except ExchangeError as e:
        await start_rescue(d, f"HTX отклонил заявку на вывод: {e}")
        return
    d["withdraw_id"], d["withdraw_at"], d["cur_withdraw"] = str(wid), time.time(), str(amount)
    d["stage"] = "withdraw_check"
    await notify(f"📤 <b>{coin}</b>: заявка на вывод {fmt(amount)} с HTX (сеть {res['htx_chain']}) "
                 f"принята, проверю баланс через {arb['check_sec']} сек.")


async def stage_withdraw_check(d):
    coin = d["coin"]
    wait = d["withdraw_at"] + arb["check_sec"] - time.time()
    if wait > 0:
        await asyncio.sleep(wait)
    free, frozen = await htx_balance(hcoin(coin, d["cfg"]))
    cur_withdraw = D(d["cur_withdraw"])
    avg = D(d["cur_cost"]) / D(d["cur_qty"])
    if free >= cur_withdraw * D("0.05") and free * avg >= 1:
        await start_rescue(d, f"через {arb['check_sec']} сек. свободный баланс {coin} на HTX не обнулился "
                              f"({fmt(free)} шт.) — вывод не прошёл")
        return
    d["qty"] = str(D(d["qty"]) + D(d["cur_qty"]))
    d["cost"] = str(D(d["cost"]) + D(d["cur_cost"]))
    d["expected"] = str(D(d["expected"]) + cur_withdraw)
    await notify(f"✅ <b>{coin}</b>: вывод ушёл (на HTX свободно {fmt(free)}, заморожено {fmt(frozen)}).")
    if d["phase"] == "probe":
        d["phase"] = "full"
        d["stage"] = "buy"
    else:
        d["stage"] = "wallet_wait"
        d["wallet_wait_since"] = time.time()


async def start_rescue(d, reason):
    """Вывод не удался: продаём остаток монеты на HTX в ноль. Сделка при этом
    либо завершается (если это была проба/единственная покупка), либо едет
    дальше только с тем, что уже успешно выведено."""
    r = {
        "id": uuid.uuid4().hex[:8], "coin": hcoin(d["coin"], d["cfg"]),
        "breakeven": str(D(d["cur_cost"]) / D(d["cur_qty"])),
        "reason": reason, "order_id": None, "created": time.time(),
    }
    rescues.append(r)
    await save()
    spawn(run_rescue(r))
    if D(d["expected"]) > 0:
        d["stage"] = "wallet_wait"
        d["wallet_wait_since"] = time.time()
    else:
        d["stage"] = "done"


async def run_rescue(r):
    coin, sym = r["coin"], f"{r['coin']}USDT"
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


async def stage_wallet_wait(d):
    coin, net = d["coin"], d["cfg"]["net"]
    need = int(D(d["expected"]) * D("0.95") * (D(10) ** d["decimals"]))
    since = d.setdefault("wallet_wait_since", time.time())
    warned = False
    while True:
        bal = await wallet_balance(net, d["token"])
        if bal - d["wallet_baseline"] >= need:
            await notify(f"👛 <b>{coin}</b>: монеты на кошельке ({fmt(D(bal) / (D(10) ** d['decimals']))}), пересылаю на MEXC.")
            d["stage"] = "forwarding"
            d["forward_sent"] = False
            return
        if not warned and time.time() - since > 1800:
            warned = True
            await notify(f"⏳ <b>{coin}</b>: за 30 минут монеты так и не пришли на кошелёк "
                         f"<code>{wallet_address(net)}</code> ({NET_TITLES[net]}). Продолжаю ждать.")
        await asyncio.sleep(10)


async def stage_forward(d):
    coin, net = d["coin"], d["cfg"]["net"]
    scale = D(10) ** d["decimals"]
    if d.get("forward_sent"):
        # Рестарт посреди пересылки: если монет на кошельке уже почти нет —
        # транзакция ушла, ждём зачисления; иначе отправляем заново.
        bal = await wallet_balance(net, d["token"])
        if D(bal) < D(d["expected"]) * scale * D("0.1"):
            d.setdefault("forwarded", d["expected"])
            d["stage"] = "mexc_wait"
            return
    res = await resolve_coin(coin, d["cfg"])
    address = await mexc_deposit_address(coin, res["mexc_net"])
    d["mexc_baseline"] = str(await mexc_free(coin))
    d["forward_sent"] = True
    await save()
    tx, amount = await wallet_send_all(net, d["token"], address)
    d["forwarded"] = str(D(amount) / scale)
    d["stage"] = "mexc_wait"
    d["mexc_wait_since"] = time.time()
    await notify(f"🚚 <b>{coin}</b>: отправил {fmt(d['forwarded'])} на MEXC <code>{address}</code>\ntx: <code>{tx}</code>")


async def stage_mexc_wait(d):
    coin = d["coin"]
    need = D(d["forwarded"]) * D("0.95")
    since = d.setdefault("mexc_wait_since", time.time())
    warned = False
    while True:
        free = await mexc_free(coin)
        got = free - D(d["mexc_baseline"])
        if got >= need:
            d["received"] = str(got)
            d["stage"] = "sell"
            await notify(f"🏦 <b>{coin}</b>: зачислено на MEXC {fmt(got)} шт., продаю.")
            return
        if not warned and time.time() - since > 1800:
            warned = True
            await notify(f"⏳ <b>{coin}</b>: за 30 минут депозит на MEXC так и не зачислен. Продолжаю ждать.")
        await asyncio.sleep(10)


async def stage_sell(d):
    coin, cfg = d["coin"], d["cfg"]
    sym = f"{coin}USDT"
    info = await mexc_symbol(sym)
    breakeven = D(d["cost"]) / D(d["qty"])
    target = breakeven * (1 + D(cfg["pct"]) / 100)
    left = round_down(min(D(d["received"]), await mexc_free(coin)), info["step"])
    proceeds = D(d.get("proceeds") or 0)

    # Рестарт посреди продажи: снимаем свой висящий ордер, начинаем заново.
    if d.get("sell_order"):
        await mexc_cancel(sym, d["sell_order"])
        o = await mexc_wait_final(sym, d["sell_order"], timeout=5)
        proceeds += o["quote"]
        d["sell_order"] = None
        left = round_down(await mexc_free(coin), info["step"])

    def done_enough(qty, price):
        return qty <= 0 or qty * price < info["min_value"]

    # 1) Моментально по ордерам на покупку, пока цена даёт минимальный %.
    bids, _ = await mexc_depth(sym)
    if bids and bids[0][0] >= target and not d.get("instant_done"):
        price = round_up(target, info["tick"])
        oid = await mexc_place(sym, "SELL", "IMMEDIATE_OR_CANCEL", left, price)
        o = await mexc_wait_final(sym, oid)
        left -= o["filled"]
        proceeds += o["quote"]
        await notify(f"💰 <b>{coin}</b>: продано сразу {fmt(o['filled'])} шт. на {fmt(o['quote'])}$ (≥ {fmt(price)}).")
    d["instant_done"] = True
    d["proceeds"] = str(proceeds)

    if done_enough(left, breakeven):
        await finish_deal(d, proceeds)
        return

    # 2) Лесенка + спам.
    aid = alarm_start(f"<b>{coin}</b>: не удалось продать на MEXC с плановой выгодой "
                      f"(цель {fmt(target)}, безубыток {fmt(breakeven)}). Осталось {fmt(left)} шт., "
                      f"работает лесенка.")
    k = 0
    order_id, order_price = None, None
    try:
        while True:
            _, asks = await mexc_depth(sym)
            others = [p for p, q in asks if p != order_price]
            best_other = others[0] if others else None
            price, floor = ladder_price(breakeven, k, arb["step_pct"], arb["floor_pct"],
                                        best_other, info["tick"])
            if price != order_price:
                if order_id:
                    await mexc_cancel(sym, order_id)
                    o = await mexc_wait_final(sym, order_id, timeout=5)
                    left -= o["filled"]
                    proceeds += o["quote"]
                    d["proceeds"] = str(proceeds)
                    order_id = d["sell_order"] = None
                    if done_enough(left, breakeven):
                        break
                left = round_down(left, info["step"])
                order_id = await mexc_place(sym, "SELL", "LIMIT", left, price)
                order_price = price
                d["sell_order"] = order_id
                await save()
                pct = (price / breakeven - 1) * 100
                alarm_update(aid, f"<b>{coin}</b>: не удалось продать на MEXC с плановой выгодой "
                                  f"(цель {fmt(target)}). Лимитка {fmt(left)} шт. по {fmt(price)} "
                                  f"({pct:+.2f}% к безубытку {fmt(breakeven)})"
                                  + (" — это пол, ниже не опускаю." if price <= floor else "."))
            # Ждём step_sec, следя за исполнением.
            deadline = time.time() + arb["step_sec"]
            filled = False
            while time.time() < deadline:
                await asyncio.sleep(5)
                o = await mexc_order(sym, order_id)
                if o["status"] == "FILLED":
                    left -= o["filled"]
                    proceeds += o["quote"]
                    filled = True
                    break
                if o["status"] in ("CANCELED", "PARTIALLY_CANCELED"):
                    # Отменили руками на бирже — отдаём управление человеку.
                    left -= o["filled"]
                    proceeds += o["quote"]
                    await notify(f"ℹ️ <b>{coin}</b>: ордер на MEXC отменён вручную, бот прекращает продажу.")
                    filled = True
                    break
            if filled:
                d["sell_order"] = None
                break
            if price > floor:
                k += 1
    finally:
        alarm_end(aid)
    d["proceeds"] = str(proceeds)
    await finish_deal(d, proceeds)


async def finish_deal(d, proceeds):
    cost = D(d["cost"])
    pnl = proceeds - cost
    await notify(
        f"🏁 <b>{d['coin']}</b>: сделка завершена.\n"
        f"Куплено на HTX: {fmt(d['qty'])} шт. за {fmt(cost)}$\n"
        f"Продано на MEXC за: {fmt(proceeds)}$\n"
        f"Итог (без учёта остатков): <b>{pnl:+.2f}$</b> ({(pnl / cost * 100 if cost else 0):+.2f}%)")
    d["stage"] = "done"


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


def _coins_text():
    if not arb["coins"]:
        return "— список пуст (/arb_add)"
    rows = []
    for coin, c in sorted(arb["coins"].items()):
        extra = f", проба {c['probe']}$" if c.get("probe") else ", сразу весь баланс"
        manual = []
        if c.get("htx_chain"):
            manual.append(f"htx={c['htx_chain']}")
        if c.get("mexc_net"):
            manual.append(f"mexc={c['mexc_net']}")
        if c.get("htx_coin"):
            manual.append(f"на HTX тикер {c['htx_coin']}")
        rows.append(f"• <b>{coin}</b> — от {c['pct']}%, {NET_TITLES[c['net']]}{extra}"
                    + (f" ({' '.join(manual)})" if manual else ""))
    return "\n".join(rows)


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
    if not await _guard(message):
        return
    if deal:
        deal_text = f"{deal['coin']} — этап «{deal['stage']}», фаза {deal['phase']}"
    else:
        deal_text = "нет"
    await message.answer(
        "🤖 <b>Автоарбитраж HTX → кошелёк → MEXC</b>\n"
        f"Состояние: <b>{'ВКЛ' if arb['enabled'] else 'ВЫКЛ'}</b> · "
        f"режим: <b>{'🧪 ТЕСТ (ничего не покупает)' if arb['dry_run'] else '💸 РЕАЛЬНЫЕ СДЕЛКИ'}</b>\n"
        f"Активная сделка: {deal_text}\n"
        f"Аварийных продаж на HTX: {len(rescues)} · тревог: {len(alarms)}\n\n"
        f"<b>Монеты:</b>\n{_coins_text()}\n\n"
        f"<b>Продажа на MEXC:</b> шаг −{arb['step_pct']}% каждые {arb['step_sec']} сек., "
        f"пол −{arb['floor_pct']}% от безубытка\n"
        f"<b>Проверка вывода HTX:</b> через {arb['check_sec']} сек.\n\n"
        f"{_keys_text()}\n\n"
        "Команды: /arb_help",
        parse_mode="HTML")


@router.message(Command("arb_help"))
async def cmd_help(message: types.Message):
    if not await _guard(message):
        return
    await message.answer(
        "📖 <b>Команды автоарбитража</b>\n"
        "/arb — статус\n"
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
        "/arb_set step 0.3 · interval 120 · floor 1.2 · check 60 — параметры\n"
        "/arb_wallet — адреса и балансы кошельков бота\n"
        "/arb_net add mapo https://rpc.maplabs.io MAPO — добавить любую EVM-сеть "
        "(имя, адрес ноды, монета на газ; можно ещё названия сети на биржах через запятую: MAPO,MAP)\n"
        "/arb_net — список сетей · /arb_net del mapo — удалить свою сеть\n"
        "/arb_reset — забыть зависшую сделку (после ручного разбора)\n"
        "/stop — остановить спам\n\n"
        "<b>Как идёт сделка</b>\n"
        "1. Спред (MEXC bid к HTX ask) ≥ заданного % → бот выкупает ордера на продажу на HTX "
        "по цене не выше той, где спред ещё равен минимуму.\n"
        "2. Вывод на кошелёк бота. HTX отклонил заявку или через минуту свободный баланс монеты "
        "не обнулился → продажа на HTX в ноль; не продалось → спам.\n"
        "3. Монеты на кошельке → пересылка на депозитный адрес MEXC.\n"
        "4. На MEXC: сразу по ордерам на покупку, пока выгода ≥ минимума. Остаток — лимитка в безубыток "
        "(или на тик ниже ближайшего продавца, если он стоит ниже), каждые 2 мин. −0,3%, до −1,2%. "
        "Не удалось взять плановую выгоду → спам до /stop или до продажи.",
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
    arb["coins"][coin] = cfg
    await save()
    text = f"✅ <b>{coin}</b>: от {pct}% · {NET_TITLES[net]} · " + \
           (f"проба {cfg['probe']}$ → весь баланс" if cfg["probe"] else "сразу весь баланс")
    try:
        res = await resolve_coin(coin, cfg)
        text += (f"\nHTX сеть: <code>{res['htx_chain']}</code> (вывод {'открыт' if res['htx_withdraw_ok'] else 'ЗАКРЫТ'} по API)"
                 f"\nMEXC сеть: <code>{res['mexc_net'].get('netWork') or res['mexc_net'].get('network')}</code>"
                 f"\n{contract_line(coin, cfg, res)}")
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
            out.append(f"• <code>{row.get('chain')}</code>: " + (f"<code>{ca}</code>" if ca else "контракта нет"))
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
            "spam": ("spam_sec", float)}
    args = (command.args or "").split()
    try:
        key, conv = keys[args[0].lower()]
        arb[key] = abs(conv(float(args[1].replace(",", "."))))
    except Exception:
        await message.answer("Пример: /arb_set step 0.3 · /arb_set interval 120 · "
                             "/arb_set floor 1.2 · /arb_set check 60 · /arb_set spam 1")
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
    if deal and deal.get("stage") != "done":
        await notify(f"♻️ Бот перезапущен посреди сделки <b>{deal['coin']}</b> "
                     f"(этап «{deal['stage']}») — продолжаю.")
        tasks.append(spawn(run_deal()))
    for r in list(rescues):
        tasks.append(spawn(run_rescue(r)))
    return tasks


BOT_COMMANDS = [
    ("arb", "Автоарбитраж: статус"),
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
