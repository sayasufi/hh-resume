"""Поисковые запросы hh по резюме: разбор ответа LLM и пересборка только при смене профиля."""
import asyncio

from hh_applicant_tool.utils import search_queries as sq


class FakeSettings:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get_value(self, key, default=None):
        return self.data.get(key, default)

    async def set_value(self, key, value, commit=None):
        self.data[key] = value


class FakeChat:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    async def send_message(self, message):
        self.calls += 1
        return self.reply


def test_parse_queries_cleans_llm_output():
    raw = ("Вот запросы:\n1. python OR golang backend\n- «Platform engineer»\n• LLM engineer\n"
           "python OR golang backend\n(broken query\n\n" + "x" * 90)
    assert sq.parse_queries(raw) == ["python OR golang backend", "Platform engineer", "LLM engineer"]


def test_parse_queries_splits_pipes_and_fixes_bare_go():
    raw = 'python OR golang OR "go lang" | backend OR бэкенд\n"data engineer" OR "etl engineer" | python OR go'
    assert sq.parse_queries(raw) == [
        'python OR golang OR "go lang"', "backend OR бэкенд",
        '"data engineer" OR "etl engineer"', "python OR golang",
    ]


def test_parse_queries_caps_count():
    raw = "\n".join(f"query {i}" for i in range(10))
    assert len(sq.parse_queries(raw)) == sq.MAX_QUERIES


def test_profile_key_ignores_order_and_case():
    a = sq.profile_key("Backend Engineer", ["Python", "Go"], ["Dev в Cally"])
    b = sq.profile_key("backend  engineer", ["go", "python"], ["dev в cally"])
    assert a == b
    assert a != sq.profile_key("Backend Engineer", ["Python"], ["Dev в Cally"])


def test_ensure_queries_regenerates_only_on_profile_change():
    settings = FakeSettings()
    chat = FakeChat("python backend\ngolang developer\nLLM engineer")
    q1 = asyncio.run(sq.ensure_queries(settings, chat, "профиль", "k1"))
    assert q1 == ["python backend", "golang developer", "LLM engineer"] and chat.calls == 1
    q2 = asyncio.run(sq.ensure_queries(settings, chat, "профиль", "k1"))
    assert q2 == q1 and chat.calls == 1  # профиль тот же — LLM не зовём
    chat.reply = "data engineer\nairflow etl"
    q3 = asyncio.run(sq.ensure_queries(settings, chat, "профиль", "k2"))
    assert q3 == ["data engineer", "airflow etl"] and chat.calls == 2


def test_ensure_queries_keeps_old_on_bad_llm_answer():
    settings = FakeSettings({"apply.search_queries": "python backend\ngolang", "apply.search_queries_key": "old"})
    chat = FakeChat("извините, не могу")
    assert asyncio.run(sq.ensure_queries(settings, chat, "профиль", "new")) == ["python backend", "golang"]
    assert settings.data["apply.search_queries_key"] == "old"
