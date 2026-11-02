from hh_applicant_tool.utils import prefs


def test_explicit_hybrid_is_not_office_due_to_full_day_schedule():
    vacancy = {'work_format': [{'id': 'HYBRID'}], 'schedule': {'id': 'fullDay'}}
    formats = prefs.hh_vacancy_formats(vacancy)
    assert not prefs.format_ok(formats, {'onsite'})
    assert prefs.format_ok(formats, {'hybrid'})


def test_legacy_schedule_used_without_known_work_format():
    assert prefs.hh_vacancy_formats({'schedule': {'id': 'remote'}}) == ['remote']
    assert prefs.hh_vacancy_formats({}) == []
