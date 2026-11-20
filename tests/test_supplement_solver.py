"""Unit tests for the minimum-cardinality supplementary MIP selector."""

from app.weighting import select_minimum_supplement


def _hh(hid, district, band, weight):
    return {
        "household_id": hid,
        "district": district,
        "size_band": band,
        "init_weight": weight,
    }


TARGETS = [
    ("district", "D1", 6.0),
    ("district", "D2", 4.0),
    ("size_band", "S1", 5.0),
    ("size_band", "S2", 5.0),
]


def test_minimum_cardinality_prefers_fewer_heavier_households():
    # D1/S1 has a hard deficit the originals cannot reach inside their factor
    # boxes; every other group stays reachable.  Each candidate caps at 2x
    # its initial weight, so two candidates are provably insufficient while
    # three close the gap: the MIP must return exactly three, never four.
    households = [
        _hh("H1", "D1", "S1", 1.0),
        _hh("H2", "D1", "S2", 2.0),
        _hh("H3", "D2", "S1", 1.0),
        _hh("H4", "D2", "S2", 3.0),
    ]
    targets = [
        ("district", "D1", 10.0),
        ("district", "D2", 6.0),
        ("size_band", "S1", 9.0),
        ("size_band", "S2", 7.0),
    ]
    candidates = [
        _hh("C1", "D1", "S1", 1.0),
        _hh("C2", "D1", "S1", 1.0),
        _hh("C3", "D1", "S1", 1.0),
        _hh("C4", "D1", "S1", 1.0),
    ]
    res = select_minimum_supplement(households, candidates, targets)
    assert res.failure_reason is None
    assert res.feasible is True
    assert len(res.selected) == 3
    assert set(res.selected).issubset({"C1", "C2", "C3", "C4"})

    # With only two candidates the same deficit has no feasible selection.
    res_two = select_minimum_supplement(
        households, candidates[:2], targets
    )
    assert res_two.feasible is False
    assert res_two.failure_reason is None


def test_no_solution_when_candidates_cannot_cover_shortfall():
    households = [
        _hh("H1", "D1", "S1", 1.0),
        _hh("H2", "D2", "S2", 1.0),
    ]
    # Candidates live in D2/S2 only, cannot fix the D1/S1 deficit.
    candidates = [
        _hh("X1", "D2", "S2", 1.0),
        _hh("X2", "D2", "S2", 1.0),
    ]
    res = select_minimum_supplement(
        households, candidates, TARGETS
    )
    assert res.feasible is False
    assert res.failure_reason is None
    assert res.selected == []
    assert res.best_gap is not None and res.best_gap > 0
    assert res.blocking_groups  # at least one group outside its factor box
    dims = {(b["dimension"], b["group_name"]) for b in res.blocking_groups}
    assert ("district", "D1") in dims or ("size_band", "S1") in dims
    # Diagnostics carry one row per target group.
    assert len(res.group_diffs) == len(TARGETS)


def test_selected_candidate_weights_stay_inside_factor_box():
    # D1/S1 target 9 exceeds the originals' joint upper bound (8), so exactly
    # one extra D1/S1 household is mandatory.  Re-solve the merged roster with
    # the ordinary LP to prove every weight stays within 0.5x..2x initial.
    households = [
        _hh("H1", "D1", "S1", 2.0),
        _hh("H2", "D1", "S2", 2.0),
        _hh("H3", "D2", "S1", 2.0),
        _hh("H4", "D2", "S2", 2.0),
    ]
    targets = [
        ("district", "D1", 9.0),
        ("district", "D2", 4.0),
        ("size_band", "S1", 9.0),
        ("size_band", "S2", 4.0),
    ]
    candidates = [_hh("C1", "D1", "S1", 2.0), _hh("C2", "D2", "S2", 2.0)]
    res = select_minimum_supplement(households, candidates, targets)
    assert res.feasible is True
    assert res.selected == ["C1"]

    from app.weighting import solve as lp_solve

    merged = households + [candidates[0]]
    check = lp_solve(merged, targets)
    assert check.balanced is True
    for hh, weight in zip(merged, check.weights):
        assert 0.5 * hh["init_weight"] - 1e-9 <= weight
        assert weight <= 2.0 * hh["init_weight"] + 1e-9


def test_zero_candidates_needed_returns_empty_selection():
    # Roster already balances the targets -> empty selection is optimal.
    households = [
        _hh("H1", "D1", "S1", 3.0),
        _hh("H2", "D1", "S2", 3.0),
        _hh("H3", "D2", "S1", 2.0),
        _hh("H4", "D2", "S2", 2.0),
    ]
    targets = [
        ("district", "D1", 6.0),
        ("district", "D2", 4.0),
        ("size_band", "S1", 5.0),
        ("size_band", "S2", 5.0),
    ]
    candidates = [_hh("C1", "D1", "S1", 1.0)]
    res = select_minimum_supplement(households, candidates, targets)
    assert res.feasible is True
    assert res.selected == []


def test_solver_failure_reported_not_confused_with_no_solution(monkeypatch):
    import highspy

    from app import weighting as wmod

    class _BrokenHighs:
        def setOptionValue(self, *args, **kwargs):
            return highspy.HighsStatus.kOk

        def addVars(self, *args, **kwargs):
            return highspy.HighsStatus.kOk

        def changeColsIntegrality(self, *args, **kwargs):
            return highspy.HighsStatus.kOk

        def changeColsCost(self, *args, **kwargs):
            return highspy.HighsStatus.kOk

        def addRow(self, *args, **kwargs):
            return highspy.HighsStatus.kOk

        def run(self):
            return highspy.HighsStatus.kError

        def getModelStatus(self):
            return highspy.HighsModelStatus.kSolveError

        def modelStatusToString(self, status):
            return "SolveError"

    monkeypatch.setattr(wmod, "_new_highs", lambda: _BrokenHighs())
    res = wmod.select_minimum_supplement(
        [_hh("H1", "D1", "S1", 1.0)],
        [_hh("C1", "D1", "S1", 1.0)],
        TARGETS,
    )
    assert res.feasible is False
    assert res.failure_reason is not None
