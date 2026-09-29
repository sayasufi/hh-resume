"""База ответов кандидата и очередь вопросов «ответь сам».

Вопросы, на которые отвечает только кандидат (самозанятость, переезд, СВО, английский…),
reply-employers кладёт в pending_questions и присылает в Telegram. Кандидат отвечает один
раз — кнопкой или реплаем; ответ уходит работодателю и запоминается в answer_bank, дальше
бот на такой же по смыслу вопрос отвечает сам.

account везде передаётся ЯВНО: модуль зовут и reply-employers (HH_ACCOUNT в env), и общий
Telegram-бот — один процесс на всех пользователей.
"""
from __future__ import annotations

from . import pgconn

_DDL = (
    "CREATE TABLE IF NOT EXISTS answer_bank ("
    " id bigserial PRIMARY KEY, account text NOT NULL, question text NOT NULL,"
    " answer text NOT NULL, used int NOT NULL DEFAULT 0, created_at timestamptz DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS idx_answer_bank_acc ON answer_bank(account)",
    "CREATE TABLE IF NOT EXISTS pending_questions ("
    " id bigserial PRIMARY KEY, account text NOT NULL, nid text NOT NULL, msg_id text NOT NULL,"
    " question text NOT NULL, vacancy text, employer text, link text,"
    " yes_no boolean NOT NULL DEFAULT false, tg_msg_id bigint,"
    " status text NOT NULL DEFAULT 'open', answer text,"
    " created_at timestamptz DEFAULT now(), answered_at timestamptz,"
    " UNIQUE (account, nid, msg_id))",
    "CREATE INDEX IF NOT EXISTS idx_pending_tg ON pending_questions(account, tg_msg_id)",
)
_ready = False
_PENDING_COLS = ("id", "account", "nid", "msg_id", "question", "vacancy", "employer", "link",
                 "yes_no", "tg_msg_id", "status", "answer")


def _q(sql: str, params=(), fetch: str | None = None):
    global _ready
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            if not _ready:
                for ddl in _DDL:
                    cur.execute(ddl)
                _ready = True
            cur.execute(sql, params)
            rows = cur.fetchall() if fetch == "all" else cur.fetchone() if fetch == "one" else None
        conn.commit()
        return rows
    finally:
        conn.close()


# --- база ответов ---

def list_answers(account: str) -> list[dict]:
    rows = _q("SELECT id, question, answer FROM answer_bank WHERE account=%s ORDER BY id",
              (account,), fetch="all")
    return [{"id": r[0], "question": r[1], "answer": r[2]} for r in rows]


def add_answer(account: str, question: str, answer: str) -> None:
    _q("INSERT INTO answer_bank(account, question, answer) VALUES (%s, %s, %s)",
       (account, question.strip()[:1000], answer.strip()[:1000]))


def bump_used(answer_id: int) -> None:
    _q("UPDATE answer_bank SET used = used + 1 WHERE id=%s", (answer_id,))


def delete_answer(account: str, answer_id: int) -> bool:
    row = _q("DELETE FROM answer_bank WHERE account=%s AND id=%s RETURNING id",
             (account, answer_id), fetch="one")
    return row is not None


# --- вопросы, ждущие ответа кандидата ---

def create_pending(account: str, nid, msg_id, question: str, vacancy: str, employer: str,
                   link: str, yes_no: bool) -> int | None:
    """id нового вопроса или None, если этот вопрос (чат + сообщение) уже в очереди."""
    row = _q(
        "INSERT INTO pending_questions(account, nid, msg_id, question, vacancy, employer, link,"
        " yes_no) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (account, nid, msg_id) DO NOTHING"
        " RETURNING id",
        (account, str(nid), str(msg_id), question[:2000], vacancy, employer, link, yes_no),
        fetch="one",
    )
    return row[0] if row else None


def set_tg_msg(pending_id: int, tg_msg_id: int) -> None:
    _q("UPDATE pending_questions SET tg_msg_id=%s WHERE id=%s", (tg_msg_id, pending_id))


def _pending(where: str, params) -> dict | None:
    row = _q(f"SELECT {', '.join(_PENDING_COLS)} FROM pending_questions WHERE {where}",
             params, fetch="one")
    return dict(zip(_PENDING_COLS, row)) if row else None


def get_pending(pending_id: int) -> dict | None:
    return _pending("id=%s", (pending_id,))


def pending_by_tg(account: str, tg_msg_id: int) -> dict | None:
    return _pending("account=%s AND tg_msg_id=%s", (account, tg_msg_id))


def close_pending(pending_id: int, status: str, answer: str | None = None) -> None:
    _q("UPDATE pending_questions SET status=%s, answer=%s, answered_at=now() WHERE id=%s",
       (status, answer, pending_id))
