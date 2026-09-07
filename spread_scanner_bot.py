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
BUILD_TAG = "2026-09-07 executable-depth"
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

    "check_interval": 10,    # Как часто проверять (сек) — bulk-эндпоинты дешёвые,
                             # можно опрашивать чаще без риска упереться в рейт-лимит
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
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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
        f"/sp 1.5 — мин. % спреда, чтобы сработал алерт (ПЕРВИЧНЫЙ критерий, общий для обоих направлений)\n"
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
        f"/tr — вкл/выкл фильтр по доступности вывода/ввода (см. ⚠️ ниже про ограничение)\n"
        f"   └ сейчас: <b>{'Вкл' if settings['require_transferable'] else 'Выкл'}</b>\n"
        f"/b BTC — добавить/убрать монету из чёрного списка (повторный вызов с той же монетой снимает её)\n"
        f"   └ в ЧС сейчас: <b>{len(blacklist)} шт.</b>\n"
        f"/bl — показать список монет в ЧС (проверить, что реально добавилось)\n"
        f"/mute BTC 30 — замьютить монету ВРЕМЕННО на N минут (снимается само)\n"
        f"   └ в муте сейчас: <b>{len(muted_until)} шт.</b>\n"
        f"/unmute BTC — снять мут досрочно\n"
        f"/channel @имя_канала — куда дублировать сигналы (пусто = выкл)\n"
        f"   └ сейчас: <b>{settings['channel_id'] or 'Не задан'}</b>\n"
        f"/s — текущий статус настроек\n"
        f"/debug — воронка последнего прохода сканера (диагностика, если алертов нет)\n\n"

        f"💾 <b>Upstash</b>: {'подключён — настройки/ЧС/муты переживут перезапуск' if UPSTASH_REDIS_REST_URL else 'не настроен — состояние в памяти, слетит при перезапуске'}\n\n"

        "⚠️ <b>Важно понимать</b>\n"
        "Это спред между ценами В МОМЕНТ ЗАПРОСА, без учёта комиссий за сделки "
        "(обычно ~0.1-0.2% на каждой бирже) и БЕЗ учёта времени перевода монеты "
        "между биржами.\n\n"
        "🚚 <b>Фильтр перевода (/tr)</b>: бот проверяет статус ввода/вывода "
        "монеты ТОЛЬКО на HTX (публичные данные, без ключа). Статус MEXC "
        "недоступен без приватного API-ключа — эта сторона в каждом алерте "
        "помечена как «не проверяется», проверяй её на бирже вручную перед "
        "сделкой. Если фильтр выключен — алерты идут вообще без проверки "
        "переводимости ни по одной из бирж.",
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
        await message.answer(f"✅ Мин. % спреда для алерта: <b>{val}%</b>", parse_mode="HTML")
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


@dp.message(Command("tr"))
async def toggle_transfer_filter(message: types.Message):
    settings["chat_id"] = message.chat.id
    settings["require_transferable"] = not settings["require_transferable"]
    state = "ВКЛЮЧЕН" if settings["require_transferable"] else "ВЫКЛЮЧЕН"
    await message.answer(
        f"✅ Фильтр по доступности перевода: <b>{state}</b>\n"
        f"<i>Напоминание: реально проверяется только сторона HTX (публичный статус). "
        f"MEXC не проверяется — нет API-ключа, эта сторона в алерте всегда помечена как непроверенная.</i>",
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
        f"1️⃣ Общих USDT-пар на обеих биржах: {debug_stats['common_pairs']}",
        f"2️⃣ Прошли мин. объём 24ч на обеих биржах (/v): {debug_stats['passed_volume_floor']}",
        f"3️⃣ Прошли порог спреда (/sp): {debug_stats['passed_spread_filter']} (отсечено сверху /spmax: {debug_stats['blocked_by_sanity']})",
        f"4️⃣ Прошли фильтр стабильности (/ss): {debug_stats['passed_stability']}",
        f"5️⃣ Прошли фильтр перевода (/tr): {debug_stats['passed_transfer_check']} (заблокировано: {debug_stats['blocked_by_transfer']})",
        f"6️⃣ Прошли анти-спам (кулдаун /cd): {debug_stats['passed_cooldown']}",
        f"7️⃣ Прошли фильтр мин. оборота (/mt): {debug_stats['passed_turnover_filter']} "
        f"(отсечено без данных о глубине: {debug_stats['blocked_by_unknown_depth']}, "
        f"дозапрошен стакан HTX: {debug_stats['depth_fallback']})",
        f"📨 Алертов отправлено за этот проход: {debug_stats['alerts_sent']}",
    ]

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
    Реальные контракты/сети монет с MEXC через ПОДПИСЫВАЕМЫЙ эндпоинт
    /api/v3/capital/config/getall (Binance-style HMAC-SHA256, требует
    MEXC_API_KEY + MEXC_API_SECRET в переменных окружения; ключ — только Read).
    Если переменные не заданы — просто ничего не возвращает, остальной бот
    работает как и раньше, без проверки контрактов.
    Кэшируется надолго — список сетей/контрактов почти никогда не меняется.
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
                addr = net.get("contract") or net.get("contractAddress")
                fee_raw = net.get("withdrawFee")
                fee = None
                if fee_raw not in (None, ""):
                    try:
                        fee = float(fee_raw)
                    except (TypeError, ValueError):
                        fee = None
                if addr or fee is not None:
                    networks.append({
                        "network": net.get("network") or net.get("netWork") or "?",
                        "contract": addr,
                        "withdraw_fee": fee,
                        "withdraw_enable": net.get("withdrawEnable", True),
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
    минимальную границу комиссии (minTransactFeeWithdraw).

    ВАЖНО (был баг): у этой функции пропала строка `def`, и её тело оказалось
    недостижимым куском внутри get_mexc_contracts (сразу после return). Из-за
    этого вызов ниже падал с NameError, который гасился голым `except Exception:
    continue` — в кэш статусов перевода попадали ТОЛЬКО монеты с закрытым
    выводом на всех сетях, а по всем остальным статус считался «неизвестен», и
    фильтр /tr фактически не блокировал ничего."""
    fee_type = chain.get("withdrawFeeType")
    try:
        if fee_type == "fixed":
            return float(chain.get("transactFeeWithdraw", 0) or 0), "фикс"
        if fee_type in ("circulated", "ratio"):
            return float(chain.get("minTransactFeeWithdraw", 0) or 0), "мин"
    except Exception:
        pass
    return None, None


async def get_htx_transfer_status():
    """
    Статус ввода/вывода и комиссия за вывод по каждой монете на HTX. ПУБЛИЧНЫЙ
    эндпоинт, ключ не нужен. Агрегируем по всем сетям (chains) монеты: если хотя
    бы одна сеть открыта — считаем ввод/вывод доступным, а комиссию берём по
    САМОЙ ДЕШЁВОЙ из открытых для вывода сетей (для арбитража не важно через
    какую именно сеть, важно с какой минимальной комиссией). Кэшируется на
    HTX_TRANSFER_TTL — эти данные почти не меняются в течение дня.
    """
    now = time.time()
    if htx_transfer_cache["data"] and (now - htx_transfer_cache["ts"]) < HTX_TRANSFER_TTL:
        return htx_transfer_cache["data"]

    result = {}
    try:
        async with http_session.get("https://api.huobi.pro/v2/reference/currencies", headers=HTX_HEADERS, timeout=TIMEOUT_HEAVY) as resp:
            if resp.status != 200:
                body = await resp.text()
                debug_stats["last_error"] = f"HTX currencies HTTP {resp.status}: {body[:150]}"
                return htx_transfer_cache["data"]  # отдаём старый кэш, если был, лучше чем ничего
            payload = await resp.json()
    except Exception as e:
        debug_stats["last_error"] = f"HTX currencies запрос: {e}"
        return htx_transfer_cache["data"]

    for item in payload.get("data", []):
        try:
            coin = item.get("currency", "").upper()
            chains = item.get("chains", [])
            deposit_ok = any(ch.get("depositStatus") == "allowed" for ch in chains)
            withdraw_ok = any(ch.get("withdrawStatus") == "allowed" for ch in chains)

            best_fee, best_fee_type, best_chain = None, None, None
            for ch in chains:
                if ch.get("withdrawStatus") != "allowed":
                    continue
                fee, ftype = _extract_withdraw_fee(ch)
                if fee is not None and (best_fee is None or fee < best_fee):
                    best_fee, best_fee_type = fee, ftype
                    best_chain = ch.get("displayName") or ch.get("chain")

            result[coin] = {
                "deposit": deposit_ok, "withdraw": withdraw_ok,
                "fee": best_fee, "fee_type": best_fee_type, "fee_chain": best_chain,
            }
        except Exception as e:
            debug_stats["skipped_htx_transfer"] += 1
            debug_stats["skip_reason"] = f"HTX currencies: {type(e).__name__} {e}"
            continue

    if result:
        htx_transfer_cache["ts"] = now
        htx_transfer_cache["data"] = result
        debug_stats["htx_transfer_ok"] = True
    else:
        debug_stats["htx_transfer_ok"] = False

    return htx_transfer_cache["data"]


async def get_htx_depth(symbol_lower):
    """
    Топ стакана HTX точечным запросом — ФОЛБЭК на случай, когда в bulk-ответе
    /market/tickers по паре не оказалось bidSize/askSize (пустое поле или ноль).
    В норме не вызывается вообще: размеры берутся из bulk-ответа, который сканер
    тянет в любом случае (см. fetch_huobi_data). Раньше этот запрос уходил по
    КАЖДОМУ кандидату каждый проход — именно его мы убрали из горячего пути.
    Возвращает (bid_qty, ask_qty) в штуках монеты, либо (None, None) при ошибке.
    """
    url = f"https://api.huobi.pro/market/depth?symbol={symbol_lower}&type=step0"
    try:
        async with http_session.get(url, headers=HTX_HEADERS, timeout=TIMEOUT_DEPTH) as resp:
            if resp.status != 200:
                debug_stats["last_error"] = f"HTX depth {symbol_lower} HTTP {resp.status}"
                return None, None
            data = await resp.json()
        tick = data.get("tick") or {}
        bids = tick.get("bids") or []
        asks = tick.get("asks") or []
        bid_qty = float(bids[0][1]) if bids else None
        ask_qty = float(asks[0][1]) if asks else None
        return bid_qty, ask_qty
    except Exception as e:
        debug_stats["last_error"] = f"HTX depth {symbol_lower}: {type(e).__name__} {e}"
        return None, None


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
    task = schedule_save()
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
            debug_stats["passed_volume_floor"] = 0
            debug_stats["passed_spread_filter"] = 0
            debug_stats["passed_stability"] = 0
            debug_stats["passed_turnover_filter"] = 0
            debug_stats["passed_transfer_check"] = 0
            debug_stats["blocked_by_transfer"] = 0
            debug_stats["passed_cooldown"] = 0
            debug_stats["alerts_sent"] = 0
            debug_stats["skipped_mexc"] = 0
            debug_stats["skipped_htx"] = 0
            debug_stats["skipped_htx_transfer"] = 0
            debug_stats["blocked_by_sanity"] = 0
            debug_stats["blocked_by_unknown_depth"] = 0
            debug_stats["depth_fallback"] = 0

            if not mexc_data or not huobi_data:
                await asyncio.sleep(settings["check_interval"])
                continue

            common = set(mexc_data.keys()) & set(huobi_data.keys())
            common -= blacklist
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
                h = huobi_data[pair]

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
                if spread_mexc_to_htx >= mexc_to_htx_threshold:
                    dir_candidates.append(("MEXC", "HTX", spread_mexc_to_htx, m["ask"], h["bid"]))
                if spread_htx_to_mexc >= htx_to_mexc_threshold:
                    dir_candidates.append(("HTX", "MEXC", spread_htx_to_mexc, h["ask"], m["bid"]))

                # ===== САНИТИ-ОТСЕЧКА СВЕРХУ =====
                # Отбрасываем направления с неправдоподобно большим спредом ДО
                # выбора лучшего — иначе "спред" в 900%, возникший из-за разной
                # деноминации одноимённых тикеров, всегда побеждал бы в max()
                # и вытеснял настоящие сигналы по этой паре.
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
                buy_ex, sell_ex, best_spread, buy_price, sell_price = max(dir_candidates, key=lambda c: c[2])

                # ============ ФИЛЬТР СТАБИЛЬНОСТИ СПРЕДА (/ss) ============
                if settings["spread_stable_sec"] > 0:
                    first_seen = spread_track.get(pair)
                    if first_seen is None:
                        spread_track[pair] = now
                        continue  # первый раз видим спред выше порога — ждём подтверждения
                    if (now - first_seen) < settings["spread_stable_sec"]:
                        continue  # ещё не набрали нужную длительность
                debug_stats["passed_stability"] += 1

                # Именно срез, а не replace("USDT", ""): replace вырезает ВСЕ
                # вхождения, и тикер вида USDTBUSDT превратился бы в "B".
                # endswith("USDT") здесь уже гарантирован отбором пар выше.
                base_coin = pair[:-4]
                htx_leg = "withdraw" if buy_ex == "HTX" else "deposit"
                htx_coin_status = htx_transfer.get(base_coin)
                htx_known = htx_coin_status is not None
                htx_ok = htx_coin_status.get(htx_leg, False) if htx_known else True  # неизвестно = не блокируем

                if settings["require_transferable"] and htx_known and not htx_ok:
                    debug_stats["blocked_by_transfer"] += 1
                    continue
                debug_stats["passed_transfer_check"] += 1

                leg_name_ru = "вывод" if htx_leg == "withdraw" else "ввод"
                if not htx_known:
                    htx_transfer_label = f"❔ статус {leg_name_ru}а неизвестен"
                elif htx_ok:
                    htx_transfer_label = f"✅ {leg_name_ru.upper()} ОТКРЫТ"
                else:
                    htx_transfer_label = f"❌ {leg_name_ru.upper()} ЗАКРЫТ"

                # Анти-спам: простой кулдаун по времени.
                prev = alert_memory.get(pair)
                if prev and (now - prev["last_msg"]) < settings["cooldown_min"] * 60:
                    continue
                debug_stats["passed_cooldown"] += 1

                candidates.append({
                    "pair": pair, "m": m, "h": h,
                    "buy_ex": buy_ex, "sell_ex": sell_ex, "best_spread": best_spread,
                    "buy_price": buy_price, "sell_price": sell_price,
                    "base_coin": base_coin, "htx_leg": htx_leg,
                    "htx_coin_status": htx_coin_status, "htx_known": htx_known,
                    "leg_name_ru": leg_name_ru, "htx_transfer_label": htx_transfer_label,
                    "prev": prev,
                })

            if not candidates:
                await asyncio.sleep(settings["check_interval"])
                continue

            # ===== ФАЗА 2: добор глубины HTX ТОЛЬКО там, где её не было =====
            # В норме размеры топа стакана уже пришли в bulk-ответах (MEXC
            # bookTicker — bidQty/askQty, HTX tickers — bidSize/askSize), и
            # сетевых вызовов здесь не происходит вовсе. Но если HTX по какой-то
            # паре не отдал bidSize/askSize, без добора мы бы либо показали
            # "н/д", либо (при включённом /mt) молча потеряли живой сигнал —
            # поэтому по таким пáрам, и только по ним, дозапрашиваем стакан.
            need_depth = [
                c for c in candidates
                if c["h"].get("bid_qty") is None or c["h"].get("ask_qty") is None
            ]
            if need_depth:
                debug_stats["depth_fallback"] = len(need_depth)
                fetched = await asyncio.gather(
                    *[get_htx_depth(c["pair"].lower()) for c in need_depth],
                    return_exceptions=True,
                )
                for c, res in zip(need_depth, fetched):
                    if isinstance(res, Exception):
                        continue
                    got_bid, got_ask = res
                    if c["h"].get("bid_qty") is None:
                        c["h"]["bid_qty"] = got_bid
                    if c["h"].get("ask_qty") is None:
                        c["h"]["ask_qty"] = got_ask

            # ===== ФАЗА 3: фильтр мин. оборота + сборка сообщений =====
            def _fmt_qty(q):
                return f"{q:,.4f}".rstrip('0').rstrip('.') if q is not None else "н/д"

            messages = []  # [(chat_id_или_channel, текст), ...] — отправим все разом в конце

            for c in candidates:
                htx_bid_qty, htx_ask_qty = c["h"].get("bid_qty"), c["h"].get("ask_qty")

                pair, m, h = c["pair"], c["m"], c["h"]
                buy_ex, sell_ex = c["buy_ex"], c["sell_ex"]
                best_spread, buy_price, sell_price = c["best_spread"], c["buy_price"], c["sell_price"]
                base_coin, htx_leg = c["base_coin"], c["htx_leg"]
                htx_coin_status, htx_known = c["htx_coin_status"], c["htx_known"]
                leg_name_ru, htx_transfer_label = c["leg_name_ru"], c["htx_transfer_label"]
                prev = c["prev"]

                if buy_ex == "MEXC":
                    buy_qty, sell_qty = m.get("ask_qty"), htx_bid_qty
                else:
                    buy_qty, sell_qty = htx_ask_qty, m.get("bid_qty")

                tradable_usd = None
                if buy_qty is not None and sell_qty is not None:
                    tradable = min(buy_qty, sell_qty)
                    tradable_usd = tradable * buy_price

                # ============ ФИЛЬТР МИН. ОБОРОТА (/mt) ============
                # Если фильтр включён, а глубину посчитать не удалось — пара НЕ
                # проходит. Раньше такие пары проскакивали (условие требовало
                # tradable_usd is not None), то есть при включённом /mt всё равно
                # приходили алерты по парам с непроверенным оборотом.
                if settings["min_turnover_usd"] > 0:
                    if tradable_usd is None:
                        debug_stats["blocked_by_unknown_depth"] += 1
                        continue
                    if tradable_usd < settings["min_turnover_usd"]:
                        continue
                debug_stats["passed_turnover_filter"] += 1

                depth_line = f"📦 Доступно: купить {_fmt_qty(buy_qty)} {base_coin} / продать {_fmt_qty(sell_qty)} {base_coin}"
                if tradable_usd is not None:
                    depth_line += f" → прокрутить ~{_fmt_qty(min(buy_qty, sell_qty))} (~{fmt_money(tradable_usd)}$)"
                else:
                    depth_line += " → сумму для прокрутки посчитать не удалось (нет данных по одной из сторон)"

                alert_memory[pair] = {
                    "time": prev["time"] if prev else now,
                    "last_msg": now,
                    "spread": best_spread,
                }
                debug_stats["alerts_sent"] += 1

                # Комиссия за вывод релевантна, только если реально ВЫВОДИМ с HTX
                # (т.е. купили на HTX и переводим монету на MEXC для продажи).
                fee_line = None
                if htx_leg == "withdraw" and htx_known:
                    fee_amt = htx_coin_status.get("fee")
                    if fee_amt is not None:
                        fee_type = htx_coin_status.get("fee_type") or ""
                        fee_chain = htx_coin_status.get("fee_chain") or "?"
                        fee_usd = fee_amt * buy_price
                        fee_line = (
                            f"💸 Комиссия вывода с HTX ({fee_chain}, {fee_type}): "
                            f"{fee_amt:g} {base_coin} (~{fmt_money(fee_usd)}$)"
                        )
                elif htx_leg == "deposit":
                    # Значит выводим именно с MEXC — комиссию берём из того же
                    # подписанного запроса, что и контракт (доп. запрос не нужен).
                    mexc_nets = mexc_contracts.get(base_coin) or []
                    fee_candidates = [
                        n for n in mexc_nets
                        if n.get("withdraw_fee") is not None and n.get("withdraw_enable", True)
                    ]
                    if fee_candidates:
                        best = min(fee_candidates, key=lambda n: n["withdraw_fee"])
                        fee_usd = best["withdraw_fee"] * buy_price
                        fee_line = (
                            f"💸 Комиссия вывода с MEXC ({best['network']}): "
                            f"{best['withdraw_fee']:g} {base_coin} (~{fmt_money(fee_usd)}$)"
                        )

                lines = [
                    f"🔀 <b>СПРЕД: <code>{base_coin}</code></b>",
                    "",
                    f"💹 <b>{best_spread:+.2f}%</b> · Купить на <b>{buy_ex}</b> ({fmt_price(buy_price)}) "
                    f"→ Продать на <b>{sell_ex}</b> ({fmt_price(sell_price)})",
                    "",
                    f"📥 MEXC: bid {fmt_price(m['bid'])} / ask {fmt_price(m['ask'])}",
                    f"📤 HTX: bid {fmt_price(h['bid'])} / ask {fmt_price(h['ask'])}",
                    "",
                    depth_line,
                    "",
                    f"💰 Объём 24ч: MEXC {fmt_money(m['vol'])}$ · HTX {fmt_money(h['vol'])}$",
                    "",
                    f"🚚 Перевод HTX ({leg_name_ru}): {htx_transfer_label}",
                    f"⚠️ Перевод MEXC: не проверяется (нет API-ключа)",
                    f"<i>Статусы перевода справочные — сверяй на самой бирже перед крупным переводом, данные API могут отставать от реального состояния.</i>",
                ]
                if fee_line:
                    lines.append(fee_line)

                # Контракт MEXC — реальные данные, если заданы MEXC_API_KEY/SECRET;
                # HTX публичного источника контрактов не имеет.
                coin_networks = mexc_contracts.get(base_coin)
                if coin_networks:
                    top_nets = coin_networks[:3]
                    nets_str = "; ".join(f"{n['network']}: {n['contract']}" for n in top_nets)
                    lines.append(f"🔗 MEXC контракт: {nets_str}")
                    lines.append("<i>Сверь этот адрес на странице пополнения HTX вручную — авто-сверки с HTX нет (нет публичного источника контрактов).</i>")
                elif MEXC_API_KEY:
                    lines.append("🔗 Контракт MEXC: не найден в ответе API для этой монеты")
                else:
                    lines.append("🔗 Проверка контракта: выключена (не задан MEXC_API_KEY/SECRET)")
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
    BotCommand(command="sp", description="Мин. % спреда для алерта"),
    BotCommand(command="spm", description="Порог спреда именно MEXC→HTX"),
    BotCommand(command="spmax", description="Верхняя отсечка спреда (анти-мусор)"),
    BotCommand(command="v", description="Мин. объём 24ч на обеих биржах"),
    BotCommand(command="mt", description="Мин. сумма для прокрутки по стакану"),
    BotCommand(command="cd", description="Пауза между повторными алертами"),
    BotCommand(command="ss", description="Мин. время стабильности спреда"),
    BotCommand(command="tr", description="Вкл/выкл фильтр доступности перевода"),
    BotCommand(command="b", description="Добавить/убрать монету из ЧС"),
    BotCommand(command="bl", description="Показать список монет в ЧС"),
    BotCommand(command="mute", description="Временно замьютить монету"),
    BotCommand(command="unmute", description="Снять мут с монеты"),
    BotCommand(command="channel", description="Куда дублировать сигналы"),
]


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
        await dp.start_polling(bot)
    finally:
        # Render перезапускает процесс регулярно — закрываемся аккуратно, чтобы
        # не оставлять недописанное состояние и открытые соединения.
        if scanner is not None:
            scanner.cancel()
        await save_state()
        await http_session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
