"""Goal progress where less is better (pos.goals): median 28.2 h against a 24 h target is not 100 %."""

from pos import goals


def test_lower_is_better_without_baseline_is_not_done():
    g = {"baseline": None, "current": 28.2, "target_value": 24, "metric": "medián hodin do první odpovědi"}
    assert goals.lower_is_better(g)
    assert goals._measured(g) == 85
    assert goals.met(g) is False


def test_lower_is_better_reached_and_with_baseline():
    assert goals._measured({"baseline": None, "current": 20, "target_value": 24, "metric": "medián hodin"}) == 100
    g = {"baseline": 40, "current": 32, "target_value": 24, "metric": ""}
    assert goals.lower_is_better(g) and goals._measured(g) == 50 and goals.met(g) is False


def test_higher_is_better_unchanged():
    g = {"baseline": 39.1, "current": 37.3, "target_value": 50, "metric": "% nákladů na byznys"}
    assert not goals.lower_is_better(g)
    assert goals._measured(g) == 0 and goals.met(g) is False
    assert goals._measured({"baseline": 0, "current": 25, "target_value": 50, "metric": "kontakty"}) == 50
