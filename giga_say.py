#!/usr/bin/env python3
"""Отправить одно текстовое сообщение боту ГР и подождать ответ (диагностика)."""
import asyncio
import sys
import time
import giga_recruiter as gr
from hh_applicant_tool.storage import pgconn


async def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "Да"
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    api_id, api_hash = pgconn.tg_api()
    bot = pgconn.get_setting("giga.bot", gr.DEFAULT_BOT) or gr.DEFAULT_BOT
    client = gr.TelegramClient(gr.StringSession(pgconn.dec_session(enc)), api_id, api_hash)
    await client.connect()
    entity = await client.get_entity(bot)
    base = await client.get_messages(entity, limit=1)
    last_id = base[0].id if base else 0
    print(f">>> отправляю текст: {text!r}")
    s = await client.send_message(entity, text, link_preview=False)
    last_id = max(last_id, s.id)
    deadline = time.time() + 185
    got = False
    while time.time() < deadline:
        await asyncio.sleep(5)
        msgs = await client.get_messages(entity, limit=8)
        new = sorted([m for m in msgs if (not m.out) and m.id > last_id], key=lambda x: x.id)
        if new:
            for m in new:
                print(f"🤖 ГР: {(m.text or '').replace(chr(10),' ')[:260]}")
            got = True
            break
    if not got:
        print("[бот молчит 185c]")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
