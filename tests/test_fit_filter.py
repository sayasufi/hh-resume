"""Детерминированная часть фильтра «по профилю»: чужая экосистема в названии вакансии."""
from hh_applicant_tool.operations.apply_similar import FOREIGN_STACK


def _hit(title):
    m = FOREIGN_STACK.search(title)
    return m.group(0).lower() if m else None


def test_foreign_stack_in_title():
    assert _hit("Программист 1С") == "1с"
    assert _hit("PHP Developer (Laravel)") == "php"
    assert _hit("Java Data Engineer") == "java"
    assert _hit("Middle C# / .NET") == "c#"
    assert _hit("iOS-разработчик") == "ios"
    assert _hit("DBA Специалист по работе с СУБД MS SQL") == "dba"
    assert _hit("Консультант SAP") == "sap"


def test_no_false_positives():
    for title in ("Frontend (JavaScript, React)", "Системный аналитик", "Data Engineer",
                  "Senior Data Scientist", "Python Developer", "Сапёр", "Golang-разработчик"):
        assert _hit(title) is None, title


def test_profile_stacks_distinguish_javascript():
    profile = "Навыки: Python, JavaScript, TypeScript"
    assert {m.lower() for m in FOREIGN_STACK.findall(profile)} == set()
