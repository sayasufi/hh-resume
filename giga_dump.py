#!/usr/bin/env python3
"""Read-only дамп чата с @Giga_recruiter_bot для HH_ACCOUNT. argv[1]=limit (def 60)."""
import asyncio
import sys
import giga_recruiter as gr
from hh_applicant_tool.storage import pgconn


async def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    cfg = pgconn.app_config()
    enc = cfg.get("tg_user_session")
    if not enc:
        print("нет сессии"); return
    api_id, api_hash = pgconn.tg_api()
    bot = pgconn.get_setting("giga.bot", gr.DEFAULT_BOT) or gr.DEFAULT_BOT
    client = gr.TelegramClient(gr.StringSession(pgconn.dec_session(enc)), api_id, api_hash)
    await client.connect()
    print("authorized:", await client.is_user_authorized(), "| bot:", bot)
    msgs = await client.get_messages(await client.get_entity(bot), limit=limit)
    print(f"--- последние {len(msgs)} сообщений (старые сверху) ---")
    for m in reversed(msgs):
        who = "Я " if m.out else "ГР"
        btns = []
        for row in (getattr(m, "buttons", None) or []):
            for b in row:
                if getattr(b, "text", None):
                    btns.append(b.text)
        t = (m.text or "").replace("\n", " ").strip()
        line = f"[{str(m.date)[11:19]}] {who}: {t}"
        if btns:
            line += f"  KB:{btns}"
        print(line)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
