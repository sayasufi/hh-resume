from types import SimpleNamespace

from services import getmatch_apply as g


def _cb(text, data):
    return SimpleNamespace(text=text, data=data)


def _btn(text):
    return SimpleNamespace(text=text, data=None)


def test_apply_callback_id():
    assert g.apply_callback_id(_cb("💥 Откликнуться в боте", b"application__send__32950")) == "32950"
    assert g.apply_callback_id(_btn("Описание")) is None
    assert g.apply_callback_id(_cb("x", b"other_data")) is None


def test_find_apply_button():
    rows = [[_btn("Описание"), _btn("Вакансии (177)")],
            [_cb("💥 Откликнуться в боте", b"application__send__34696")]]
    btn = g.find_apply_button(rows)
    assert g.apply_callback_id(btn) == "34696"
    # запасной матч по тексту, если callback не нашёлся
    assert g.find_apply_button([[_btn("💥 Откликнуться в боте")]]).text == "💥 Откликнуться в боте"
    assert g.find_apply_button([[_btn("Описание")]]) is None
    assert g.find_apply_button([]) is None


def test_is_profile_ok():
    assert g.is_profile_ok("У вас подтверждённый профиль 🤘") is True
    assert g.is_profile_ok("С ним можно откликаться на вакансии в один клик") is True
    assert g.is_profile_ok("Заполните профиль: пришлите ссылку на резюме") is False
    assert g.is_profile_ok("") is False


def test_applied_ok():
    assert g.applied_ok("Отклик отправлен работодателю") is True
    assert g.applied_ok("Отклик уже отправлен работодателю") is True
    assert g.applied_ok("Что-то пошло не так") is False


def test_is_expired():
    assert g.is_expired("Вакансия уже неактуальна") is True
    assert g.is_expired("Вакансия закрыта") is True
    assert g.is_expired("Отклик отправлен") is False
