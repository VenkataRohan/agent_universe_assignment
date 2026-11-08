"""Ticket 03: cohorts and history baselines. Everything goes through Seam 1 and the query interface.

The real simulator covers what it can produce. A scripted world behind the same HTTP API covers
what it can't (groups under 10% of traffic) and lets expected values be worked out by hand.
"""
from __future__ import annotations

import re
from collections import Counter

import pytest

from controller import queries
from controller.__main__ import main as cli_main
from controller.loop import Controller

from .harness import all_events, features, scripted_client, troubled_client


def test_baselines_come_from_history_alone(world, db_path):
    client = world(days=1, history_days=2)
    Controller(client, db_path).run()  # the live day is in the store too, and must not count

    history: dict[str, Counter] = {}
    for ev in all_events(client):
        if ev["type"] == "call" and ev["ts"] < "2027-01-04":
            c = history.setdefault(ev["workflow_id"], Counter())
            c["dials"] += 1
            c["connected"] += ev["connected"]
            c["engaged"] += ev["engaged"]
            c["flags"] += bool(ev["qa"] and ev["qa"]["hallucination_flag"])

    got = {w["workflow_id"]: w["baselines"] for w in queries.status(db_path)["workflows"]}
    assert set(got) == set(history) == {"nw_renewal", "nw_kyc", "ks_cancel"}
    for wf, c in history.items():
        assert got[wf]["pickup_rate"] == pytest.approx(c["connected"] / c["dials"])
        assert got[wf]["engagement_rate"] == pytest.approx(c["engaged"] / c["connected"])
        assert got[wf]["hallucination_flag_rate"] == pytest.approx(c["flags"] / c["connected"])


def _partition(db_path, workflow_id="wf") -> dict:
    return queries.status(db_path, workflow_id)["workflows"][0]["partition"]


def _start(client, db_path) -> None:
    c = Controller(client, db_path)
    c.start()
    c.close()


def test_partition_is_the_feature_whose_groups_differ_most_on_the_primary_kpi(db_path):
    # Engagement by age is 30% vs 70% (40 points); by language it is 41% vs 60% (19 points).
    # Connected-only calls: not-connected ones are outside the engagement KPI.
    calls = [
        (features(age_band="60+", lang="hi"), 10, 1, True, True, 27),
        (features(age_band="60+", lang="hi"), 10, 1, True, False, 63),
        (features(age_band="18-30", lang="hi"), 10, 1, True, True, 18),
        (features(age_band="18-30", lang="hi"), 10, 1, True, False, 2),
        (features(age_band="18-30", lang="ta"), 10, 1, True, True, 45),
        (features(age_band="18-30", lang="ta"), 10, 1, True, False, 25),
        (features(age_band="60+", lang="ta"), 10, 1, True, True, 3),
        (features(age_band="60+", lang="ta"), 10, 1, True, False, 7),
    ]
    _start(scripted_client("engagement_rate", calls), db_path)

    p = _partition(db_path)
    assert p["feature"] == "age_band"
    assert p["spread"] == pytest.approx(0.70 - 0.30)


def _connected_calls(feats: dict, engaged: int, total: int, hour: int = 10, attempt: int = 1) -> list:
    """`total` connected calls of one kind at one hour, `engaged` of them engaged."""
    return [(feats, hour, attempt, True, True, engaged), (feats, hour, attempt, True, False, total - engaged)]


def test_largest_group_is_default_and_the_others_are_named_cohorts_with_stable_ids(db_path):
    calls = (_connected_calls(features(age_band="31-45"), 50, 100) + _connected_calls(features(age_band="60+"), 20, 50)
             + _connected_calls(features(age_band="18-30"), 30, 40))
    _start(scripted_client("engagement_rate", calls), db_path)

    assert _partition(db_path)["cohorts"] == [
        {"id": "age_band_18_30", "name": "age 18-30", "where": {"age_band": ["18-30"]}},
        {"id": "age_band_60plus", "name": "age 60+", "where": {"age_band": ["60+"]}},
        {"id": "default", "name": "everyone else (age 31-45)", "where": {"age_band": ["31-45"]}},
    ]


@pytest.mark.parametrize("feature, values, ids_and_names", [
    ("lang", ["hi", "ta"], {"lang_ta": "Tamil speakers"}),
    ("city_tier", ["1", "2"], {"city_tier_2": "city tier 2"}),
    ("value_band", ["low", "high"], {"value_band_high": "high value"}),
])
def test_cohorts_are_named_in_plain_language(db_path, feature, values, ids_and_names):
    big, small = values
    calls = _connected_calls(features(**{feature: big}), 50, 100) + _connected_calls(features(**{feature: small}), 10, 40)
    _start(scripted_client("engagement_rate", calls), db_path)

    named = {c["id"]: c["name"] for c in _partition(db_path)["cohorts"] if c["id"] != "default"}
    assert named == ids_and_names


def test_a_workflow_with_no_varying_feature_has_only_default(db_path):
    _start(scripted_client("engagement_rate", _connected_calls(features(), 10, 20)), db_path)

    assert _partition(db_path) == {"feature": None, "spread": None, "cohorts": [
        {"id": "default", "name": "everyone", "where": {}}]}


def test_groups_under_ten_percent_of_traffic_merge_into_the_group_with_the_nearest_rate(db_path):
    # 100 connects. 60+ is 8% at 62.5% engagement, nearer 18-30 (68.75%) than 31-45 (20%), so they merge.
    calls = (_connected_calls(features(age_band="31-45"), 12, 60) + _connected_calls(features(age_band="18-30"), 22, 32)
             + _connected_calls(features(age_band="60+"), 5, 8))
    _start(scripted_client("engagement_rate", calls), db_path)

    p = _partition(db_path)
    assert p["cohorts"] == [
        {"id": "age_band_18_30_60plus", "name": "age 18-30 or 60+", "where": {"age_band": ["18-30", "60+"]}},
        {"id": "default", "name": "everyone else (age 31-45)", "where": {"age_band": ["31-45"]}},
    ]
    # Merged group: 27 engaged of 40 connects = 67.5%. Default: 12 of 60 = 20%.
    assert p["spread"] == pytest.approx(0.675 - 0.20)
    assert queries.cohort_kpis(db_path, "wf")["age_band_18_30_60plus"] == {
        "dials": 40, "connected": 40, "engaged": 27, "hallucination_flags": 0}
    assert [(r["cohort_id"], r["traffic"], r["successes"]) for r in queries.cohort_hour_rates(db_path, "wf")] == [
        ("age_band_18_30_60plus", 40, 27), ("default", 60, 12)]


def test_a_group_of_exactly_ten_percent_is_kept(db_path):
    calls = _connected_calls(features(age_band="31-45"), 45, 90) + _connected_calls(features(age_band="60+"), 1, 10)
    _start(scripted_client("engagement_rate", calls), db_path)

    assert [c["id"] for c in _partition(db_path)["cohorts"]] == ["age_band_60plus", "default"]


def test_a_merged_group_still_under_ten_percent_keeps_merging(db_path):
    # ta (6%, 83%) merges with kn or te (7%, 86% each; kn wins the tie by name) to give 13%. te is
    # still under the line and is nearer that merged group (85%) than hi (50%), so it joins it.
    calls = (_connected_calls(features(lang="hi"), 40, 80) + _connected_calls(features(lang="ta"), 5, 6)
             + _connected_calls(features(lang="kn"), 6, 7) + _connected_calls(features(lang="te"), 6, 7))
    _start(scripted_client("engagement_rate", calls), db_path)

    p = _partition(db_path)
    assert p["cohorts"][0] == {"id": "lang_kn_ta_te", "name": "Kannada or Tamil or Telugu speakers",
                               "where": {"lang": ["kn", "ta", "te"]}}
    assert p["spread"] == pytest.approx(17 / 20 - 40 / 80)


def test_on_a_pickup_workflow_traffic_means_dials_when_deciding_what_to_merge(db_path):
    # 60+ is 15% of dials but only 1 of 86 connects: under the line on connects, kept on dials.
    calls = [(features(age_band="31-45"), 10, 1, True, False, 85), (features(age_band="60+"), 10, 1, True, False, 1),
             (features(age_band="60+"), 10, 1, False, False, 14)]
    _start(scripted_client("pickup_rate", calls), db_path)

    assert [c["id"] for c in _partition(db_path)["cohorts"]] == ["age_band_60plus", "default"]


def test_features_with_an_equal_spread_tie_break_on_feature_name(db_path):
    # Age and language split the same leads, so their spreads are identical; age_band sorts first.
    calls = (_connected_calls(features(age_band="31-45", lang="hi"), 30, 60)
             + _connected_calls(features(age_band="60+", lang="ta"), 10, 40))
    _start(scripted_client("engagement_rate", calls), db_path)

    assert _partition(db_path)["feature"] == "age_band"


def test_a_feature_that_collapses_into_one_group_is_not_a_candidate(db_path):
    calls = _connected_calls(features(lang="hi"), 44, 95) + _connected_calls(features(lang="ta"), 4, 5)
    _start(scripted_client("engagement_rate", calls), db_path)

    assert _partition(db_path)["feature"] is None


def test_features_compete_on_their_spread_after_merging(db_path):
    # Unmerged, age would win on a single 100%-engaged 60+ call (spread 51 points). Merged, age's
    # spread is about 1 point, so language's 20 points (40% hi vs 60% ta) wins.
    calls = (_connected_calls(features(age_band="31-45", lang="hi"), 20, 50) + _connected_calls(features(age_band="31-45", lang="ta"), 30, 50)
             + _connected_calls(features(age_band="46-60", lang="hi"), 19, 49) + _connected_calls(features(age_band="46-60", lang="ta"), 30, 50)
             + _connected_calls(features(age_band="60+", lang="hi"), 1, 1))
    _start(scripted_client("engagement_rate", calls), db_path)

    p = _partition(db_path)
    assert p["feature"] == "lang"
    assert p["spread"] == pytest.approx(0.60 - 0.40)


def _leads_of(client) -> dict[str, dict]:
    return {ev["lead_id"]: ev for ev in all_events(client) if ev["type"] == "lead"}


def _member_of(cohorts: list[dict], feats: dict) -> str:
    """Independent of the controller: the simulator's documented first-match rule."""
    for c in cohorts:
        if c["id"] != "default" and all(feats[f] in vs for f, vs in c["where"].items()):
            return c["id"]
    return "default"


PUBLIC_01 = ("nw_renewal", "nw_kyc", "ks_cancel")


def test_two_independent_runs_choose_the_identical_partition(world, tmp_path):
    def analysed(run: str) -> list[dict]:
        db = tmp_path / f"{run}.db"
        _start(world(days=1, history_days=3), db)
        return [{"id": w["workflow_id"], "partition": w["partition"]} for w in queries.status(db)["workflows"]]

    first, second = analysed("a"), analysed("b")
    assert first == second
    assert [w["id"] for w in first] == sorted(PUBLIC_01)
    assert all(w["partition"]["feature"] for w in first)


def test_partition_does_not_move_with_live_data_or_a_restart(world, tmp_path):
    fresh = tmp_path / "fresh.db"
    _start(world(days=1, history_days=3), fresh)

    resumed = tmp_path / "resumed.db"
    client = world(days=1, history_days=3)
    c = Controller(client, resumed)
    for _ in range(4 * 15):  # to D0 15:00, with plenty of live calls in the store
        c.step()
    c.close()
    Controller(client, resumed).run()

    assert queries.status(resumed)["workflows"] == queries.status(fresh)["workflows"]


def test_cohorts_are_disjoint_and_with_default_cover_every_feature_cell_once(world, db_path):
    client = world(days=1, history_days=2)
    Controller(client, db_path).run()

    seen: dict[str, set[tuple]] = {}
    for lead in _leads_of(client).values():
        seen.setdefault(lead["workflow_id"], set()).add(tuple(sorted(lead["features"].items())))

    for wf in PUBLIC_01:
        cohorts = _partition(db_path, wf)["cohorts"]
        cells = queries.cohort_cells(db_path, wf)
        listed = [tuple(sorted(feats.items())) for members in cells.values() for feats in members]
        assert len(listed) == len(set(listed)), "a cell is in two cohorts"
        assert set(listed) == seen[wf], "some cell is uncovered"
        for cohort_id, members in cells.items():
            assert all(_member_of(cohorts, feats) == cohort_id for feats in members)


def test_a_cohorts_kpis_summed_from_cells_equal_a_recount_of_its_exposure_log(world, db_path):
    client = world(days=1, history_days=1)
    Controller(client, db_path).run()
    leads = _leads_of(client)

    for wf in PUBLIC_01:
        cohorts = _partition(db_path, wf)["cohorts"]
        recount: dict[str, Counter] = {c["id"]: Counter() for c in cohorts}
        for lead_id, lead in leads.items():
            if lead["workflow_id"] != wf:
                continue
            c = recount[_member_of(cohorts, lead["features"])]
            for e in queries.exposures(db_path, lead_id):
                c["dials"] += 1
                c["connected"] += e["connected"]
                c["engaged"] += e["engaged"]
                c["hallucination_flags"] += bool(e["hallucination_flag"])
        assert all(c["dials"] > 0 for c in recount.values())
        assert queries.cohort_kpis(db_path, wf) == {k: dict(v) for k, v in recount.items()}


def test_hourly_rates_adjust_for_attempt_number_and_can_reverse_the_raw_ranking(db_path):
    # Attempt 1 engages 120/200 = 60%, attempt 3+ engages 100/400 = 25%; overall 220/600.
    # 09h: 80/200 raw 40%, expected 100*.6 + 100*.25 = 85 -> 80/85 * 220/600 = 34.51%
    # 10h: 140/400 raw 35%, expected 100*.6 + 300*.25 = 135 -> 140/135 * 220/600 = 38.02%
    calls = (_connected_calls(features(), 70, 100, hour=9, attempt=1) + _connected_calls(features(), 10, 100, hour=9, attempt=3)
             + _connected_calls(features(), 50, 100, hour=10, attempt=1) + _connected_calls(features(), 90, 300, hour=10, attempt=4))
    _start(scripted_client("engagement_rate", calls), db_path)

    rates = queries.cohort_hour_rates(db_path, "wf")
    assert [(r["cohort_id"], r["hour"], r["traffic"], r["successes"]) for r in rates] == [
        ("default", 9, 200, 80), ("default", 10, 400, 140)]
    assert rates[0]["rate"] == pytest.approx(0.40)
    assert rates[1]["rate"] == pytest.approx(0.35)
    assert rates[0]["adjusted_rate"] == pytest.approx(80 / 85 * 220 / 600)
    assert rates[1]["adjusted_rate"] == pytest.approx(140 / 135 * 220 / 600)


def test_an_hour_carrying_only_late_attempts_is_not_penalised_for_them(db_path):
    # Every attempt band performs exactly at its own norm, so both hours adjust to the overall 40%.
    calls = _connected_calls(features(), 60, 100, hour=9, attempt=1) + _connected_calls(features(), 20, 100, hour=10, attempt=3)
    _start(scripted_client("engagement_rate", calls), db_path)

    assert [round(r["adjusted_rate"], 6) for r in queries.cohort_hour_rates(db_path, "wf")] == [0.4, 0.4]


def test_an_adjusted_rate_never_exceeds_one(db_path):
    # 09h is all attempt 1, which engages 10/100 overall, yet 09h itself engaged 10/10: ten times
    # its expected count, against a cohort rate of 1010/1100. Uncapped that is 918%.
    calls = (_connected_calls(features(), 10, 10, hour=9, attempt=1) + _connected_calls(features(), 0, 90, hour=10, attempt=1)
             + _connected_calls(features(), 1000, 1000, hour=10, attempt=3))
    _start(scripted_client("engagement_rate", calls), db_path)

    assert queries.cohort_hour_rates(db_path, "wf")[0]["adjusted_rate"] == 1.0


def test_hourly_rates_use_the_pickup_kpi_on_pickup_primary_workflows_and_every_cohort(db_path):
    # Pickup primary: every dial counts, connected or not. 60+ picks up 1 of 4; 31-45 picks up 3 of 6.
    calls = [(features(age_band="60+"), 11, 1, True, False, 1), (features(age_band="60+"), 11, 1, False, False, 3),
             (features(age_band="31-45"), 11, 1, True, True, 3), (features(age_band="31-45"), 11, 1, False, False, 3)]
    _start(scripted_client("pickup_rate", calls), db_path)

    assert [(r["cohort_id"], r["traffic"], r["successes"]) for r in queries.cohort_hour_rates(db_path, "wf")] == [
        ("age_band_60plus", 4, 1), ("default", 6, 3)]


def test_hourly_rates_on_real_history_cover_each_cohort_and_ignore_live_calls(world, db_path):
    client = world(days=1, history_days=2)
    Controller(client, db_path).run()
    leads = _leads_of(client)

    for wf, (trials_of, success_of) in {"nw_renewal": ("connected", "engaged"), "nw_kyc": (None, "connected")}.items():
        cohorts = _partition(db_path, wf)["cohorts"]
        recount: Counter = Counter()
        for ev in all_events(client):
            if ev["type"] == "call" and ev["workflow_id"] == wf and ev["ts"] < "2027-01-04"\
                    and (trials_of is None or ev[trials_of]):
                key = (_member_of(cohorts, leads[ev["lead_id"]]["features"]), int(ev["ts"][11:13]))
                recount[(*key, "traffic")] += 1
                recount[(*key, "successes")] += ev[success_of]

        rates = queries.cohort_hour_rates(db_path, wf)
        got = {(r["cohort_id"], r["hour"], k): r[k] for r in rates for k in ("traffic", "successes") if r[k]}
        assert got == dict(+recount)
        assert {r["cohort_id"] for r in rates} == {c["id"] for c in cohorts}


def _apply_through_approval(client, controller, wf: str, cohorts: list[dict]) -> int:
    """Propose `cohorts` as the workflow's cohorts, wait for approval, apply. Returns the new version."""
    for attempt in range(3):
        cur = client.get(f"/workflows/{wf}/config").json()
        cfg = {**cur["config"], "cohorts": cohorts}
        r = client.post("/approvals", json={
            "proposal_id": f"t-{wf}-{attempt}", "workflow_id": wf, "base_version": cur["version"],
            "proposed_config": cfg, "change_summary": "write cohorts", "diff": {"cohorts": cohorts},
            "affected_leads": 1, "dry_run": {"x": 1}, "impact_score": 0.1, "rationale": "r"})
        assert r.status_code == 200, r.text
        approval_id = r.json()["approval_id"]
        while (st := client.get(f"/approvals/{approval_id}").json()["status"]) == "pending":
            controller.step()
        if st == "approved":
            applied = client.post(f"/workflows/{wf}/apply", json={"approval_id": approval_id})
            assert applied.status_code == 200, applied.text
            return applied.json()["version"]
    raise AssertionError(f"{wf}: approver refused three times")


def test_materialised_cohorts_are_accepted_and_call_events_carry_the_same_ids(world, db_path):
    client = world(days=2, history_days=2)
    quiet, trouble = troubled_client(client)
    trouble.no_experiments = True  # this test's own request needs the config to itself
    c = Controller(quiet, db_path)
    for _ in range(4 * 10):  # D0 10:00, inside approver hours
        c.step()

    # ks_cancel gets its whole partition; nw_renewal only the one cohort a table would need.
    full = queries.config_cohorts(db_path, "ks_cancel")
    assert len(full) >= 2 and all(set(x) == {"id", "where"} and x["id"] != "default" for x in full)
    one = queries.config_cohorts(db_path, "nw_renewal", only=[_partition(db_path, "nw_renewal")["cohorts"][0]["id"]])
    assert len(one) == 1

    versions = {"ks_cancel": _apply_through_approval(client, c, "ks_cancel", full),
                "nw_renewal": _apply_through_approval(client, c, "nw_renewal", one)}
    c.run()

    written = {"ks_cancel": full, "nw_renewal": one}
    leads = _leads_of(client)
    for wf, version in versions.items():
        calls = [ev for ev in all_events(client)
                 if ev["type"] == "call" and ev["workflow_id"] == wf and ev["config_version"] == version]
        assert calls
        for ev in calls:
            assert ev["cohort_id"] == _member_of(written[wf], leads[ev["lead_id"]]["features"])
        assert {ev["cohort_id"] for ev in calls} == {x["id"] for x in written[wf]} | {"default"}


def test_status_cli_shows_partition_cohort_names_and_baselines_for_every_workflow(world, db_path, capsys):
    client = world(days=1, history_days=2)
    _start(client, db_path)

    assert cli_main(["status", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out

    for w in queries.status(db_path)["workflows"]:
        b = w["baselines"]
        assert w["workflow_id"] in out
        assert f"primary KPI: {w['primary_kpi']}" in out
        assert f"pickup {b['pickup_rate']:.1%}" in out
        assert f"engagement {b['engagement_rate']:.1%}" in out
        assert f"hallucination flags {b['hallucination_flag_rate']:.1%}" in out
        assert f"by {w['partition']['feature'].replace('_', ' ')}" in out
        for cohort in w["partition"]["cohorts"]:
            assert cohort["name"] in out and cohort["id"] in out


def test_status_shows_each_cohorts_share_and_kpi_in_history(db_path, capsys):
    calls = _connected_calls(features(age_band="31-45"), 30, 60) + _connected_calls(features(age_band="60+"), 10, 40)
    _start(scripted_client("engagement_rate", calls), db_path)

    assert cli_main(["status", "wf", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out

    assert re.search(r"age 60\+\s+age_band_60plus\s+40\.0% of traffic\s+engagement_rate 25\.0%", out)
    assert re.search(r"everyone else \(age 31-45\)\s+default\s+60\.0% of traffic\s+engagement_rate 50\.0%", out)


def test_status_says_when_a_workflow_has_no_partition_or_is_unknown(db_path, capsys):
    _start(scripted_client("engagement_rate", _connected_calls(features(), 10, 20)), db_path)

    assert cli_main(["status", "--db", str(db_path)]) == 0
    assert "no feature splits" in capsys.readouterr().out

    assert cli_main(["status", "nope", "--db", str(db_path)]) == 1
    assert "nope" in capsys.readouterr().err
