"""End-to-end API tests for supplementary-visit sampling."""

from tests.test_api import _upload_roster, _upload_targets, _wait_finished

# Roster whose D1/S1 capacity is insufficient for the targets below.
ROSTER = [
    {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "H2", "district": "D1", "size_band": "S2", "init_weight": 2.0},
    {"household_id": "H3", "district": "D2", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "H4", "district": "D2", "size_band": "S2", "init_weight": 3.0},
]
TARGETS = [
    {"dimension": "district", "group_name": "D1", "target_count": 10.0},
    {"dimension": "district", "group_name": "D2", "target_count": 6.0},
    {"dimension": "size_band", "group_name": "S1", "target_count": 9.0},
    {"dimension": "size_band", "group_name": "S2", "target_count": 7.0},
]
# D1/S1 needs +8 weighted units; every candidate caps at 2x its initial
# weight, so at least three candidates must be selected (four are offered).
CANDIDATES = [
    {"household_id": "C1", "district": "D1", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "C2", "district": "D1", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "C3", "district": "D1", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "C4", "district": "D1", "size_band": "S1", "init_weight": 1.0},
]


def _unbalanced_job(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    client.post("/weighting/jobs")
    final = _wait_finished(client)
    assert final["status"] == "unbalanced"
    return final["job_id"]


def _create_plan(client, job_id, candidates):
    return client.post(
        "/supplement/plans",
        json={"job_id": job_id, "candidates": candidates},
    )


def test_plan_creation_rejects_non_unbalanced_job(client):
    job_id = _unbalanced_job(client)

    # Unknown job id.
    resp = _create_plan(client, 9999, CANDIDATES[:1])
    assert resp.status_code == 404
    assert resp.json()["error"] == "job_not_found"

    # A balanced job on a fresh version pair cannot drive supplementation.
    balanced_targets = [
        {"dimension": "district", "group_name": "D1", "target_count": 3.0},
        {"dimension": "district", "group_name": "D2", "target_count": 4.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 2.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 5.0},
    ]
    _upload_targets(client, balanced_targets)
    client.post("/weighting/jobs")
    balanced = _wait_finished(client)
    assert balanced["status"] == "balanced"
    resp = _create_plan(client, balanced["job_id"], CANDIDATES[:1])
    assert resp.status_code == 409
    assert resp.json()["error"] == "job_not_unbalanced"
    assert job_id  # original unbalanced job still exists in history


def test_candidate_validation_rules(client):
    job_id = _unbalanced_job(client)

    # More than 30 candidates.
    too_many = [
        {"household_id": f"X{i}", "district": "D1",
         "size_band": "S1", "init_weight": 1.0}
        for i in range(31)
    ]
    resp = _create_plan(client, job_id, too_many)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_payload"

    # Duplicate household ids inside the candidate list.
    resp = _create_plan(client, job_id, CANDIDATES[:1] + CANDIDATES[:1])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_payload"

    # Already sampled in the roster.
    resp = _create_plan(
        client,
        job_id,
        [{"household_id": "H1", "district": "D1",
          "size_band": "S1", "init_weight": 1.0}],
    )
    assert resp.status_code == 400
    assert resp.json()["details"]["already_sampled"] == ["H1"]

    # Non-positive initial weight.
    resp = _create_plan(
        client,
        job_id,
        [{"household_id": "Z1", "district": "D1",
          "size_band": "S1", "init_weight": 0}],
    )
    assert resp.status_code == 400

    # Candidate group missing from the pinned target table.
    resp = _create_plan(
        client,
        job_id,
        [{"household_id": "Z2", "district": "D9",
          "size_band": "S1", "init_weight": 1.0}],
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "missing_group_targets"
    missing = resp.json()["details"]["missing_group_targets"]
    assert {"dimension": "district", "group_name": "D9"} in missing


def test_solved_plan_returns_minimum_selection_and_versions(client):
    job_id = _unbalanced_job(client)
    resp = _create_plan(client, job_id, CANDIDATES)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "solved"
    assert body["plan_id"] > 0
    assert body["respondent_version"] == 1
    assert body["target_version"] == 1
    assert body["selected_count"] == 3
    assert len(body["selected_household_ids"]) == 3
    assert set(body["selected_household_ids"]).issubset(
        {"C1", "C2", "C3", "C4"}
    )

    detail = client.get(f"/supplement/plans/{body['plan_id']}").json()
    assert detail["status"] == "solved"
    assert len(detail["candidates"]) == 4
    selected_rows = [c for c in detail["candidates"] if c["is_selected"]]
    assert len(selected_rows) == 3


def test_no_solution_plan_explains_reason_and_keeps_history(client):
    job_id = _unbalanced_job(client)
    resp = _create_plan(
        client,
        job_id,
        [{"household_id": "X1", "district": "D2",
          "size_band": "S2", "init_weight": 1.0}],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "no_solution"
    assert body["selected_household_ids"] == []
    assert body["no_solution_reason"]
    assert body["best_gap"] > 0
    assert body["blocking_groups"]
    assert len(body["diagnostics"]) == 4

    detail = client.get(f"/supplement/plans/{body['plan_id']}").json()
    assert detail["status"] == "no_solution"
    assert detail["diagnostics"]
    # The no-solution plan cannot be completed.
    resp = client.post(
        f"/supplement/plans/{body['plan_id']}/completion",
        json={"responded_household_ids": ["X1"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "plan_not_actionable"


def test_successful_completion_merges_roster_and_publishes_weights(client):
    job_id = _unbalanced_job(client)
    plan = _create_plan(client, job_id, CANDIDATES).json()
    selected = plan["selected_household_ids"]

    resp = client.post(
        f"/supplement/plans/{plan['plan_id']}/completion",
        json={"responded_household_ids": selected},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["respondent_version"] == 2
    assert body["target_version"] == 1
    assert body["merged_household_count"] == 7
    assert set(body["added_household_ids"]) == set(selected)

    # New roster and official weights take effect together.
    weights = client.get("/weighting/weights").json()
    assert weights["respondent_version"] == 2
    assert weights["target_version"] == 1
    assert weights["total_count"] == 7
    by_id = {r["household_id"]: r["weight"] for r in weights["weights"]}
    all_households = {h["household_id"]: h for h in ROSTER + CANDIDATES}
    sums: dict[str, float] = {}
    for hid, weight in by_id.items():
        hh = all_households[hid]
        assert 0.5 * hh["init_weight"] - 1e-9 <= weight
        assert weight <= 2.0 * hh["init_weight"] + 1e-9
        sums[hh["district"]] = sums.get(hh["district"], 0.0) + weight
        sums[hh["size_band"]] = sums.get(hh["size_band"], 0.0) + weight
    for target in TARGETS:
        assert abs(sums[target["group_name"]] - target["target_count"]) < 1e-5

    # Status immediately reflects the balanced merged pair.
    status = client.get("/weighting/status").json()
    assert status["status"] == "balanced"
    assert status["respondent_version"] == 2

    # Plan cannot be completed twice.
    again = client.post(
        f"/supplement/plans/{plan['plan_id']}/completion",
        json={"responded_household_ids": selected},
    )
    assert again.status_code == 409
    assert again.json()["error"] == "plan_not_actionable"


def test_response_mismatch_leaves_live_roster_untouched(client):
    job_id = _unbalanced_job(client)
    plan = _create_plan(client, job_id, CANDIDATES).json()
    selected = plan["selected_household_ids"]

    before = client.get("/tables/versions").json()

    for payload in (
        selected[:-1],                              # missing one
        selected + ["C3"],                          # unexpected extra
        selected + [selected[0]],                   # duplicated id
    ):
        resp = client.post(
            f"/supplement/plans/{plan['plan_id']}/completion",
            json={"responded_household_ids": payload},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "response_mismatch"

    # Live roster pointer unchanged and still unbalanced.
    after = client.get("/tables/versions").json()
    assert after["respondents"]["version"] == before["respondents"]["version"]
    status = client.get("/weighting/status").json()
    assert status["status"] == "unbalanced"

    # Correct submission still succeeds afterwards.
    ok = client.post(
        f"/supplement/plans/{plan['plan_id']}/completion",
        json={"responded_household_ids": selected},
    )
    assert ok.status_code == 200


def test_table_swapped_between_solve_and_completion_is_rejected(client):
    job_id = _unbalanced_job(client)
    plan = _create_plan(client, job_id, CANDIDATES).json()
    selected = plan["selected_household_ids"]

    # Swap the roster after the plan was produced.
    _upload_roster(
        client,
        [dict(r, init_weight=r["init_weight"] + 0.25) for r in ROSTER],
    )
    resp = client.post(
        f"/supplement/plans/{plan['plan_id']}/completion",
        json={"responded_household_ids": selected},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "source_version_changed"

    # Live data remains whatever the current version pair holds (pending here).
    status = client.get("/weighting/status").json()
    assert status["respondent_version"] == 2
    assert status["status"] == "pending"


def test_table_swapped_during_plan_solve_is_rejected(client, monkeypatch):
    from app import service as service_mod

    job_id = _unbalanced_job(client)
    original = service_mod.weighting.select_minimum_supplement

    def swap_then_solve(households, candidates, target_rows):
        result = original(households, candidates, target_rows)
        # Simulate a replacement landing while the MIP was running.
        service_mod.WeightingService.replace_respondents(
            client.app.state._injected_service,
            [dict(r, init_weight=r["init_weight"] + 0.25) for r in ROSTER],
        )
        return result

    monkeypatch.setattr(
        service_mod.weighting,
        "select_minimum_supplement",
        swap_then_solve,
    )
    resp = _create_plan(client, job_id, CANDIDATES)
    assert resp.status_code == 409
    assert resp.json()["error"] == "source_version_changed"

    # No plan row was persisted for the stale solve.
    listing = client.get("/supplement/plans").json()
    assert listing["total_count"] == 0


def test_solver_failure_keeps_live_data_and_returns_500(client, monkeypatch):
    from app import service as service_mod
    from app.weighting import SupplementResult

    job_id = _unbalanced_job(client)

    def failing_selector(households, candidates, target_rows):
        return SupplementResult(
            feasible=False, failure_reason="模拟求解器内部错误"
        )

    monkeypatch.setattr(
        service_mod.weighting,
        "select_minimum_supplement",
        failing_selector,
    )
    resp = _create_plan(client, job_id, CANDIDATES)
    assert resp.status_code == 500
    assert resp.json()["error"] == "supplement_solve_failed"

    # No plan stored; roster and unbalanced supply stay available.
    assert client.get("/supplement/plans").json()["total_count"] == 0
    diffs = client.get("/weighting/differences")
    assert diffs.status_code == 200
    assert diffs.json()["status"] == "unbalanced"


def test_merge_compute_failure_rolls_back_and_keeps_supply(client, monkeypatch):
    from app import service as service_mod

    job_id = _unbalanced_job(client)
    plan = _create_plan(client, job_id, CANDIDATES).json()
    selected = plan["selected_household_ids"]
    versions_before = client.get("/tables/versions").json()

    def failing_solve(households, target_rows):
        raise RuntimeError("模拟重算阶段崩溃")

    monkeypatch.setattr(service_mod.weighting, "solve", failing_solve)
    resp = client.post(
        f"/supplement/plans/{plan['plan_id']}/completion",
        json={"responded_household_ids": selected},
    )
    assert resp.status_code == 500
    assert resp.json()["error"] == "supplement_merge_failed"

    # Roster pointer and the previous supply are preserved.
    versions_after = client.get("/tables/versions").json()
    assert (
        versions_after["respondents"]["version"]
        == versions_before["respondents"]["version"]
    )
    diffs = client.get("/weighting/differences").json()
    assert diffs["status"] == "unbalanced"
    plan_detail = client.get(f"/supplement/plans/{plan['plan_id']}").json()
    assert plan_detail["status"] == "solved"


def test_unbalanced_merge_result_rolls_back(client, monkeypatch):
    from app import service as service_mod
    from app.weighting import SolveResult

    job_id = _unbalanced_job(client)
    plan = _create_plan(client, job_id, CANDIDATES).json()
    selected = plan["selected_household_ids"]

    def unbalanced_solve(households, target_rows):
        return SolveResult(
            weights=[1.0] * len(households),
            balanced=False,
            gap=3.5,
            group_diffs=[],
        )

    monkeypatch.setattr(service_mod.weighting, "solve", unbalanced_solve)
    resp = client.post(
        f"/supplement/plans/{plan['plan_id']}/completion",
        json={"responded_household_ids": selected},
    )
    assert resp.status_code == 500
    assert resp.json()["error"] == "supplement_merge_failed"
    assert client.get("/weighting/status").json()["status"] == "unbalanced"


def test_unknown_plan_and_plan_listing(client):
    resp = client.get("/supplement/plans/4242")
    assert resp.status_code == 404
    assert resp.json()["error"] == "plan_not_found"

    resp = client.post(
        "/supplement/plans/4242/completion",
        json={"responded_household_ids": ["C1"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "plan_not_found"

    job_id = _unbalanced_job(client)
    _create_plan(client, job_id, CANDIDATES)
    listing = client.get("/supplement/plans?limit=1&offset=0").json()
    assert listing["total_count"] == 1
    assert len(listing["plans"]) == 1
    assert listing["plans"][0]["status_label"]


def test_stale_job_version_rejected_before_solving(client):
    job_id = _unbalanced_job(client)
    # Replace the roster: the historical unbalanced job's pair is no longer
    # current, so supplementation against it must be refused up front.
    _upload_roster(
        client,
        [dict(r, init_weight=r["init_weight"] + 0.25) for r in ROSTER],
    )
    resp = _create_plan(client, job_id, CANDIDATES)
    assert resp.status_code == 409
    assert resp.json()["error"] == "source_version_changed"
    assert client.get("/supplement/plans").json()["total_count"] == 0


def test_merged_roster_supports_normal_replacement_afterwards(client):
    job_id = _unbalanced_job(client)
    plan = _create_plan(client, job_id, CANDIDATES).json()
    selected = plan["selected_household_ids"]
    completed = client.post(
        f"/supplement/plans/{plan['plan_id']}/completion",
        json={"responded_household_ids": selected},
    ).json()
    assert completed["respondent_version"] == 2

    # The merged snapshot behaves like an ordinary roster version: a fresh
    # whole-table replacement and re-weighting run both work.
    new_roster = [
        {"household_id": "N1", "district": "D1",
         "size_band": "S1", "init_weight": 2.0},
        {"household_id": "N2", "district": "D2",
         "size_band": "S2", "init_weight": 2.0},
    ]
    resp = _upload_roster(client, new_roster)
    assert resp.status_code == 200
    assert resp.json()["version"] == 3
    new_targets = [
        {"dimension": "district", "group_name": "D1", "target_count": 2.0},
        {"dimension": "district", "group_name": "D2", "target_count": 2.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 2.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 2.0},
    ]
    _upload_targets(client, new_targets)
    client.post("/weighting/jobs")
    final = _wait_finished(client)
    assert final["status"] == "balanced"
    weights = client.get("/weighting/weights").json()
    assert weights["respondent_version"] == 3
    assert weights["target_version"] == 2

    # The completed plan keeps its audit trail.
    detail = client.get(f"/supplement/plans/{plan['plan_id']}").json()
    assert detail["status"] == "completed"
    assert detail["merged_respondent_version"] == 2
    assert detail["merged_job_id"] == completed["job_id"]
