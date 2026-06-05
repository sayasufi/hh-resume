from services.getmatch_api import abs_url, apply_form, profile_filters


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
