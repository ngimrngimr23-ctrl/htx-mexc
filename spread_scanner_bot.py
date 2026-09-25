import asyncio
import aiohttp
import json
import re
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand
from aiohttp import web
import time
import os
import hmac
import hashlib
import urllib.parse

import arbitrage
import scanner_quality as sq

# ================= НАСТРОЙКИ =================
# ВАЖНО: токен ТОЛЬКО из переменной окружения. Никогда не хардкодь его в файле,
# иначе при пуше на GitHub он утечёт даже из приватного репозитория. На Render:
# Settings -> Environment -> добавь BOT_TOKEN.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

# Опционально: read-only ключ MEXC для проверки реальных контрактов монет
# (эндпоинт /api/v3/capital/config/getall — ПОДПИСЫВАЕМЫЙ, без ключа недоступен).
# Если не заданы — бот просто не проверяет контракты и работает как раньше.
MEXC_API_KEY = os.environ.get("MEXC_API_KEY")
MEXC_API_SECRET = os.environ.get("MEXC_API_SECRET")

# Опционально: Upstash Redis (REST API) для сохранения настроек/ЧС/мутов между
# перезапусками (Render на бесплатном тарифе перезапускает процесс регулярно —
# без этого все настройки, чёрный список и муты слетали бы каждый раз).
# Если не заданы — бот просто работает в памяти, как раньше, без персистентности.
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

# HTX (api.huobi.pro) стоит за Cloudflare, и без "браузерного" User-Agent он
# иногда блокирует запросы как ботов (403 + капча-страница вместо JSON) — из-за
# этого часть монет могла молча не попадать в кэш статуса ввода/вывода.
HTX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# Таймауты объектами ClientTimeout, а не голым числом: число aiohttp 3.x ещё
# принимает (заворачивает в total=), но в 4.x это уже ошибка.
# Метка сборки. Нужна, чтобы по одному сообщению бота было видно, какой код
# реально крутится: без неё старый и новый деплой по алертам почти неотличимы,
# и «баг» легко перепутать с «Render не передеплоился». RENDER_GIT_COMMIT Render
# подставляет сам; BUILD_TAG бампаем руками при значимых изменениях логики.
BUILD_TAG = "2026-09-26 depth-routes"
BUILD_COMMIT = (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or "локально"

TIMEOUT_BULK = aiohttp.ClientTimeout(total=15)
TIMEOUT_HEAVY = aiohttp.ClientTimeout(total=20)
TIMEOUT_REDIS = aiohttp.ClientTimeout(total=10)
TIMEOUT_DEPTH = aiohttp.ClientTimeout(total=8)

settings = {
    # ПЕРВИЧНЫЙ критерий: мин. % спреда между MEXC и HTX (в ЛЮБУЮ из двух сторон),
    # чтобы сработал алерт. Спред считается как отношение (цена продажи - цена
    # покупки) / цена покупки * 100, где покупка идёт по ask, продажа — по bid
    # (реальные исполнимые цены топа стакана, а не last price).
    "spread_percent": 1.0,

    # Отдельный порог СПЕЦИАЛЬНО для направления "купить на MEXC → продать на
    # HTX" — если > 0, используется вместо spread_percent именно для этого
    # направления (например, если тут выше комиссии/риски и нужен порог строже).
    # 0 = не задан отдельно, используется общий spread_percent, как раньше.
    "spread_percent_mexc_to_htx": 0.0,

    # Мин. объём торгов за 24ч в $ — ОБЯЗАН выполняться на ОБЕИХ биржах разом,
    # иначе пара считается неликвидной (спред может быть просто "фантомным" —
    # широкий стакан без реальной глубины) и пропускается.
    "min_volume": 100000,

    "check_interval": 4,     # Как часто проверять (сек) — bulk-эндпоинты дешёвые,
                             # можно опрашивать чаще без риска упереться в рейт-лимит (/int)

    # Искать ли спреды в направлении MEXC→HTX (/dir). Автоарбитраж работает только
    # HTX→MEXC, и если обратные сигналы не нужны — это просто шум.
    "scan_mexc_to_htx": True,

    # Торговые комиссии (taker, %) для оценки чистого спреда (/fee).
    "fee_htx_pct": 0.2,
    "fee_mexc_pct": 0.05,
    "cooldown_min": 10,      # Мин. пауза между повторными алертами по одной паре

    # Спред должен непрерывно держаться выше порога (/sp) хотя бы это число
    # секунд, прежде чем бот отправит алерт — отсекает случайные разовые скачки
    # цены на долю секунды, которые физически не успеть исполнить руками.
    # 0 = выключено (алерт сразу же, как раньше).
    "spread_stable_sec": 0,

    # Мин. сумма в $, которую реально можно "прокрутить" (min объёма на бид/аск
    # с обеих сторон по глубине стакана) — отсекает пары, где спред красивый на
    # бумаге, но исполнить его целиком нельзя из-за тонкого стакана.
    # 0 = выключено (фильтр не применяется, сумма просто показывается в алерте).
    "min_turnover_usd": 0,

    # ВЕРХНЯЯ отсечка спреда, %. Если тикер на биржах совпал, а актив по факту
    # разный (разная деноминация — классика 1000SATS против SATS; или тикер
    # переиспользован после ребренда токена), спред считается в сотни-тысячи
    # процентов и приходит как самый жирный сигнал в списке. Реальный арбитраж
    # таких величин на ликвидных парах не даёт. 0 = отсечку не применять.
    "max_spread_percent": 50.0,

    # Фильтр по факту возможности перевода монеты. ВАЖНО: реально проверяется
    # ТОЛЬКО сторона HTX (публичный эндпоинт, без API-ключа). Статус MEXC без
    # приватного API-ключа недоступен в принципе — эта сторона в алерте всегда
    # помечается как "не проверяется", её нужно смотреть на бирже вручную.
    "require_transferable": True,

    # Не слать сигналы, если контракты монеты на HTX и MEXC РАЗНЫЕ — значит, это
    # разные монеты с одним тикером (/ca). Где контракт не отдан — не режем.
    "check_contracts": True,

    "chat_id": None,
    "channel_id": None,
}

def apply_env_overrides():
    """Позволяет задать любую настройку переменной окружения BOT_<КЛЮЧ>, напр.
    BOT_MIN_TURNOVER_USD=250, BOT_SPREAD_PERCENT=1.5, BOT_MIN_VOLUME=100000.

    Зачем: на бесплатном Render процесс перезапускается регулярно, и без Upstash
    все настройки, выставленные командами, каждый раз откатывались к дефолтам —
    выглядело это как «я задал /mt 250, а бот его игнорирует». Переменные
    окружения переживают перезапуск всегда. Приоритет: дефолты → env → Upstash
    (живое состояние из команд важнее, поэтому оно применяется последним)."""
    for key, default in list(settings.items()):
        raw = os.environ.get(f"BOT_{key.upper()}")
        if raw is None or raw == "":
            continue
        try:
            if isinstance(default, bool):
                settings[key] = raw.strip().lower() in ("1", "true", "yes", "on", "да")
            elif isinstance(default, int) and not isinstance(default, bool):
                settings[key] = int(float(raw.replace(",", ".")))
            elif isinstance(default, float):
                settings[key] = float(raw.replace(",", "."))
            else:
                settings[key] = raw
            print(f"env-override: {key} = {settings[key]}", flush=True)
        except Exception as e:
            print(f"env-override: не разобрал BOT_{key.upper()}={raw!r}: {e}", flush=True)


blacklist = set()

# symbol -> timestamp (unix), до которого монета замьючена. Отличается от
# blacklist тем, что снимается автоматически по истечении времени, не навсегда.
muted_until = {}
# symbol -> {"time": ts первого алерта, "last_msg": ts последнего алерта, "spread": % на момент последнего алерта}
alert_memory = {}

# symbol -> ts, когда спред НЕПРЕРЫВНО начал держаться выше порога (для /ss).
# Сбрасывается, как только спред падает ниже порога хоть на одном проходе.
spread_track = {}

# Кэш статуса ввода/вывода с HTX: {"ts": fetched_at, "data": {"BTC": {"deposit": bool, "withdraw": bool}, ...}}
# Обновляется редко (см. HTX_TRANSFER_TTL) — этот статус почти не меняется в течение дня,
# незачем дёргать эндпоинт каждый проход сканера.
htx_transfer_cache = {"ts": 0.0, "data": {}}
HTX_TRANSFER_TTL = 600  # 10 минут

# Сколько кандидатов за проход догружаем стаканами (по 2 запроса на кандидата).
MAX_DEPTH_CANDIDATES = 25

mexc_contracts_cache = {"ts": 0.0, "data": {}}
MEXC_CONTRACTS_TTL = 6 * 3600  # 6 часов — сети/контракты почти никогда не меняются

# Кэш суточных объёмов MEXC: {"ts": fetched_at, "data": {symbol: quoteVolume}}.
# /api/v3/ticker/24hr — самый тяжёлый ответ у MEXC (все пары разом, ~1 МБ), а
# нужен он только ради фильтра ликвидности (/v). Объём за 24ч физически не может
# заметно измениться за 10 секунд, поэтому тянуть его каждый проход — чистая
# трата трафика и времени. Цены (bookTicker) при этом обновляются каждый проход.
mexc_vol_cache = {"ts": 0.0, "data": {}}
MEXC_VOL_TTL = 60  # 1 минута

debug_stats = {
    "ts": 0.0,
    "mexc_ok": False,
    "huobi_ok": False,
    "mexc_contracts_ok": False,
    "htx_transfer_ok": False,
    "common_pairs": 0,
    "passed_volume_floor": 0,
    "passed_spread_filter": 0,
    "passed_stability": 0,
    "passed_turnover_filter": 0,
    "passed_transfer_check": 0,
    "blocked_by_transfer": 0,
    "passed_cooldown": 0,
    "alerts_sent": 0,
    "last_error": None,
    # Сколько записей молча выброшено при разборе ответов бирж. Раньше такие
    # пропуски были полностью невидимы (голый except ... continue), из-за чего
    # сломанный парсинг статусов перевода жил незамеченным.
    "skipped_mexc": 0,
    "skipped_htx": 0,
    "skipped_htx_transfer": 0,
    "skip_reason": None,
    # Отсечено как заведомо не-арбитраж (разная деноминация тикера и т.п.)
    "blocked_by_sanity": 0,
    # Отсечено фильтром /mt из-за того, что глубину посчитать не удалось
    "blocked_by_unknown_depth": 0,
    # По скольким парам пришлось дозапрашивать стакан HTX (в норме 0 — размеры
    # приходят в bulk-ответе; стабильно ненулевое значение = HTX не отдаёт
    # bidSize/askSize, и точечные запросы вернулись в горячий путь)
    "depth_fallback": 0,
    "blocked_by_contract": 0,
    "route_unmatched": 0,
    "blocked_by_net": 0,
    # Что HTX реально отдал по сетям (заполняет get_htx_transfer_status)
    "htx_data": {},
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_router(arbitrage.router)


# Если задан ARB_ADMIN_IDS — бот отвечает ТОЛЬКО этим людям. Без этого любой,
# кто найдёт бота в поиске, мог менять настройки, а /start перенаправлял бы
# все сообщения (включая сделки автоарбитража) в его чат.
@dp.message.outer_middleware()
async def _only_admins_messages(handler, event, data):
    if arbitrage.ADMIN_IDS and (event.from_user is None or event.from_user.id not in arbitrage.ADMIN_IDS):
        return None
    return await handler(event, data)


@dp.callback_query.outer_middleware()
async def _only_admins_callbacks(handler, event, data):
    if arbitrage.ADMIN_IDS and event.from_user.id not in arbitrage.ADMIN_IDS:
        return None
    return await handler(event, data)

# Одна общая HTTP-сессия на всё время жизни бота вместо создания новой сессии
# (= новый TCP+TLS handshake) на КАЖДЫЙ запрос. Инициализируется в main() —
# держит пул соединений (keep-alive) и DNS-кэш, заметно снижает задержку
# повторных обращений к тем же хостам.
http_session: aiohttp.ClientSession | None = None


def fmt_money(x):
    """Компактный формат суммы в $: без десятичных для крупных чисел, 2 знака для мелких."""
    try:
        x = float(x)
    except Exception:
        return str(x)
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.2f}"


def normalize_pair(raw: str) -> str:
    """Приводит ввод пользователя к формату пары бирж ("BTCUSDT"). Раньше сюда
    просто клали .upper() от аргумента команды — если пользователь писал монету
    как "$BTC", "BTC/USDT", "btc-usdt" и т.п., в ЧС/мут попадала строка вроде
    "$BTCUSDT" или "BTC/USDT", которая НИКОГДА не совпадала с реальным ключом
    пары ("BTCUSDT") из данных бирж — команда отвечала "успехом", а по факту
    ничего не блокировала. Теперь сначала вырезаем всё, кроме букв/цифр."""
    coin = re.sub(r"[^A-Z0-9]", "", raw.upper())
    return coin if coin.endswith("USDT") else f"{coin}USDT"


def fmt_price(x):
    """Цена без обрезания значимых знаков — у мелких монет 6-8 знаков после запятой важны."""
    try:
        x = float(x)
    except Exception:
        return str(x)
    if x >= 1:
        return f"{x:,.4f}".rstrip('0').rstrip('.')
    return f"{x:.8f}".rstrip('0').rstrip('.')


# ================= TELEGRAM UI =================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    settings["chat_id"] = message.chat.id
    stable_display = "Выкл" if settings["spread_stable_sec"] == 0 else f"{settings['spread_stable_sec']} сек"
    turnover_display = "Выкл" if settings["min_turnover_usd"] == 0 else f"{settings['min_turnover_usd']:,.0f}$"
    spm_display = "Выкл (общий /sp)" if settings["spread_percent_mexc_to_htx"] == 0 else f"{settings['spread_percent_mexc_to_htx']}%"
    spmax_display = "Выкл" if settings["max_spread_percent"] == 0 else f"{settings['max_spread_percent']}%"
    await message.answer(
        "🔀 <b>Спред-сканер MEXC ⇄ HTX (Huobi) запущен</b>\n"
        "Ищет расхождение цены по ВСЕМ общим USDT-парам на обеих биржах. "
        "Спред считается по реальным исполнимым ценам топа стакана (bid/ask), "
        "в обе стороны — берётся направление с большим спредом.\n\n"

        "⚙️ <b>Команды</b>\n"
        f"/sp 1.5 — мин. ЧИСТЫЙ % спреда (после торговых комиссий и комиссии вывода), общий для обоих направлений\n"
        f"   └ сейчас: <b>{settings['spread_percent']}%</b>\n"
        f"/spm 2 — отдельный порог именно для направления MEXC→HTX (0 = использовать общий /sp)\n"
        f"   └ сейчас: <b>{spm_display}</b>\n"
        f"/spmax 50 — верхняя отсечка: спреды выше этого % считаются не-арбитражем (разная деноминация одноимённых тикеров), 0 = выключить\n"
        f"   └ сейчас: <b>{spmax_display}</b>\n"
        f"/v 100000 — мин. объём торгов за 24ч в $, обязателен на ОБЕИХ биржах сразу (фильтр ликвидности/фантомных спредов)\n"
        f"   └ сейчас: <b>{settings['min_volume']:,}$</b>\n"
        f"/mt 500 — мин. сумма в $, которую реально можно прокрутить по глубине стакана (0 = не фильтровать, просто показывать)\n"
        f"   └ сейчас: <b>{turnover_display}</b>\n"
        f"/cd 10 — пауза между повторными алертами по одной и той же паре, в минутах\n"
        f"   └ сейчас: <b>{settings['cooldown_min']} мин</b>\n"
        f"/ss 30 — спред должен непрерывно держаться выше порога минимум N секунд перед алертом (0 = выключить)\n"
        f"   └ сейчас: <b>{stable_display}</b>\n"
        f"/int 4 — как часто проверять спреды, сек\n"
        f"   └ сейчас: <b>{settings['check_interval']} сек</b>\n"
        f"/dir — вкл/выкл сигналы MEXC→HTX (HTX→MEXC есть всегда)\n"
        f"   └ сейчас: <b>{'оба направления' if settings['scan_mexc_to_htx'] else 'только HTX→MEXC'}</b>\n"
        f"/fee 0.2 0.05 — торговые комиссии HTX и MEXC (%) для чистого спреда\n"
        f"/tr — вкл/выкл фильтр «есть общая открытая сеть для перевода»\n"
        f"   └ сейчас: <b>{'Вкл' if settings['require_transferable'] else 'Выкл'}</b>\n"
        f"/ca — вкл/выкл отсев монет с разными контрактами на HTX и MEXC\n"
        f"   └ сейчас: <b>{'Вкл' if settings['check_contracts'] else 'Выкл'}</b>\n"
        f"/b BTC — добавить/убрать монету из чёрного списка (повторный вызов с той же монетой снимает её)\n"
        f"   └ в ЧС сейчас: <b>{len(blacklist)} шт.</b>\n"
        f"/bl — показать список монет в ЧС (проверить, что реально добавилось)\n"
        f"/mute BTC 30 — замьютить монету ВРЕМЕННО на N минут (снимается само)\n"
        f"   └ в муте сейчас: <b>{len(muted_until)} шт.</b>\n"
        f"/unmute BTC — снять мут досрочно\n"
        f"/channel @имя_канала — куда дублировать сигналы (пусто = выкл)\n"
        f"   └ сейчас: <b>{settings['channel_id'] or 'Не задан'}</b>\n"
        f"/s — текущий статус настроек\n"
        f"/debug — воронка последнего прохода сканера (диагностика, если алертов нет)\n"
        f"/arb — автоарбитраж HTX → кошелёк → MEXC (помощь: /arb_help)\n\n"

        f"💾 <b>Upstash</b>: {'подключён — настройки/ЧС/муты переживут перезапуск' if UPSTASH_REDIS_REST_URL else 'не настроен — состояние в памяти, слетит при перезапуске'}\n\n"

        "⚠️ <b>Важно понимать</b>\n"
        "Это спред между ценами В МОМЕНТ ЗАПРОСА, без учёта комиссий за сделки "
        "(обычно ~0.1-0.2% на каждой бирже) и БЕЗ учёта времени перевода монеты "
        "между биржами.\n\n"
        "🚚 <b>Фильтр перевода (/tr)</b>: сигнал проходит, только если есть "
        "ОДНА сеть, где на бирже покупки открыт вывод, а на бирже продажи — "
        "депозит (сторона MEXC — по ключу MEXC_API_KEY). Статусы HTX — со слов "
        "HTX, они бывают неточны. Сколько данных HTX реально отдаёт — в /debug.",
        parse_mode="HTML")


@dp.message(Command("channel"))
async def set_channel(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args:
        settings["channel_id"] = command.args
        await message.answer(f"✅ Канал установлен: <b>{command.args}</b>\n<i>Сделай бота админом канала!</i>", parse_mode="HTML")
    else:
        settings["channel_id"] = None
        await message.answer("✅ Дублирование в канал <b>ОТКЛЮЧЕНО</b>", parse_mode="HTML")
    schedule_save()


@dp.message(Command("sp"))
async def set_spread(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    try:
        val = abs(float(command.args.replace(',', '.')))
        settings["spread_percent"] = val
        await message.answer(f"✅ Мин. ЧИСТЫЙ % спреда для алерта (после всех комиссий): <b>{val}%</b>", parse_mode="HTML")
        schedule_save()
    except Exception:
        await message.answer("❌ Ошибка. Пример: /sp 1.5")


@dp.message(Command("spm"))
async def set_spread_mexc_to_htx(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    try:
        val = max(0.0, float(command.args.replace(',', '.')))
        settings["spread_percent_mexc_to_htx"] = val
        if val == 0:
            await message.answer("✅ Отдельный порог для MEXC→HTX <b>ВЫКЛЮЧЕН</b> — используется общий /sp", parse_mode="HTML")
        else:
            await message.answer(f"✅ Порог именно для направления MEXC→HTX: <b>{val}%</b> (для HTX→MEXC остаётся общий /sp)", parse_mode="HTML")
        schedule_save()
    except Exception:
        await message.answer("❌ Ошибка. Пример: /spm 2 (0 = выключить, использовать общий /sp)")


@dp.message(Command("spmax"))
async def set_spread_max(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    try:
        val = max(0.0, float(command.args.replace(',', '.')))
        settings["max_spread_percent"] = val
        if val == 0:
            await message.answer(
                "⚠️ Верхняя отсечка спреда <b>ВЫКЛЮЧЕНА</b> — в алерты снова смогут "
                "попадать пары с одинаковым тикером, но разным активом/деноминацией "
                "(спреды в сотни процентов)", parse_mode="HTML")
        else:
            await message.answer(
                f"✅ Спреды выше <b>{val}%</b> отбрасываются как заведомо не-арбитраж "
                f"(разная деноминация одноимённых тикеров и т.п.)", parse_mode="HTML")
        schedule_save()
    except Exception:
        await message.answer("❌ Ошибка. Пример: /spmax 50 (0 = выключить отсечку)")


@dp.message(Command("v"))
async def set_volume(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.isdigit():
        settings["min_volume"] = int(command.args)
        await message.answer(f"✅ Мин. объём 24ч (на ОБЕИХ биржах): <b>{settings['min_volume']:,}$</b>", parse_mode="HTML")
        schedule_save()
    else:
        await message.answer("❌ Ошибка. Пример: /v 100000")


@dp.message(Command("ss"))
async def set_spread_stable(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.lstrip('-').isdigit():
        val = max(0, int(command.args))
        settings["spread_stable_sec"] = val
        spread_track.clear()  # старые отметки времени были для другого порога — сбрасываем
        if val == 0:
            await message.answer("✅ Фильтр стабильности спреда <b>ВЫКЛЮЧЕН</b> (алерт сразу, как только спред превысит порог)", parse_mode="HTML")
        else:
            await message.answer(f"✅ Спред должен непрерывно держаться выше порога минимум <b>{val} сек</b> перед алертом", parse_mode="HTML")
        schedule_save()
    else:
        await message.answer("❌ Ошибка. Пример: /ss 30 (0 = выключить)")


@dp.message(Command("mt"))
async def set_min_turnover(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.replace('.', '', 1).isdigit():
        val = max(0.0, float(command.args))
        settings["min_turnover_usd"] = val
        if val == 0:
            await message.answer("✅ Фильтр мин. оборота <b>ВЫКЛЮЧЕН</b> (сумма для прокрутки просто показывается в алерте, не фильтрует)", parse_mode="HTML")
        else:
            await message.answer(f"✅ Мин. сумма для прокрутки (по глубине стакана): <b>{val:,.0f}$</b> — пары с меньшей глубиной не алертятся", parse_mode="HTML")
        schedule_save()
    else:
        await message.answer("❌ Ошибка. Пример: /mt 500 (0 = выключить)")


@dp.message(Command("cd"))
async def set_cooldown(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.isdigit():
        settings["cooldown_min"] = int(command.args)
        await message.answer(f"✅ Пауза между повторными алертами: <b>{settings['cooldown_min']} мин</b>", parse_mode="HTML")
        schedule_save()
    else:
        await message.answer("❌ Ошибка. Пример: /cd 10")


@dp.message(Command("int"))
async def set_interval(message: types.Message, command: CommandObject):
    try:
        val = max(2, int(float((command.args or "").replace(",", "."))))
    except Exception:
        await message.answer("❌ Пример: /int 4 (секунд между проходами сканера, минимум 2)")
        return
    settings["check_interval"] = val
    await message.answer(f"✅ Сканер проверяет спреды каждые <b>{val} сек</b>", parse_mode="HTML")
    schedule_save()


@dp.message(Command("dir"))
async def toggle_direction(message: types.Message):
    settings["scan_mexc_to_htx"] = not settings["scan_mexc_to_htx"]
    state = "HTX→MEXC и MEXC→HTX" if settings["scan_mexc_to_htx"] else "только HTX→MEXC"
    await message.answer(f"✅ Направления сигналов: <b>{state}</b>", parse_mode="HTML")
    schedule_save()


@dp.message(Command("fee"))
async def set_fees(message: types.Message, command: CommandObject):
    args = (command.args or "").replace(",", ".").split()
    try:
        settings["fee_htx_pct"] = abs(float(args[0]))
        settings["fee_mexc_pct"] = abs(float(args[1]))
    except Exception:
        await message.answer(
            f"❌ Пример: /fee 0.2 0.05 — комиссия taker на HTX и на MEXC в %\n"
            f"Сейчас: HTX {settings['fee_htx_pct']}%, MEXC {settings['fee_mexc_pct']}%")
        return
    await message.answer(f"✅ Комиссии для чистого спреда: HTX <b>{settings['fee_htx_pct']}%</b>, "
                         f"MEXC <b>{settings['fee_mexc_pct']}%</b>", parse_mode="HTML")
    schedule_save()


@dp.message(Command("ca"))
async def toggle_contract_check(message: types.Message):
    settings["check_contracts"] = not settings["check_contracts"]
    await message.answer(
        f"✅ Отсев монет с РАЗНЫМИ контрактами на HTX и MEXC: "
        f"<b>{'ВКЛЮЧЁН' if settings['check_contracts'] else 'ВЫКЛЮЧЕН'}</b>", parse_mode="HTML")
    schedule_save()


@dp.message(Command("tr"))
async def toggle_transfer_filter(message: types.Message):
    settings["chat_id"] = message.chat.id
    settings["require_transferable"] = not settings["require_transferable"]
    state = "ВКЛЮЧЕН" if settings["require_transferable"] else "ВЫКЛЮЧЕН"
    await message.answer(
        f"✅ Фильтр по доступности перевода: <b>{state}</b>\n"
        f"<i>Сигнал проходит, только если есть одна сеть, где открыт вывод на бирже покупки и "
        f"депозит на бирже продажи. Сторона MEXC проверяется при заданном MEXC_API_KEY, "
        f"статусы HTX — со слов HTX (бывают неточны).</i>",
        parse_mode="HTML")
    schedule_save()


@dp.message(Command("b"))
async def add_blacklist(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args:
        pair = normalize_pair(command.args)
        if pair in blacklist:
            blacklist.discard(pair)
            await message.answer(f"✅ <b>{pair}</b> убран из ЧС (сейчас в ЧС: {len(blacklist)})", parse_mode="HTML")
        else:
            blacklist.add(pair)
            await message.answer(f"🚫 <b>{pair}</b> в ЧС (сейчас в ЧС: {len(blacklist)})", parse_mode="HTML")
        schedule_save()
    else:
        await message.answer("❌ Ошибка. Пример: /b BTC (повторный вызов уберёт монету из ЧС). Список ЧС — /bl")


@dp.message(Command("bl"))
async def list_blacklist(message: types.Message):
    if not blacklist:
        await message.answer("🚫 Чёрный список пуст")
        return
    coins = "\n".join(f"• {p}" for p in sorted(blacklist))
    await message.answer(f"🚫 <b>Чёрный список ({len(blacklist)}):</b>\n{coins}", parse_mode="HTML")


@dp.message(Command("mute"))
async def mute_coin(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if not command.args:
        await message.answer("❌ Ошибка. Пример: /mute BTC 30 (замьютить BTCUSDT на 30 минут)")
        return
    parts = command.args.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("❌ Ошибка. Пример: /mute BTC 30 (монета + минуты)")
        return
    pair = normalize_pair(parts[0])
    minutes = int(parts[1])
    muted_until[pair] = time.time() + minutes * 60
    schedule_save()
    await message.answer(f"🔇 <b>{pair}</b> замьючена на <b>{minutes} мин</b> (алертов по ней не будет до истечения)", parse_mode="HTML")


@dp.message(Command("unmute"))
async def unmute_coin(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if not command.args:
        await message.answer("❌ Ошибка. Пример: /unmute BTC")
        return
    pair = normalize_pair(command.args)
    if pair in muted_until:
        del muted_until[pair]
        schedule_save()
        await message.answer(f"🔊 <b>{pair}</b> размьючена досрочно", parse_mode="HTML")
    else:
        await message.answer(f"ℹ️ <b>{pair}</b> и так не в муте", parse_mode="HTML")


@dp.message(Command("s"))
async def status_cmd(message: types.Message):
    stable_display = "Выкл" if settings["spread_stable_sec"] == 0 else f"{settings['spread_stable_sec']} сек"
    turnover_display = "Выкл" if settings["min_turnover_usd"] == 0 else f"{settings['min_turnover_usd']:,.0f}$"
    spm_display = "Выкл (общий /sp)" if settings["spread_percent_mexc_to_htx"] == 0 else f"{settings['spread_percent_mexc_to_htx']}%"
    spmax_display = "Выкл" if settings["max_spread_percent"] == 0 else f"{settings['max_spread_percent']}%"
    upstash_line = (
        "<b>Подключён</b>" if UPSTASH_REDIS_REST_URL
        else "<b>⚠️ НЕ НАСТРОЕН</b> — настройки слетят при перезапуске Render "
             "(задай их через переменные BOT_*, они переживают рестарт)"
    )
    await message.answer(
        "📊 <b>Статус</b>\n"
        f"🏷 Сборка: <code>{BUILD_TAG}</code> · коммит <code>{BUILD_COMMIT}</code>\n"
        f"🔀 Мин. % спреда (общий): <b>{settings['spread_percent']}%</b>\n"
        f"🔀 Мин. % спреда MEXC→HTX: <b>{spm_display}</b>\n"
        f"🔀 Верхняя отсечка спреда: <b>{spmax_display}</b>\n"
        f"💰 Мин. объём 24ч (обе биржи): <b>{settings['min_volume']:,}$</b>\n"
        f"📦 Мин. сумма для прокрутки: <b>{turnover_display}</b>\n"
        f"⏱ Пауза между повторными алертами: <b>{settings['cooldown_min']} мин</b>\n"
        f"⏳ Стабильность спреда перед алертом: <b>{stable_display}</b>\n"
        f"🚚 Фильтр перевода (только HTX-плечо): <b>{'Вкл' if settings['require_transferable'] else 'Выкл'}</b>\n"
        f"🚫 В чёрном списке: <b>{len(blacklist)} шт.</b>\n"
        f"🔇 В муте сейчас: <b>{len(muted_until)} шт.</b>\n"
        f"💾 Upstash: {upstash_line}\n"
        f"📢 Канал: {settings['channel_id'] or 'Не задан'}\n"
        f"🔁 Интервал проверки: {settings['check_interval']} сек\n"
        f"↔️ Направления: {'оба' if settings['scan_mexc_to_htx'] else 'только HTX→MEXC'}\n"
        f"🧮 Комиссии: HTX {settings['fee_htx_pct']}% · MEXC {settings['fee_mexc_pct']}%\n"
        f"🛑 В памяти алертов: {len(alert_memory)}\n"
        f"🔗 Общих пар на прошлом проходе: {debug_stats['common_pairs']}"
        , parse_mode="HTML")


@dp.message(Command("debug"))
async def debug_cmd(message: types.Message):
    ts = debug_stats["ts"]
    ago = int(time.time() - ts) if ts else None
    ago_str = f"{ago} сек назад" if ago is not None else "ещё не было прохода"

    mexc_status = "✅ ОК" if debug_stats["mexc_ok"] else "❌ Ошибка/пусто"
    huobi_status = "✅ ОК" if debug_stats["huobi_ok"] else "❌ Ошибка/пусто"
    htx_transfer_status = "✅ ОК" if debug_stats["htx_transfer_ok"] else "❌ Ошибка/пусто"
    if not MEXC_API_KEY or not MEXC_API_SECRET:
        contracts_status = "⚪ Выключено (нет MEXC_API_KEY/SECRET)"
    else:
        contracts_status = "✅ ОК" if debug_stats["mexc_contracts_ok"] else "❌ Ошибка"

    lines = [
        "🔍 <b>Воронка последнего прохода сканера</b>",
        f"🏷 Сборка: <code>{BUILD_TAG}</code> · коммит <code>{BUILD_COMMIT}</code>",
        f"⚙️ Сейчас активно: /mt={settings['min_turnover_usd']:g} /sp={settings['spread_percent']:g} "
        f"/v={settings['min_volume']:g} /tr={'вкл' if settings['require_transferable'] else 'выкл'}",
        f"⏱ Прошёл: {ago_str}",
        f"📡 MEXC (bid/ask + объём): {mexc_status}",
        f"📡 HTX (bid/ask + объём): {huobi_status}",
        f"📡 HTX (статус ввода/вывода): {htx_transfer_status}",
        f"📡 MEXC (контракты монет): {contracts_status}",
        f"1️⃣ Общих USDT-пар на обеих биржах: {debug_stats['common_pairs']} "
        f"(из них с разным тикером, сопоставлено по контракту/сети: {debug_stats.get('pairs_by_contract', 0)})",
        f"2️⃣ Прошли мин. объём 24ч на обеих биржах (/v): {debug_stats['passed_volume_floor']}",
        f"3️⃣ Грязный спред по лучшей цене ≥ /sp: {debug_stats['passed_spread_filter']} (отсечено сверху /spmax: {debug_stats['blocked_by_sanity']})",
        f"4️⃣ Прошли фильтр стабильности (/ss): {debug_stats['passed_stability']}",
        f"5️⃣ Прошли сверку контракта и общую сеть (/tr): {debug_stats['passed_transfer_check']} "
        f"(разные контракты: {debug_stats['blocked_by_contract']}, нет открытой общей сети: {debug_stats['blocked_by_transfer']}, "
        f"сети не сопоставились: {debug_stats['route_unmatched']})",
        f"6️⃣ Прошли анти-спам (кулдаун /cd): {debug_stats['passed_cooldown']}",
        f"7️⃣ Чистый спред ≥ /sp после всех комиссий: отсеяно {debug_stats['blocked_by_net']}",
        f"8️⃣ Прошли фильтр мин. оборота по стакану (/mt): {debug_stats['passed_turnover_filter']} "
        f"(без данных о стакане: {debug_stats['blocked_by_unknown_depth']}, "
        f"стакан не загрузился: {debug_stats['depth_fallback']})",
        f"📨 Алертов отправлено за этот проход: {debug_stats['alerts_sent']}",
    ]

    hd = debug_stats.get("htx_data") or {}
    if hd:
        ch = hd.get("chains") or 0
        pct = lambda n: f"{n} из {ch} ({n / ch * 100:.0f}%)" if ch else str(n)
        lines += [
            "",
            f"📑 <b>Что HTX отдаёт по сетям</b> ({hd.get('coins', 0)} монет, {ch} сетей):",
            f"   комиссия вывода: основной источник {pct(hd.get('v2_fee', 0))}, "
            f"второй источник добавил ещё {hd.get('v1_fee', 0)}",
            f"   контракт: {pct(hd.get('ca', 0))}",
            f"   расхождения источников: вывод «открыт» в основном, но закрыт во втором — "
            f"{hd.get('w_conflict', 0)}; то же по вводу — {hd.get('d_conflict', 0)} (считаются закрытыми)",
            f"   второй источник (/v1/settings/common/chains): "
            + ("✅ отвечает" if hd.get("v1_ok") else f"❌ {hd.get('v1_err', 'не отвечает')}"),
        ]
        if hd.get("v1_keys"):
            lines.append(f"   его поля: <code>{hd['v1_keys']}</code>")

    skipped_total = (debug_stats["skipped_mexc"] + debug_stats["skipped_htx"]
                     + debug_stats["skipped_htx_transfer"])
    if skipped_total:
        lines.append("")
        lines.append(
            f"⚠️ Записей выброшено при разборе ответов: MEXC {debug_stats['skipped_mexc']}, "
            f"HTX {debug_stats['skipped_htx']}, статусы перевода {debug_stats['skipped_htx_transfer']}"
        )
        if debug_stats["skip_reason"]:
            lines.append(f"   └ причина последней: {debug_stats['skip_reason']}")

    if debug_stats["last_error"]:
        lines.append(f"⚠️ Последняя ошибка: {debug_stats['last_error']}")

    lines.append("")
    lines.append(
        "💡 Если шаг 1 близок к нулю — вероятно, упал запрос к одной из бирж "
        "(смотри статус выше). Если шаг 2→3 сильно обнуляется — попробуй снизить "
        "/sp, спреды >1-2% на ликвидных парах случаются нечасто."
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


# ================= API =================

async def _mexc_get(url, label, timeout=TIMEOUT_BULK):
    """GET к публичному эндпоинту MEXC. Возвращает разобранный JSON или None."""
    async with http_session.get(url, timeout=timeout) as resp:
        if resp.status != 200:
            debug_stats["last_error"] = f"MEXC {label} HTTP {resp.status}"
            return None
        return await resp.json()


async def fetch_mexc_volumes():
    """
    Суточные объёмы MEXC (quoteVolume, т.е. в USDT) по всем USDT-парам.
    Кэшируется на MEXC_VOL_TTL: это самый тяжёлый ответ у MEXC (все пары разом),
    а объём за 24ч за несколько секунд не меняется ни на что значимое — тянуть
    его каждый проход бессмысленно. Если запрос упал, отдаём прошлый кэш:
    слегка устаревший объём лучше, чем обнуление фильтра ликвидности.
    """
    now = time.time()
    if mexc_vol_cache["data"] and (now - mexc_vol_cache["ts"]) < MEXC_VOL_TTL:
        return mexc_vol_cache["data"]

    try:
        vol_data = await _mexc_get("https://api.mexc.com/api/v3/ticker/24hr", "24hr")
    except Exception as e:
        debug_stats["last_error"] = f"MEXC 24hr запрос: {e}"
        return mexc_vol_cache["data"]

    if vol_data is None:
        return mexc_vol_cache["data"]

    vol_by_symbol = {}
    for item in vol_data:
        try:
            sym = item["symbol"]
            if sym.endswith("USDT"):
                vol_by_symbol[sym] = float(item["quoteVolume"])
        except Exception as e:
            debug_stats["skipped_mexc"] += 1
            debug_stats["skip_reason"] = f"MEXC 24hr: {type(e).__name__} {e}"
            continue

    if vol_by_symbol:
        mexc_vol_cache["ts"] = now
        mexc_vol_cache["data"] = vol_by_symbol

    return mexc_vol_cache["data"]


async def fetch_mexc_data():
    """
    Возвращает dict symbol -> {"bid", "ask", "vol" ($ за 24ч), "bid_qty", "ask_qty"}.
    Цены и объём топа стакана берутся из bookTicker (лёгкий bulk-ответ, тянется
    каждый проход), суточный объём — из кэша fetch_mexc_volumes (тяжёлый ответ,
    раз в MEXC_VOL_TTL). Оба запроса стартуют параллельно; когда объём ещё
    свежий в кэше, второй запрос вообще не уходит в сеть.
    """
    try:
        book_data, vol_by_symbol = await asyncio.gather(
            _mexc_get("https://api.mexc.com/api/v3/ticker/bookTicker", "bookTicker"),
            fetch_mexc_volumes(),
        )
    except Exception as e:
        debug_stats["last_error"] = f"MEXC запрос: {e}"
        return {}

    if book_data is None:
        return {}

    result = {}
    for item in book_data:
        try:
            sym = item["symbol"]
            if not sym.endswith("USDT"):
                continue
            bid = float(item["bidPrice"])
            ask = float(item["askPrice"])
            if bid <= 0 or ask <= 0:
                continue
            result[sym] = {
                "bid": bid, "ask": ask, "vol": vol_by_symbol.get(sym, 0.0),
                "bid_qty": float(item.get("bidQty", 0) or 0),
                "ask_qty": float(item.get("askQty", 0) or 0),
            }
        except Exception as e:
            debug_stats["skipped_mexc"] += 1
            debug_stats["skip_reason"] = f"MEXC bookTicker: {type(e).__name__} {e}"
            continue

    return result


def _first_num(v):
    """Huobi иногда отдаёт bid/ask как число, иногда как [цена, объём] — берём цену в обоих случаях."""
    if isinstance(v, (list, tuple)):
        return float(v[0]) if v else None
    return float(v)


def _opt_num(v):
    """float или None, если значения нет/оно не число (для необязательных полей)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


async def fetch_huobi_data():
    """
    Возвращает dict symbol (в формате MEXC, напр. "BTCUSDT") ->
    {"bid", "ask", "vol", "bid_qty", "ask_qty"}.

    Один bulk-запрос без параметров — все пары сразу. Поле "vol" — суточный
    оборот в quote-валюте (для *usdt пар — в USDT), совпадает по смыслу с
    quoteVolume MEXC (в базовой валюте у HTX идёт "amount", он нам не нужен).

    ВАЖНО про скорость: этот же ответ содержит bidSize/askSize — объём на лучшей
    цене, то есть ровно то, ради чего раньше по КАЖДОМУ кандидату улетал
    отдельный запрос /market/depth. Берём глубину прямо отсюда — целая сетевая
    фаза сканера (N запросов на проход) исчезает.
    """
    result = {}
    try:
        async with http_session.get("https://api.huobi.pro/market/tickers", headers=HTX_HEADERS, timeout=TIMEOUT_BULK) as resp:
            if resp.status != 200:
                debug_stats["last_error"] = f"HTX tickers HTTP {resp.status}"
                return {}
            payload = await resp.json()
    except Exception as e:
        debug_stats["last_error"] = f"HTX запрос: {e}"
        return {}

    for item in payload.get("data", []):
        try:
            raw_sym = item.get("symbol", "")
            if not raw_sym.endswith("usdt"):
                continue
            sym = raw_sym.upper()  # "btcusdt" -> "BTCUSDT", тот же формат, что у MEXC
            bid = _first_num(item.get("bid"))
            ask = _first_num(item.get("ask"))
            if not bid or not ask or bid <= 0 or ask <= 0:
                continue
            vol = float(item.get("vol", 0.0) or 0.0)  # оборот в USDT за сутки
            result[sym] = {
                "bid": bid, "ask": ask, "vol": vol,
                # None (а не 0), если поля нет — 0 означал бы «стакан пустой» и
                # ошибочно резал бы пару фильтром /mt.
                "bid_qty": _opt_num(item.get("bidSize")),
                "ask_qty": _opt_num(item.get("askSize")),
            }
        except Exception as e:
            debug_stats["skipped_htx"] += 1
            debug_stats["skip_reason"] = f"HTX tickers: {type(e).__name__} {e}"
            continue

    return result


def _mexc_signed_query(extra_params=None):
    """Строит подписанную query-строку для приватных (SIGNED) эндпоинтов MEXC.
    Возвращает (query_string, api_key) или (None, None), если ключи не заданы."""
    if not MEXC_API_KEY or not MEXC_API_SECRET:
        return None, None
    params = dict(extra_params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 10000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(MEXC_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}", MEXC_API_KEY


async def get_mexc_contracts():
    """
    Сети монет на MEXC через ПОДПИСЫВАЕМЫЙ /api/v3/capital/config/getall
    (нужны MEXC_API_KEY + MEXC_API_SECRET). По каждой сети: названия, контракт,
    комиссия вывода и — главное для фильтра переводимости — открыт ли депозит и
    вывод именно в этой сети. Кэш на MEXC_CONTRACTS_TTL.
    """
    now = time.time()
    if mexc_contracts_cache["data"] and (now - mexc_contracts_cache["ts"]) < MEXC_CONTRACTS_TTL:
        return mexc_contracts_cache["data"]

    query, api_key = _mexc_signed_query()
    if not api_key:
        return {}

    url = f"https://api.mexc.com/api/v3/capital/config/getall?{query}"
    try:
        async with http_session.get(url, headers={"X-MEXC-APIKEY": api_key}, timeout=TIMEOUT_HEAVY) as resp:
            if resp.status != 200:
                body = await resp.text()
                debug_stats["last_error"] = f"MEXC contracts HTTP {resp.status}: {body[:150]}"
                return mexc_contracts_cache["data"]
            data = await resp.json()
    except Exception as e:
        debug_stats["last_error"] = f"MEXC contracts запрос: {e}"
        return mexc_contracts_cache["data"]

    result = {}
    for item in (data if isinstance(data, list) else []):
        try:
            coin = str(item.get("coin", "")).upper()
            networks = []
            for net in item.get("networkList", []):
                fee_raw = net.get("withdrawFee")
                fee = None
                if fee_raw not in (None, ""):
                    try:
                        fee = float(fee_raw)
                    except (TypeError, ValueError):
                        fee = None
                networks.append({
                    "network": net.get("network") or net.get("netWork") or "?",
                    "names": [net.get("network"), net.get("netWork"), net.get("name")],
                    "contract": net.get("contract") or net.get("contractAddress"),
                    "withdraw_fee": fee,
                    "withdraw_enable": bool(net.get("withdrawEnable", False)),
                    "deposit_enable": bool(net.get("depositEnable", False)),
                })
            if networks:
                result[coin] = networks
        except Exception:
            continue

    if result:
        mexc_contracts_cache["ts"] = now
        mexc_contracts_cache["data"] = result
        debug_stats["mexc_contracts_ok"] = True
    else:
        debug_stats["mexc_contracts_ok"] = False

    return mexc_contracts_cache["data"]


def _extract_withdraw_fee(chain):
    """Возвращает (сумма_комиссии, тип) для одной сети HTX, или (None, None), если
    определить не удалось. fixed — фиксированная сумма; circulated/ratio — берём
    минимальную границу комиссии (minTransactFeeWithdraw)."""
    fee_type = chain.get("withdrawFeeType")
    try:
        if fee_type == "fixed":
            return float(chain.get("transactFeeWithdraw", 0) or 0), "фикс"
        if fee_type in ("circulated", "ratio"):
            return float(chain.get("minTransactFeeWithdraw", 0) or 0), "мин"
    except Exception:
        pass
    return None, None


def _v1_flag(v):
    """Флаг из второго источника HTX (de/we): True/False, или None, если поля нет."""
    if v is None or v == "":
        return None
    return str(v).strip().lower() not in ("false", "0", "no", "off")


def _v1_fee(row):
    """Комиссия вывода из второго источника HTX (/v1/settings/common/chains):
    ft — тип, fn — фиксированная сумма. None, если данных нет."""
    try:
        if str(row.get("ft", "")).lower() in ("fixed", "fix") and row.get("fn") not in (None, ""):
            return float(row["fn"]), "фикс"
        if row.get("fn") not in (None, "") and float(row["fn"]) > 0:
            return float(row["fn"]), "фикс"
    except (TypeError, ValueError):
        pass
    return None, None


async def _htx_json(url):
    async with http_session.get(url, headers=HTX_HEADERS, timeout=TIMEOUT_HEAVY) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:150]}")
        return await resp.json()


async def get_htx_transfer_status():
    """
    Статусы ввода/вывода, комиссии и контракты монет на HTX ПО КАЖДОЙ СЕТИ.
    Два публичных источника, склеиваются по (монета, сеть):
      1) /v2/reference/currencies — статусы ввода/вывода и комиссии;
      2) /v1/settings/common/chains — контракт (ca) и запасные комиссия/статусы.
    HTX известен тем, что отдаёт это неполно, поэтому в debug_stats считаем,
    по скольким сетям данные реально пришли (/debug), а в алерте честно пишем,
    чего нет. Кэш на HTX_TRANSFER_TTL.
    """
    now = time.time()
    if htx_transfer_cache["data"] and (now - htx_transfer_cache["ts"]) < HTX_TRANSFER_TTL:
        return htx_transfer_cache["data"]

    v2, v1 = await asyncio.gather(
        _htx_json("https://api.huobi.pro/v2/reference/currencies"),
        _htx_json("https://api.huobi.pro/v1/settings/common/chains"),
        return_exceptions=True,
    )
    if isinstance(v2, Exception):
        debug_stats["last_error"] = f"HTX currencies: {v2}"
        return htx_transfer_cache["data"]

    v1_rows = {}
    stats = {"coins": 0, "chains": 0, "v2_fee": 0, "v1_fee": 0, "ca": 0, "w_conflict": 0, "d_conflict": 0,
             "v1_ok": not isinstance(v1, Exception), "v1_keys": ""}
    if not isinstance(v1, Exception):
        rows = v1.get("data") or []
        for row in rows:
            v1_rows[(str(row.get("currency", "")).lower(), row.get("chain"))] = row
        if rows:
            stats["v1_keys"] = ", ".join(sorted(rows[0].keys()))[:300]
    else:
        stats["v1_err"] = str(v1)[:150]

    result = {}
    for item in v2.get("data", []):
        try:
            coin = item.get("currency", "").upper()
            chains = []
            for ch in item.get("chains", []):
                row = v1_rows.get((coin.lower(), ch.get("chain")), {})
                fee, ftype = _extract_withdraw_fee(ch)
                if fee is not None:
                    stats["v2_fee"] += 1
                else:
                    fee, ftype = _v1_fee(row)
                    if fee is not None:
                        stats["v1_fee"] += 1
                ca = row.get("ca") or row.get("contractAddress") or ch.get("contractAddress")
                if ca:
                    stats["ca"] += 1
                w_ok = ch.get("withdrawStatus") == "allowed"
                d_ok = ch.get("depositStatus") == "allowed"
                if w_ok and _v1_flag(row.get("we")) is False:
                    w_ok = False
                    stats["w_conflict"] += 1
                if d_ok and _v1_flag(row.get("de")) is False:
                    d_ok = False
                    stats["d_conflict"] += 1
                chains.append({
                    "chain": ch.get("chain"),
                    "names": [ch.get("displayName"), ch.get("baseChain"),
                              ch.get("baseChainProtocol"), ch.get("chain")],
                    # Открыто, только если ОБА источника HTX не говорят «закрыто»:
                    # второй источник (we/de) иногда знает о приостановке раньше.
                    "withdraw": w_ok,
                    "deposit": d_ok,
                    "wdesc": (row.get("withdraw-desc") or "").strip(),
                    "fee": fee, "fee_type": ftype, "ca": ca,
                })
            stats["coins"] += 1
            stats["chains"] += len(chains)

            open_w = [c for c in chains if c["withdraw"] and c["fee"] is not None]
            best = min(open_w, key=lambda c: c["fee"]) if open_w else None
            result[coin] = {
                "deposit": any(c["deposit"] for c in chains),
                "withdraw": any(c["withdraw"] for c in chains),
                "fee": best["fee"] if best else None,
                "fee_type": best["fee_type"] if best else None,
                "fee_chain": (best["names"][0] or best["chain"]) if best else None,
                "chains": chains,
            }
        except Exception as e:
            debug_stats["skipped_htx_transfer"] += 1
            debug_stats["skip_reason"] = f"HTX currencies: {type(e).__name__} {e}"
            continue

    if result:
        htx_transfer_cache["ts"] = now
        htx_transfer_cache["data"] = result
        debug_stats["htx_transfer_ok"] = True
        debug_stats["htx_data"] = stats
    else:
        debug_stats["htx_transfer_ok"] = False

    return htx_transfer_cache["data"]


async def get_htx_book(symbol_lower):
    """Стакан HTX целиком (до 150 уровней): (bids, asks) или None при ошибке."""
    url = f"https://api.huobi.pro/market/depth?symbol={symbol_lower}&type=step0"
    try:
        async with http_session.get(url, headers=HTX_HEADERS, timeout=TIMEOUT_DEPTH) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        tick = data.get("tick") or {}
        bids = [(float(p), float(q)) for p, q in tick.get("bids") or []]
        asks = [(float(p), float(q)) for p, q in tick.get("asks") or []]
        return bids, asks
    except Exception as e:
        debug_stats["last_error"] = f"HTX depth {symbol_lower}: {type(e).__name__} {e}"
        return None


async def get_mexc_book(symbol):
    """Стакан MEXC (до 100 уровней): (bids, asks) или None при ошибке."""
    url = f"https://api.mexc.com/api/v3/depth?symbol={symbol}&limit=100"
    try:
        async with http_session.get(url, timeout=TIMEOUT_DEPTH) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        bids = [(float(p), float(q)) for p, q in data.get("bids") or []]
        asks = [(float(p), float(q)) for p, q in data.get("asks") or []]
        return bids, asks
    except Exception as e:
        debug_stats["last_error"] = f"MEXC depth {symbol}: {type(e).__name__} {e}"
        return None


# ================= UPSTASH (персистентность настроек) =================

async def redis_cmd(*args):
    """Одна команда к Upstash Redis через REST API (POST с JSON-массивом
    команды). Возвращает result или None при ошибке/если Upstash не настроен."""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        async with http_session.post(
            UPSTASH_REDIS_REST_URL,
            json=list(args),
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=TIMEOUT_REDIS,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("result")
    except Exception as e:
        print(f"Upstash ошибка: {e}", flush=True)
        return None


# Event loop держит на задачи только СЛАБЫЕ ссылки: если не сохранить ссылку на
# результат create_task, сборщик мусора может убить задачу прямо посреди
# выполнения, и сохранение в Upstash молча потеряется. Держим ссылки здесь и
# отпускаем по завершении.
_background_tasks = set()


def schedule_save():
    """Fire-and-forget сохранение состояния, но со ссылкой на задачу."""
    task = asyncio.create_task(save_state())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def save_state():
    """Сохраняет settings/blacklist/muted_until в Upstash. Вызывается сразу
    после любого изменения (fire-and-forget из команд), плюс раз в проход
    сканера как подстраховка."""
    if not UPSTASH_REDIS_REST_URL:
        return
    await asyncio.gather(
        redis_cmd("SET", "spread_bot:settings", json.dumps(settings)),
        redis_cmd("SET", "spread_bot:blacklist", json.dumps(list(blacklist))),
        redis_cmd("SET", "spread_bot:muted", json.dumps(muted_until)),
        return_exceptions=True,
    )


async def load_state():
    """Восстанавливает состояние из Upstash при старте бота. Если ключей нет
    (первый запуск) или Upstash не настроен — просто ничего не делает, бот
    стартует с дефолтами, как раньше."""
    if not UPSTASH_REDIS_REST_URL:
        return
    raw_settings, raw_blacklist, raw_muted = await asyncio.gather(
        redis_cmd("GET", "spread_bot:settings"),
        redis_cmd("GET", "spread_bot:blacklist"),
        redis_cmd("GET", "spread_bot:muted"),
        return_exceptions=True,
    )
    try:
        if isinstance(raw_settings, str):
            loaded = json.loads(raw_settings)
            settings.update(loaded)  # merge — новые ключи из кода не потеряются
    except Exception as e:
        print(f"Upstash: не удалось разобрать settings: {e}", flush=True)
    try:
        if isinstance(raw_blacklist, str):
            blacklist.clear()
            blacklist.update(json.loads(raw_blacklist))
    except Exception as e:
        print(f"Upstash: не удалось разобрать blacklist: {e}", flush=True)
    try:
        if isinstance(raw_muted, str):
            muted_until.clear()
            muted_until.update(json.loads(raw_muted))
    except Exception as e:
        print(f"Upstash: не удалось разобрать muted: {e}", flush=True)
    print("--- Состояние восстановлено из Upstash ---", flush=True)


# ================= ОСНОВНОЙ ЦИКЛ =================

def _withdraw_fee_usd(c):
    """Комиссия вывода в $ для кандидата: по выбранной общей сети, а если сети
    сопоставить не удалось — по самой дешёвой открытой сети HTX. None = неизвестна."""
    route, price = c["route"], c["buy_price"]
    best = route.get("best")
    if route["state"] == "open" and best and best.get("fee") is not None:
        return best["fee"] * price
    hs = c.get("htx_coin_status")
    if c["buy_ex"] == "HTX" and route["state"] in ("nodata", "nomatch") and hs and hs.get("fee") is not None:
        return hs["fee"] * price
    return None


def _nets_overlap(htx_status, mexc_nets):
    """Есть ли у монеты на HTX хоть одна сеть, совпадающая по названию с сетями MEXC."""
    for h in (htx_status or {}).get("chains", []):
        h_ids = sq.net_ids(*h.get("names", []))
        if any(h_ids & sq.net_ids(*m.get("names", [])) for m in mexc_nets):
            return True
    return False


def build_pair_map(mexc_data, huobi_data, htx_transfer, mexc_contracts):
    """Какую пару MEXC с какой парой HTX сравнивать: {"MONUSDT": "MONADUSDT", ...}.

    База — одинаковые тикеры. Поверх — сопоставление по АДРЕСУ КОНТРАКТА: если
    на HTX монета с тем же контрактом торгуется под другим тикером, сравниваем с
    ней (и находим монеты, которые по тикеру не сопоставились бы вовсе, и
    заменяем ложную пару «тот же тикер — другая монета»). Нативные монеты сетей
    (контракта нет) — по названию сети (MON на MEXC ↔ MONAD на HTX)."""
    pair_map = {p: p for p in set(mexc_data) & set(huobi_data)}

    ca_to_htx = {}
    for cur, st in htx_transfer.items():
        for ch in st.get("chains", []):
            ca = sq.norm_contract(ch.get("ca"))
            if ca:
                ca_to_htx.setdefault(ca, set()).add(cur)

    by_contract = 0
    for coin, nets in mexc_contracts.items():
        mp = f"{coin}USDT"
        if mp not in mexc_data:
            continue
        hits = set()
        for n in nets:
            ca = sq.norm_contract(n.get("contract"))
            if ca:
                hits |= ca_to_htx.get(ca, set())
        hits = {h for h in hits if f"{h}USDT" in huobi_data}
        if len(hits) == 1:
            hc = hits.pop()
            if hc != coin:
                pair_map[mp] = f"{hc}USDT"
                by_contract += 1
        elif not hits and (mp not in pair_map or not _nets_overlap(htx_transfer.get(coin), nets)):
            # Нативная монета сети: контракта нет — ищем на HTX монету, у которой
            # есть сеть с тем же названием, что у сети MEXC, и которая сама
            # называется как эта сеть (Monad: MEXC MON / сеть MONAD → HTX MONAD).
            for n in nets:
                if sq.norm_contract(n.get("contract")):
                    continue
                for name in n.get("names", []):
                    cand = re.sub(r"[^A-Z0-9]", "", str(name or "").upper())
                    if cand and cand != coin and f"{cand}USDT" in huobi_data and cand in htx_transfer:
                        pair_map[mp] = f"{cand}USDT"
                        by_contract += 1
                        break
                if mp in pair_map:
                    break
    debug_stats["pairs_by_contract"] = by_contract
    return pair_map


async def scanner_task():
    while True:
        try:
            mexc_data, huobi_data, htx_transfer, mexc_contracts = await asyncio.gather(
                fetch_mexc_data(), fetch_huobi_data(),
                get_htx_transfer_status(), get_mexc_contracts(),
            )
            debug_stats["ts"] = time.time()
            debug_stats["mexc_ok"] = bool(mexc_data)
            debug_stats["huobi_ok"] = bool(huobi_data)
            for k in ("passed_volume_floor", "passed_spread_filter", "passed_stability",
                      "passed_turnover_filter", "passed_transfer_check", "blocked_by_transfer",
                      "passed_cooldown", "alerts_sent", "skipped_mexc", "skipped_htx",
                      "skipped_htx_transfer", "blocked_by_sanity", "blocked_by_unknown_depth",
                      "depth_fallback", "blocked_by_contract", "route_unmatched", "blocked_by_net"):
                debug_stats[k] = 0

            if not mexc_data or not huobi_data:
                await asyncio.sleep(settings["check_interval"])
                continue

            pair_map = build_pair_map(mexc_data, huobi_data, htx_transfer, mexc_contracts)
            common = set(pair_map) - blacklist
            debug_stats["common_pairs"] = len(common)

            now = time.time()

            # Чистим память алертов старше суток — кулдаун всё равно считается
            # минутами, а без этого словарь только рос всё время работы процесса.
            stale = [p for p, a in alert_memory.items() if (now - a["last_msg"]) >= 86400]
            for p in stale:
                del alert_memory[p]

            # ===== ФАЗА 1: синхронная фильтрация, без единого сетевого вызова =====
            candidates = []
            for pair in common:
                # Мут (/mute) — проверяем раньше всего, дешёвая проверка по словарю.
                mute_expiry = muted_until.get(pair)
                if mute_expiry is not None:
                    if now < mute_expiry:
                        continue  # ещё в муте
                    del muted_until[pair]  # мут истёк — снимаем и чистим память

                m = mexc_data[pair]
                hpair = pair_map[pair]  # пара на HTX (тикер может отличаться)
                h = huobi_data[hpair]

                if m["vol"] < settings["min_volume"] or h["vol"] < settings["min_volume"]:
                    continue
                debug_stats["passed_volume_floor"] += 1

                # Направление 1: купить на MEXC по ask, продать на HTX по bid
                spread_mexc_to_htx = (h["bid"] - m["ask"]) / m["ask"] * 100
                # Направление 2: купить на HTX по ask, продать на MEXC по bid
                spread_htx_to_mexc = (m["bid"] - h["ask"]) / h["ask"] * 100

                mexc_to_htx_threshold = (
                    settings["spread_percent_mexc_to_htx"]
                    if settings["spread_percent_mexc_to_htx"] > 0
                    else settings["spread_percent"]
                )
                htx_to_mexc_threshold = settings["spread_percent"]

                dir_candidates = []
                if settings["scan_mexc_to_htx"] and spread_mexc_to_htx >= mexc_to_htx_threshold:
                    dir_candidates.append(("MEXC", "HTX", spread_mexc_to_htx, m["ask"], h["bid"], mexc_to_htx_threshold))
                if spread_htx_to_mexc >= htx_to_mexc_threshold:
                    dir_candidates.append(("HTX", "MEXC", spread_htx_to_mexc, h["ask"], m["bid"], htx_to_mexc_threshold))

                # ===== САНИТИ-ОТСЕЧКА СВЕРХУ =====
                # Отбрасываем направления с неправдоподобно большим спредом ДО
                # выбора лучшего — иначе "спред" в 900%, возникший из-за разной
                # деноминации одноимённых тикеров, всегда побеждал бы в max().
                if settings["max_spread_percent"] > 0:
                    sane = [c for c in dir_candidates if c[2] <= settings["max_spread_percent"]]
                    if len(sane) != len(dir_candidates):
                        debug_stats["blocked_by_sanity"] += 1
                    dir_candidates = sane

                if not dir_candidates:
                    spread_track.pop(pair, None)  # ни одно направление не прошло свой порог — сбрасываем отсчёт
                    continue
                debug_stats["passed_spread_filter"] += 1

                # Если прошли оба направления — берём то, где спред больше.
                buy_ex, sell_ex, best_spread, buy_price, sell_price, threshold = max(dir_candidates, key=lambda c: c[2])

                # ============ ФИЛЬТР СТАБИЛЬНОСТИ СПРЕДА (/ss) ============
                if settings["spread_stable_sec"] > 0:
                    first_seen = spread_track.get(pair)
                    if first_seen is None:
                        spread_track[pair] = now
                        continue  # первый раз видим спред выше порога — ждём подтверждения
                    if (now - first_seen) < settings["spread_stable_sec"]:
                        continue  # ещё не набрали нужную длительность
                debug_stats["passed_stability"] += 1

                # Именно срез, а не replace("USDT", ""): replace вырезает ВСЕ вхождения.
                base_coin = pair[:-4]
                htx_base = hpair[:-4]
                htx_coin_status = htx_transfer.get(htx_base)

                # ===== ОБЩАЯ СЕТЬ + СВЕРКА КОНТРАКТА =====
                # Перевести монету можно только по ОДНОЙ сети, где на бирже покупки
                # открыт вывод, а на бирже продажи — депозит. И одинаковый тикер ещё
                # не значит одну монету — сверяем контракты, где биржи их отдают.
                route = sq.find_route(
                    buy_ex,
                    (htx_coin_status or {}).get("chains", []),
                    mexc_contracts.get(base_coin, []),
                )
                if settings["check_contracts"] and route["contract"] == "mismatch":
                    debug_stats["blocked_by_contract"] += 1
                    continue

                if route["state"] == "nodata":
                    # Нет данных по сетям одной из бирж (например, нет ключа MEXC) —
                    # как раньше, смотрим только агрегированный статус HTX.
                    htx_leg = "withdraw" if buy_ex == "HTX" else "deposit"
                    htx_ok = htx_coin_status.get(htx_leg, False) if htx_coin_status else True
                    transfer_blocked = htx_coin_status is not None and not htx_ok
                elif route["state"] == "nomatch":
                    debug_stats["route_unmatched"] += 1
                    transfer_blocked = False  # не смогли сопоставить названия — не режем, но пометим
                else:
                    transfer_blocked = route["state"] == "closed"

                if settings["require_transferable"] and transfer_blocked:
                    debug_stats["blocked_by_transfer"] += 1
                    continue
                debug_stats["passed_transfer_check"] += 1

                # Анти-спам: простой кулдаун по времени.
                prev = alert_memory.get(pair)
                if prev and (now - prev["last_msg"]) < settings["cooldown_min"] * 60:
                    continue
                debug_stats["passed_cooldown"] += 1

                candidates.append({
                    "pair": pair, "m": m, "h": h,
                    "buy_ex": buy_ex, "sell_ex": sell_ex, "best_spread": best_spread,
                    "buy_price": buy_price, "sell_price": sell_price, "threshold": threshold,
                    "base_coin": base_coin, "htx_coin_status": htx_coin_status,
                    "hpair": hpair, "htx_base": htx_base,
                    "route": route, "prev": prev,
                })

            if not candidates:
                await asyncio.sleep(settings["check_interval"])
                continue

            # ===== ФАЗА 2: стаканы обеих бирж по кандидатам =====
            # Лучшая цена часто держит монет на $20 — по ней спред красивый, а на
            # реальную сумму его нет. Поэтому по каждому кандидату тянем стаканы и
            # считаем, сколько $ можно прокрутить, пока спред на очередном уровне
            # не ниже порога, и какой средний спред на этот объём. Кандидатов после
            # фильтров единицы, так что запросов немного; ограничиваем сверху.
            candidates.sort(key=lambda c: c["best_spread"], reverse=True)
            candidates = candidates[:MAX_DEPTH_CANDIDATES]
            books = await asyncio.gather(
                *[asyncio.gather(get_htx_book(c["hpair"].lower()), get_mexc_book(c["pair"]))
                  for c in candidates],
                return_exceptions=True,
            )
            # Порог /sp — это ЧИСТЫЙ спред: после торговых комиссий обеих бирж и
            # комиссии вывода. Сигнал проходит, только если есть объём, на котором
            # чистый спред не ниже порога; этот объём и чистый спред и показываем.
            trade_fees = settings["fee_htx_pct"] + settings["fee_mexc_pct"]
            passed = []
            for c, res in zip(candidates, books):
                htx_book, mexc_book = (None, None) if isinstance(res, Exception) else res
                c["tradable_usd"] = c["avg_spread"] = c["net"] = None
                c["depth_capped"] = False
                c["withdraw_fee_usd"] = _withdraw_fee_usd(c)
                fee_usd = c["withdraw_fee_usd"] or 0.0
                if htx_book and mexc_book:
                    if c["buy_ex"] == "HTX":
                        asks, bids = htx_book[1], mexc_book[0]
                    else:
                        asks, bids = mexc_book[1], htx_book[0]
                    r = sq.arb_volume_net(asks, bids, trade_fees, fee_usd, c["threshold"])
                    if r["cost"] is None:
                        debug_stats["blocked_by_net"] += 1
                        continue
                    c["tradable_usd"], c["avg_spread"], c["net"] = r["cost"], r["gross"], r["net"]
                    # «Стакан глубже загруженного» — только если упёрлись в лимит
                    # загрузки (100+ уровней), а не в настоящий конец тонкого стакана.
                    c["depth_capped"] = r["capped"] and max(len(asks), len(bids)) >= 100
                else:
                    debug_stats["depth_fallback"] += 1
                    # Запасной вариант — объём только на лучшей цене из bulk-ответов.
                    if c["buy_ex"] == "MEXC":
                        bq, sq_ = c["m"].get("ask_qty"), c["h"].get("bid_qty")
                    else:
                        bq, sq_ = c["h"].get("ask_qty"), c["m"].get("bid_qty")
                    if bq is not None and sq_ is not None and bq * sq_ > 0:
                        c["tradable_usd"] = min(bq, sq_) * c["buy_price"]
                        c["net"] = c["best_spread"] - trade_fees - fee_usd / c["tradable_usd"] * 100
                    if c["net"] is None or c["net"] < c["threshold"]:
                        debug_stats["blocked_by_net"] += 1
                        continue
                passed.append(c)
            candidates = passed

            # ===== ФАЗА 3: фильтр мин. оборота + сборка сообщений =====
            messages = []  # [(chat_id_или_channel, текст), ...] — отправим все разом в конце

            for c in candidates:
                pair, m, h = c["pair"], c["m"], c["h"]
                buy_ex, sell_ex = c["buy_ex"], c["sell_ex"]
                best_spread, buy_price, sell_price = c["best_spread"], c["buy_price"], c["sell_price"]
                base_coin, route, prev = c["base_coin"], c["route"], c["prev"]
                tradable_usd, avg_spread = c["tradable_usd"], c["avg_spread"]

                # ============ ФИЛЬТР МИН. ОБОРОТА (/mt) ============
                # Если фильтр включён, а объём посчитать не удалось — пара НЕ проходит.
                if settings["min_turnover_usd"] > 0:
                    if tradable_usd is None:
                        debug_stats["blocked_by_unknown_depth"] += 1
                        continue
                    if tradable_usd < settings["min_turnover_usd"]:
                        continue
                debug_stats["passed_turnover_filter"] += 1

                if avg_spread is None:
                    depth_line = (f"📦 Прокрутить с чистым спредом ≥ {c['threshold']:g}%: ~{fmt_money(tradable_usd)}$ "
                                  f"(только лучшая цена — стакан не загрузился)")
                else:
                    depth_line = (f"📦 Прокрутить с чистым спредом ≥ {c['threshold']:g}%: <b>~{fmt_money(tradable_usd)}$</b>"
                                  f"{'+ (стакан глубже загруженного)' if c['depth_capped'] else ''}, "
                                  f"грязный спред на этот объём {avg_spread:+.2f}%")

                alert_memory[pair] = {
                    "time": prev["time"] if prev else now,
                    "last_msg": now,
                    "spread": best_spread,
                }
                debug_stats["alerts_sent"] += 1

                # ----- Перевод: какая сеть и что с ней -----
                withdraw_fee_usd = c["withdraw_fee_usd"]
                from_ex, to_ex = buy_ex, sell_ex
                transfer_lines = []
                best = route.get("best")
                if route["state"] == "open" and best:
                    net_name = best["htx"]["names"][0] or best["htx"]["chain"]
                    transfer_lines.append(
                        f"🚚 Общая сеть: <b>{net_name}</b> — вывод {from_ex} ✅ · депозит {to_ex} ✅")
                    if best["htx"].get("wdesc") and buy_ex == "HTX":
                        transfer_lines.append(f"ℹ️ HTX о выводе: {best['htx']['wdesc'][:200].replace('<', '')}")
                    if best["fee"] is not None:
                        transfer_lines.append(
                            f"💸 Комиссия вывода с {from_ex}: {best['fee']:g} {base_coin} (~{fmt_money(withdraw_fee_usd)}$)")
                    else:
                        transfer_lines.append(f"💸 Комиссию вывода {from_ex} не отдал")
                elif route["state"] == "closed":
                    transfer_lines.append(f"🚚 ❌ Нет общей сети, где открыт вывод {from_ex} и депозит {to_ex}")
                elif route["state"] == "nomatch":
                    transfer_lines.append("🚚 ⚠️ Сети бирж не удалось сопоставить по названиям — проверь вручную")
                else:
                    hs = c["htx_coin_status"]
                    leg = "вывод" if buy_ex == "HTX" else "ввод"
                    if hs is None:
                        transfer_lines.append(f"🚚 HTX ({leg}): ❔ статус неизвестен · MEXC: нет данных")
                    else:
                        ok = hs.get("withdraw" if buy_ex == "HTX" else "deposit")
                        transfer_lines.append(f"🚚 HTX ({leg}): {'✅ открыт' if ok else '❌ закрыт'} в какой-то сети · MEXC: нет данных (нужен ключ)")
                    if buy_ex == "HTX" and hs and hs.get("fee") is not None:
                        transfer_lines.append(f"💸 Комиссия вывода с HTX ({hs.get('fee_chain') or '?'}): {hs['fee']:g} {base_coin} (~{fmt_money(withdraw_fee_usd)}$)")

                if route["contract"] == "ok":
                    contract_line = "🔗 Контракт: ✅ совпадает на обеих биржах"
                elif route["contract"] == "mismatch":
                    contract_line = "🔗 Контракт: ⛔ РАЗНЫЙ на биржах — скорее всего разные монеты (/ca выключает проверку)"
                else:
                    mexc_cas = [n["contract"] for n in mexc_contracts.get(base_coin, []) if n.get("contract")]
                    contract_line = ("🔗 Контракт: ⚠️ не сверен (HTX не отдал) — MEXC: "
                                     + (", ".join(mexc_cas[:2]) if mexc_cas else "нет данных"))

                # ----- Чистый спред (уже посчитан в фазе 2 по стакану) -----
                net = c["net"]
                if withdraw_fee_usd is not None and tradable_usd:
                    net_note = f"торговые {trade_fees:g}% и вывод ~{fmt_money(withdraw_fee_usd)}$ на {fmt_money(tradable_usd)}$"
                elif withdraw_fee_usd is not None:
                    net_note = f"торговые {trade_fees:g}%; вывод ~{fmt_money(withdraw_fee_usd)}$ не учтён — неизвестен объём"
                else:
                    net_note = f"торговые {trade_fees:g}%; комиссия вывода неизвестна — не учтена"

                lines = [
                    f"🔀 <b>СПРЕД: <code>{base_coin}</code></b>"
                    + (f" (на HTX: <code>{c['htx_base']}</code>, сопоставлено по контракту/сети)"
                       if c["htx_base"] != base_coin else ""),
                    "",
                    f"💹 <b>{best_spread:+.2f}%</b> по лучшей цене · Купить на <b>{buy_ex}</b> ({fmt_price(buy_price)}) "
                    f"→ Продать на <b>{sell_ex}</b> ({fmt_price(sell_price)})",
                    depth_line,
                    f"🧮 Чистый спред ≈ <b>{net:+.2f}%</b> (минус {net_note})",
                    "",
                    f"📥 MEXC: bid {fmt_price(m['bid'])} / ask {fmt_price(m['ask'])}",
                    f"📤 HTX: bid {fmt_price(h['bid'])} / ask {fmt_price(h['ask'])}",
                    f"💰 Объём 24ч: MEXC {fmt_money(m['vol'])}$ · HTX {fmt_money(h['vol'])}$",
                    "",
                    *transfer_lines,
                    contract_line,
                    "<i>Статусы перевода — со слов бирж (HTX часто неточен), сверяй перед крупным переводом.</i>",
                ]
                alert_text = "\n".join(lines)

                if settings["chat_id"]:
                    messages.append((settings["chat_id"], alert_text))
                if settings["channel_id"]:
                    messages.append((settings["channel_id"], alert_text))

            # ===== Отправка всех сообщений этого прохода ПАРАЛЛЕЛЬНО =====
            async def _send(chat, text):
                try:
                    await bot.send_message(chat, text, parse_mode="HTML")
                except Exception as e:
                    print(f"Ошибка отправки в {chat}: {e}", flush=True)

            if messages:
                await asyncio.gather(*[_send(chat, text) for chat, text in messages])

        except Exception as e:
            print(f"Ошибка сканера: {e}", flush=True)
            debug_stats["last_error"] = str(e)

        await asyncio.sleep(settings["check_interval"])


# ================= WEB & RUN =================

BOT_COMMANDS = [
    BotCommand(command="start", description="Инфо и список команд"),
    BotCommand(command="s", description="Текущий статус настроек"),
    BotCommand(command="debug", description="Диагностика последнего прохода сканера"),
    BotCommand(command="sp", description="Мин. ЧИСТЫЙ % спреда для алерта"),
    BotCommand(command="spm", description="Порог спреда именно MEXC→HTX"),
    BotCommand(command="spmax", description="Верхняя отсечка спреда (анти-мусор)"),
    BotCommand(command="v", description="Мин. объём 24ч на обеих биржах"),
    BotCommand(command="mt", description="Мин. сумма для прокрутки по стакану"),
    BotCommand(command="cd", description="Пауза между повторными алертами"),
    BotCommand(command="ss", description="Мин. время стабильности спреда"),
    BotCommand(command="tr", description="Вкл/выкл фильтр общей открытой сети"),
    BotCommand(command="ca", description="Вкл/выкл отсев разных контрактов"),
    BotCommand(command="int", description="Как часто проверять спреды, сек"),
    BotCommand(command="dir", description="Вкл/выкл сигналы MEXC→HTX"),
    BotCommand(command="fee", description="Торговые комиссии для чистого спреда"),
    BotCommand(command="b", description="Добавить/убрать монету из ЧС"),
    BotCommand(command="bl", description="Показать список монет в ЧС"),
    BotCommand(command="mute", description="Временно замьютить монету"),
    BotCommand(command="unmute", description="Снять мут с монеты"),
    BotCommand(command="channel", description="Куда дублировать сигналы"),
] + [BotCommand(command=c, description=d) for c, d in arbitrage.BOT_COMMANDS]


async def handle_ping(request):
    return web.Response(text="OK", status=200)


async def main():
    global http_session

    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()

    # Общая сессия на всё время жизни процесса: пул соединений (keep-alive) +
    # DNS-кэш — реальная экономия задержки на каждом запросе к тем же хостам.
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    http_session = aiohttp.ClientSession(connector=connector)

    scanner = None
    arb_tasks = []

    def _set_chat(chat_id):
        settings["chat_id"] = chat_id
        schedule_save()

    arbitrage.setup(
        bot,
        session_getter=lambda: http_session,
        redis_cmd=redis_cmd if UPSTASH_REDIS_REST_URL else None,
        chat_get=lambda: settings["chat_id"],
        chat_set=_set_chat,
    )
    try:
        # Порядок важен: дефолты в коде → переменные окружения (переживают
        # перезапуск Render) → Upstash (живое состояние, выставленное командами).
        apply_env_overrides()
        await load_state()  # восстанавливаем settings/blacklist/muted из Upstash, если настроен

        print(f"--- Старт: сборка {BUILD_TAG} ({BUILD_COMMIT}), "
              f"mt={settings['min_turnover_usd']}, sp={settings['spread_percent']}, "
              f"upstash={'да' if UPSTASH_REDIS_REST_URL else 'НЕТ'} ---", flush=True)

        # Регистрируем список команд в Telegram — по нажатию "/" в чате сразу
        # всплывает меню с подсказками, без этого вызова Telegram о командах не знает.
        await bot.set_my_commands(BOT_COMMANDS)

        await bot.delete_webhook(drop_pending_updates=True)
        scanner = asyncio.create_task(scanner_task())
        arb_tasks = await arbitrage.start()
        await dp.start_polling(bot)
    finally:
        # Render перезапускает процесс регулярно — закрываемся аккуратно, чтобы
        # не оставлять недописанное состояние и открытые соединения.
        if scanner is not None:
            scanner.cancel()
        for t in arb_tasks:
            t.cancel()
        await save_state()
        await arbitrage.save()
        await http_session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
