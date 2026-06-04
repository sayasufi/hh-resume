# GetMatch Source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Авто-отклик от лица кандидата на ленту вакансий GetMatch через Telegram-бота @g_jobbot (один клик, дедуп, дневной лимит 50), но ТОЛЬКО при подтверждённом профиле.

**Architecture:** Новая standalone-операция `services/getmatch_apply.py` (зеркало `giga_recruiter.py`): Telethon-сессия кандидата + advisory-лок → проверка профиля → цикл «новая вакансия из ленты → клик Откликнуться → пагинация». Гейтинг `feat.getmatch` (нужна TG-сессия), JOBS-строка в Prefect, тумблер+счётчик в Mini App. Дедуп `seen_keys("getmatch")`, лимит `activity_daily(kind=getmatch)`.

**Tech Stack:** Python async, Telethon (StringSession, message.click), psycopg/pgconn, Prefect (JOBS), FastAPI+ванильный JS (кабинет), pytest.

Спек: `docs/superpowers/specs/2026-06-05-getmatch-source-design.md`.

---

### Task 1: Калибровочный спайк на живой сессии (read-only)

Цель — зафиксировать ТОЧНЫЕ маркеры и способ выборки ленты ДО кода. Не пишет prod-код.

**Files:** нет (разовый скрипт во временном файле `_gm_spike.py`, потом удалить).

- [ ] **Step 1: Прогнать через сессию Семёна (account 144968591) read-only**

Скрипт (на сервере, `docker exec -e HH_ACCOUNT=144968591 hh_web python /app/_gm_spike.py`):
```python
import asyncio
from hh_applicant_tool.storage import pgconn
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    cfg = pgconn.app_config(account="144968591")
    api_id, api_hash = pgconn.tg_api()
    cl = TelegramClient(StringSession(pgconn.dec_session(cfg["tg_user_session"])), api_id, api_hash)
    await cl.connect()
    ent = await cl.get_entity("g_jobbot")
    # 1) /profile -> точный текст подтверждённого профиля
    before = (await cl.get_messages(ent, limit=1))[0].id
    await cl.send_message(ent, "/profile"); await asyncio.sleep(5)
    for m in [x for x in await cl.get_messages(ent, limit=5) if x.id > before and not x.out]:
        print("PROFILE:", repr((m.text or "")[:200]))
    # 2) /job_offers -> присылает ли пачку вакансий с кнопками + "Ещё N"
    before = (await cl.get_messages(ent, limit=1))[0].id
    await cl.send_message(ent, "/job_offers"); await asyncio.sleep(7)
    for m in reversed([x for x in await cl.get_messages(ent, limit=12) if x.id > before and not x.out]):
        btns = [b.text for row in (m.buttons or []) for b in row]
        print("OFFER:", repr((m.text or "")[:90]), "BTNS:", btns)
    await cl.disconnect()
asyncio.run(main())
```
Expected: видим (а) точный текст «подтверждённого профиля», (б) присылает ли `/job_offers` сообщения-вакансии с кнопкой «💥 Откликнуться в боте» и кнопкой «Ещё N вакансий».

- [ ] **Step 2: Зафиксировать находки в комментарии Task 3** — какой триггер ленты (`/job_offers` или чтение push), точные подстроки маркеров. Удалить `_gm_spike.py`.

---

### Task 2: Чистые хелперы парсинга + тесты (TDD)

**Files:**
- Create: `services/getmatch_apply.py` (только хелперы на этом шаге)
- Create: `tests/test_getmatch.py`

- [ ] **Step 1: Написать падающие тесты**

`tests/test_getmatch.py`:
```python
from types import SimpleNamespace
from services import getmatch_apply as g


def _btn(text):
    return SimpleNamespace(text=text)


def test_extract_vacancy_id():
    assert g.extract_vacancy_id(
        "https://getmatch.ru/vacancies/34650-senior-data-scientist-sberads?s=bot") == "34650"
    assert g.extract_vacancy_id("https://getmatch.ru/companies") is None
    assert g.extract_vacancy_id("") is None


def test_is_profile_ok():
    assert g.is_profile_ok("У вас подтверждённый профиль 🤘 ...") is True
    assert g.is_profile_ok("С ним можно откликаться на вакансии в один клик") is True
    assert g.is_profile_ok("Заполните профиль: пришлите ссылку на резюме") is False
    assert g.is_profile_ok("") is False


def test_find_apply_button():
    rows = [[_btn("Описание"), _btn("Вакансии (177)")], [_btn("💥 Откликнуться в боте")]]
    assert g.find_apply_button(rows).text == "💥 Откликнуться в боте"
    assert g.find_apply_button([[_btn("Описание")]]) is None
    assert g.find_apply_button([]) is None


def test_applied_ok():
    assert g.applied_ok("Отклик отправлен работодателю") is True
    assert g.applied_ok("Отклик уже отправлен работодателю") is True
    assert g.applied_ok("Что-то пошло не так") is False


def test_find_more_button():
    assert g.find_more_button([[_btn("Ещё 177 вакансий для вас")]]).text.startswith("Ещё")
    assert g.find_more_button([[_btn("Откликнуться")]]) is None
```

- [ ] **Step 2: Запустить — падает (нет модуля)**

Run: `docker run --rm -v /var/www1/hh-applicant-tool:/app -w /app hh_applicant_tool:latest sh -c "pip install -q pytest && python -m pytest tests/test_getmatch.py -q"`
Expected: FAIL (ImportError / AttributeError).

- [ ] **Step 3: Реализовать хелперы**

`services/getmatch_apply.py` (верх файла):
```python
"""Авто-отклик на ленту GetMatch через Telegram-бота @g_jobbot (Telethon).
Запуск: python /app/services/getmatch_apply.py [--dry]   (обычно через Prefect JOBS).
Гейт: feat.getmatch + app_config.tg_user_session. Профиль обязан быть подтверждён."""
import asyncio
import os
import random
import re
import sys

from hh_applicant_tool.storage import pgconn

DRY = "--dry" in sys.argv
BOT = "g_jobbot"
SEEN_KIND = "getmatch"
DEFAULT_MAX = 50

_VAC_RE = re.compile(r"getmatch\.ru/vacancies/(\d+)")
_APPLIED_RE = re.compile(r"отклик.{0,20}отправлен", re.I)


def extract_vacancy_id(url: str):
    m = _VAC_RE.search(url or "")
    return m.group(1) if m else None


def is_profile_ok(text: str) -> bool:
    t = (text or "").lower()
    return "подтвержд" in t or "в один клик" in t


def _btn_text(b) -> str:
    return getattr(b, "text", "") or ""


def find_apply_button(buttons):
    for row in (buttons or []):
        for b in row:
            t = _btn_text(b)
            if "Откликнуться" in t or "💥" in t:
                return b
    return None


def find_more_button(buttons):
    for row in (buttons or []):
        for b in row:
            t = _btn_text(b).lower()
            if ("ещё" in t or "еще" in t) and "ваканс" in t:
                return b
    return None


def applied_ok(text: str) -> bool:
    t = text or ""
    return bool(_APPLIED_RE.search(t)) or "Мои отклики" in t
```

- [ ] **Step 4: Запустить — проходит**

Run: тот же pytest. Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add services/getmatch_apply.py tests/test_getmatch.py
git commit -m "feat(getmatch): чистые хелперы парсинга ленты @g_jobbot + тесты"
```

---

### Task 3: Основная операция (Telethon-цикл, profile-гейт, лимит)

**Files:**
- Modify: `services/getmatch_apply.py` (добавить main + цикл)

- [ ] **Step 1: Дописать операцию (после хелперов)**

```python
def _today_count(account: str) -> int:
    conn = pgconn.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(count,0) FROM activity_daily "
                        "WHERE account=%s AND kind=%s AND day=current_date",
                        (account, SEEN_KIND))
            r = cur.fetchone()
        return int(r[0]) if r else 0
    finally:
        conn.close()


async def _read_new(client, ent, after_id):
    msgs = await client.get_messages(ent, limit=30)
    return [m for m in reversed(msgs) if m.id > after_id and not m.out]


async def run(account: str):
    cfg = pgconn.app_config(account=account)
    if not pgconn.feature_enabled("getmatch", account):
        print("feat.getmatch выключен — пропуск"); return
    enc = cfg.get("tg_user_session")
    if not enc:
        print("нет tg_user_session — пропуск"); return

    # single-flight: тот же advisory-лок, что у giga (по хешу account)
    lock_id = pgconn.advisory_lock_id("getmatch:" + account)
    conn = pgconn.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        if not cur.fetchone()[0]:
            print("getmatch: уже выполняется для аккаунта — пропуск"); conn.close(); return
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        api_id, api_hash = pgconn.tg_api()
        client = TelegramClient(StringSession(pgconn.dec_session(enc)), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            print("getmatch: сессия не авторизована"); return
        ent = await client.get_entity(BOT)

        # --- profile-гейт ---
        before = (await client.get_messages(ent, limit=1))[0].id
        await client.send_message(ent, "/profile")
        await asyncio.sleep(5)
        prof = " ".join((m.text or "") for m in await _read_new(client, ent, before))
        if not is_profile_ok(prof):
            print("getmatch: профиль НЕ подтверждён — не откликаемся, кладём дело")
            if not DRY:
                pgconn.notify(pgconn.PRIORITY_MED,
                              "Подтвердите профиль на GetMatch (@g_jobbot, /profile), "
                              "чтобы бот начал откликаться.",
                              category="action", link="https://t.me/g_jobbot",
                              dedup_key="getmatch:profile", account=account)
            return

        # --- лимит ---
        limit = int(pgconn.get_setting("getmatch.max_per_day", DEFAULT_MAX, account=account) or DEFAULT_MAX)
        sent = _today_count(account)
        if sent >= limit:
            print(f"getmatch: дневной лимит достигнут ({sent}/{limit})"); return
        seen = pgconn.seen_keys(SEEN_KIND, account=account)

        # --- триггер ленты + цикл ---
        before = (await client.get_messages(ent, limit=1))[0].id
        await client.send_message(ent, "/job_offers")
        await asyncio.sleep(6)
        rounds = 0
        while sent < limit and rounds < 40:
            rounds += 1
            batch = await _read_new(client, ent, before)
            if batch:
                before = max(m.id for m in batch)
            more = None
            applied_any = False
            for m in batch:
                if more is None:
                    more = find_more_button(m.buttons)
                vid = extract_vacancy_id(m.text or "")
                if not vid or vid in seen:
                    continue
                ab = find_apply_button(m.buttons)
                if not ab:
                    pgconn.add_seen(SEEN_KIND, [vid], account=account); seen.add(vid)
                    continue
                if DRY:
                    print(f"getmatch[dry]: откликнулся бы на vac {vid}")
                    pgconn.add_seen(SEEN_KIND, [vid], account=account); seen.add(vid)
                    sent += 1; applied_any = True
                else:
                    try:
                        res = await ab.click()
                        await asyncio.sleep(2)
                        ok = applied_ok(getattr(res, "message", "") or "")
                    except Exception as e:
                        print("getmatch: клик не удался:", repr(e)[:80]); ok = False
                    pgconn.add_seen(SEEN_KIND, [vid], account=account); seen.add(vid)
                    if ok:
                        pgconn.bump_activity("getmatch", 1, account=account)
                        sent += 1; applied_any = True
                        print(f"getmatch: отклик на vac {vid} ({sent}/{limit})")
                    await asyncio.sleep(random.uniform(5, 15))
                if sent >= limit:
                    break
            if sent >= limit:
                break
            # пагинация
            if more is not None:
                before2 = (await client.get_messages(ent, limit=1))[0].id
                try:
                    await more.click()
                except Exception:
                    break
                await asyncio.sleep(5)
                before = before2
            elif not applied_any:
                break
        print(f"getmatch: готово, отправлено {sent - _today_count(account) if DRY else ''} (всего сегодня {sent}/{limit})")
        await client.disconnect()
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        conn.close()


if __name__ == "__main__":
    asyncio.run(run(pgconn.get_account()))
```

- [ ] **Step 2: Проверить, что нужные хелперы pgconn существуют**

Run: `docker exec hh_web python -c "from hh_applicant_tool.storage import pgconn; print(all(hasattr(pgconn,a) for a in ['advisory_lock_id','seen_keys','add_seen','tg_api','dec_session','get_account','feature_enabled','get_setting','notify','bump_activity','app_config','connect']))"`
Expected: `True`. Если `advisory_lock_id`/`dec_session`/`tg_api` называются иначе — взять реальные имена из `giga_recruiter.py` (он их уже использует) и поправить вызовы.

- [ ] **Step 3: py_compile**

Run: `docker exec hh_web python -c "compile(open('/app/services/getmatch_apply.py',encoding='utf-8').read(),'g','exec')" && echo OK`
Expected: OK.

- [ ] **Step 4: Commit**

```bash
git add services/getmatch_apply.py
git commit -m "feat(getmatch): операция авто-отклика (Telethon, profile-гейт, лимит 50, дедуп, --dry)"
```

---

### Task 4: Гейтинг (web_app) + JOBS-строка (Prefect)

**Files:**
- Modify: `services/web_app.py` (FEATURES, _set_config, _activity)
- Modify: `orchestration/flows.py` (JOBS)

- [ ] **Step 1: FEATURES += getmatch**

В `services/web_app.py`:
```python
FEATURES = ("apply", "tests", "reply", "browse", "notify", "giga", "getmatch")
```

- [ ] **Step 2: _set_config — getmatch требует tg_user_session (как giga)**

В `_set_config`, в ветке `if key in FEATURES:` рядом с проверкой giga:
```python
        if key in ("giga", "getmatch") and bool(value):
            cfg = await asyncio.to_thread(pgconn.app_config, account)
            if not cfg.get("tg_user_session"):
                raise HTTPException(
                    400, "Подключите Telegram (/connect в боте), чтобы включить эту функцию.")
```
(заменить существующую giga-only проверку на этот вид с обеими фичами.)

- [ ] **Step 3: _activity — добавить getmatch в выдаваемые kind**

В `_activity` последняя строка:
```python
    return {k: agg.get(k, 0) for k in ("apply", "tests", "reply", "browse", "bump", "getmatch")}
```

- [ ] **Step 4: JOBS-строка в `orchestration/flows.py`** (после строки `giga`)

```python
    dict(name="getmatch",       command=["python", "/app/services/getmatch_apply.py"],
         feature="getmatch", cron="10 6-18/4 * * *", jitter=120, tags=[],          timeout=1200),
```

- [ ] **Step 5: Проверки**

Run: `docker exec hh_web python -c "compile(open('/app/services/web_app.py',encoding='utf-8').read(),'w','exec'); print('web ok')"`
Run: `docker compose restart hh-web && sleep 8 && docker exec hh_web python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:60080/',timeout=6).status)"`
Expected: web ok / 200.
Run: `docker exec hh_orchestrator python -c "from orchestration import flows; print('getmatch' in [j['name'] for j in flows.JOBS])"`
Expected: True.

- [ ] **Step 6: Зарегистрировать деплоймент + перезапуск оркестратора**

Run: `docker compose restart hh-orchestrator && sleep 10 && docker exec hh_orchestrator prefect deployment ls 2>&1 | grep -c getmatch`
Expected: ≥1 (`dispatch/hh-getmatch`).

- [ ] **Step 7: Commit**

```bash
git add services/web_app.py orchestration/flows.py
git commit -m "feat(getmatch): гейтинг feat.getmatch (нужна TG-сессия) + JOBS-строка + счётчик активности"
```

---

### Task 5: Кабинет — тумблер «GetMatch» + счётчик

**Files:**
- Modify: `webapp_static/index.html` (тумблер в «Прочее», счётчик в «Активность»)
- Modify: `webapp_static/app.js` (bindToggles уже общий по `data-feat`; renderActivity += getmatch)

- [ ] **Step 1: index.html — тумблер рядом с ГР (в блоке «Прочее»)**

После строки тумблера `data-feat="giga"` добавить:
```html
          <label class="cell toggle"><span class="t"><b>GetMatch</b><small>авто-отклики на ленту getmatch.ru</small></span>
            <input type="checkbox" data-feat="getmatch"><i></i></label>
```

- [ ] **Step 2: index.html — счётчик в «Активность бота за период»** (после `a-bump` stat)

```html
          <div class="stat"><div class="num" id="a-getmatch">—</div><div class="lbl">Откликов через GetMatch</div></div>
```

- [ ] **Step 3: app.js — renderActivity += getmatch**

В функции `renderActivity(a)` добавить строку:
```javascript
  $("#a-getmatch").textContent = a.getmatch || 0;
```
(Тумблер `getmatch` подхватится существующим `bindToggles` по `data-feat`; он уже блокирует фичи без tgConnected только для giga — расширить условие `lockGiga` на getmatch:)
В `bindToggles`, строка вычисления блокировки:
```javascript
    const needsTg = inp.dataset.feat === "giga" || inp.dataset.feat === "getmatch";
    const lock = needsTg && !tgConnected;
    inp.disabled = lock;
    if (lock) inp.checked = false;
    inp.closest(".toggle").classList.toggle("disabled", lock);
```
(заменить прежние `lockGiga`-строки на `lock`/`needsTg`.)

- [ ] **Step 4: node --check + деплой статики**

Run (локально): `node --check webapp_static/app.js && echo JS_OK`
Деплой: запушить три файла, `docker exec hh_web` проверить, что `/app.js` содержит `a-getmatch` и кабинет отдаёт 200.

- [ ] **Step 5: Commit**

```bash
git add webapp_static/index.html webapp_static/app.js
git commit -m "feat(getmatch): тумблер GetMatch (нужна TG) + счётчик откликов в кабинете"
```

---

### Task 6: Боевая калибровка — dry-run → малый лимит → расписание

**Files:** нет (операционные прогоны).

- [ ] **Step 1: Включить feat.getmatch Семёну** (у него есть tg_session)

Run: `docker exec hh_web python -c "from hh_applicant_tool.storage import pgconn; pgconn.set_setting('feat.getmatch',True,account='144968591'); pgconn.set_setting('getmatch.max_per_day',3,account='144968591'); print('on, limit 3')"`

- [ ] **Step 2: --dry прогон на Семёне**

Run: `docker exec -e HH_ACCOUNT=144968591 hh_web python /app/services/getmatch_apply.py --dry 2>&1 | grep -i getmatch`
Expected: profile-гейт прошёл (профиль подтверждён), «откликнулся бы на vac …» по нескольким новым вакансиям, дедуп работает, реальных откликов НЕТ. Если что-то не так (триггер ленты/кнопки) — поправить регексы/триггер из Task 1 и повторить.

- [ ] **Step 3: Боевой прогон лимит=3**

Run: `docker exec -e HH_ACCOUNT=144968591 hh_web python /app/services/getmatch_apply.py 2>&1 | grep -i getmatch`
Expected: ровно 3 реальных отклика, `activity_daily(getmatch)=3`, в боте «Отклик отправлен». Проверить @g_jobbot «Мои отклики», что отклики появились и адекватные.

- [ ] **Step 4: Проверить дедуп — повторный прогон**

Run: тот же боевой прогон. Expected: 0 новых откликов (всё в seen) ИЛИ только реально новые из ленты; не дублирует.

- [ ] **Step 5: Поднять лимит до 50, оставить на расписании**

Run: `docker exec hh_web python -c "from hh_applicant_tool.storage import pgconn; pgconn.set_setting('getmatch.max_per_day',50,account='144968591'); print('limit 50')"`
Деплоймент `dispatch/hh-getmatch` уже на расписании (Task 4). Готово.

---

## Self-Review (покрытие спека)

- §4 profile-гейт → Task 3 (is_profile_ok + ветка notify). ✓
- §4 цикл/дедуп/лимит/пагинация → Task 3. ✓
- §4 --dry безопасный → Task 3 (dry не кликает). ✓
- §5 данные (seen_keys/activity_daily/settings/feat/action_items) → Task 3/4. ✓
- §6 гейтинг + JOBS → Task 4. ✓
- §7 кабинет (тумблер+счётчик) → Task 5. ✓
- §8 защита (лок/защитный матч/cap/паузы) → Task 3. ✓
- §9 тесты → Task 2. ✓
- §10 этапы (спайк→код→dry→боевой) → Task 1/6. ✓

Открытый риск: точные имена pgconn-хелперов (`advisory_lock_id`, `dec_session`, `tg_api`) — свериться с `giga_recruiter.py` в Task 3 Step 2 (giga их уже использует) и при расхождении взять реальные.
