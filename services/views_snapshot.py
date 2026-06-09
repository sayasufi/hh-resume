"""views-snapshot: ежечасный снимок просмотров резюме (по аккаунту) -> таблица
resume_views. Чтобы видеть динамику роста по дням/часам. Per-account (feature=None)."""
import asyncio
from hh_applicant_tool.storage import pgconn
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.api.user_agent import generate_android_useragent

DDL = ("CREATE TABLE IF NOT EXISTS resume_views ("
       "id bigserial PRIMARY KEY, account text NOT NULL, "
       "ts timestamptz NOT NULL DEFAULT now(), views int NOT NULL, new_views int);"
       "CREATE INDEX IF NOT EXISTS idx_rv_acc_ts ON resume_views(account, ts);")

async def main():
    cfg = pgconn.app_config(); tok = cfg.get("token") or {}
    if not tok.get("access_token"):
        return
    api = ApiClient(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                    access_expires_at=tok.get("access_expires_at", 0),
                    user_agent=generate_android_useragent(), refresh_hook=pgconn.locked_token_refresh)
    try:
        res = await api.get("/resumes/mine")
    finally:
        await api.aclose()
    tv = nv = 0
    for r in res.get("items", []):
        t = r.get("total_views"); n = r.get("new_views")
        tv += (t.get("total") if isinstance(t, dict) else (t or 0))
        nv += (n.get("total") if isinstance(n, dict) else (n or 0))
    acc = pgconn.get_account()
    c = pgconn.connect(); cur = c.cursor()
    cur.execute(DDL)
    cur.execute("INSERT INTO resume_views(account, views, new_views) VALUES (%s,%s,%s)", (acc, tv, nv))
    c.commit(); c.close()
    print(f"views-snapshot {acc}: {tv} (new {nv})")

if __name__ == "__main__":
    asyncio.run(main())
