import asyncio

from services.getmatch_api import abs_url, apply_form, profile_filters, GetMatchAPI


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_offers_empty_and_params():
    api = GetMatchAPI("acc", None)
    cap = {}

    async def fake_get(url, params=None):
        cap["url"], cap["params"] = url, params
        return _Resp({})  # ответ без ключа offers -> []

    api.hc.get = fake_get
    res = asyncio.run(api.offers(limit=5, sp=["python"]))
    assert res == []
    assert cap["params"]["exclude_applied"] == "true"
    assert cap["params"]["limit"] == 5
    assert cap["params"]["sp"] == ["python"]
    asyncio.run(api.aclose())


def test_offers_passthrough():
    api = GetMatchAPI("acc", None)

    async def fake_get(url, params=None):
        return _Resp({"offers": [{"id": 1}, {"id": 2}]})

    api.hc.get = fake_get
    assert [o["id"] for o in asyncio.run(api.offers())] == [1, 2]
    asyncio.run(api.aclose())


def test_apply_multipart_cover_letter():
    api = GetMatchAPI("acc", None)
    cap = {}

    async def fake_post(url, files=None):
        cap["url"], cap["files"] = url, files
        return _Resp({"application_id": "x"})

    api.hc.post = fake_post
    offer = {"id": 777, "location_requirements": [{"location_id": "msk"}]}
    me = {"first_name": "A", "last_name": "B", "salary_from": 100000, "salary_currency": "RUB"}
    asyncio.run(api.apply(offer, me))  # без письма
    assert cap["url"] == "/api/offers/777/apply"
    assert "cover_letter" not in cap["files"]
    asyncio.run(api.apply(offer, me, cover_letter="привет"))  # с письмом
    assert cap["files"]["cover_letter"] == (None, "привет")
    asyncio.run(api.aclose())


def test_abs_url():
    assert abs_url("/vacancies/34693-mlops") == "https://getmatch.ru/vacancies/34693-mlops"
    assert abs_url("https://getmatch.ru/x") == "https://getmatch.ru/x"
    assert abs_url("") == ""
    assert abs_url(None) == ""


def test_apply_form_basic():
    offer = {"id": 34687,
             "location_requirements": [{"location_id": "moscow__mo__russia", "format": "office"}]}
    me = {"first_name": "Семен", "last_name": "Рябов", "salary_from": 200000, "salary_currency": "RUB"}
    f = apply_form(offer, me)
    assert f == {"first_name": "Семен", "last_name": "Рябов", "salary_currency": "RUB",
                 "salary_from": "200000", "location_id": "moscow__mo__russia",
                 "web_apply_source": "offers_list"}


def test_apply_form_defaults():
    # нет локаций и валюты — безопасные дефолты, salary_from строкой
    f = apply_form({"id": 1}, {"first_name": "A", "last_name": "B", "salary_from": None})
    assert f["location_id"] == ""
    assert f["salary_currency"] == "RUB"
    assert f["salary_from"] == "0"


def test_profile_filters():
    me = {"specializations": ["data_science", "python"], "locations": ["moscow", "remote"],
          "salary_from": 200000}
    assert profile_filters(me) == {"sp": ["data_science", "python"],
                                   "l": ["moscow", "remote"], "sa": 200000}
    assert profile_filters({}) == {}
