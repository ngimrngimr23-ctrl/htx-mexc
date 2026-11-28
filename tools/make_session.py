"""Одноразово получить строку сессии юзербота (TG_SESSION) для халяль-проверки и @SpreaderApp.

Запускается у тебя на компьютере, НЕ на Render и НЕ в репозитории:
    pip install telethon
    python make_session.py

Спросит api_id и api_hash (my.telegram.org → API development tools), потом номер
телефона, код из Telegram и пароль двухэтапной проверки, если он есть. В конце
напечатает строку — её в Render → Environment как TG_SESSION.

Google Colab (там уже запущен event loop, asyncio.run не работает) — в ячейку:
    !pip install -q telethon
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id = int(input("api_id: ")); api_hash = input("api_hash: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print(client.session.save())

ВАЖНО: строка сессии даёт ПОЛНЫЙ доступ к аккаунту. Только в переменные окружения
хостинга — никогда в чат, код или git. Лучше завести для этого отдельный аккаунт и
один раз нажать в нём Start в @CryptoGulfHalal_Bot и @SpreaderApp.
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()
    # async with сам спросит телефон, код и пароль 2FA
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        print("\nВошёл как", "@" + me.username if me.username else me.id)
        print("\nTG_SESSION (скопируй целиком, это секрет):\n")
        print(client.session.save())
        print("\nTG_API_ID =", api_id)
        print("TG_API_HASH = (тот, что ты ввёл)")


if __name__ == "__main__":
    asyncio.run(main())
