"""Ticket 12: loop health and paging, driven through Seam 1 against the real simulator."""
from __future__ import annotations

import re

from controller import queries
from controller.__main__ import main as cli_main
from controller.loop import Controller

from .harness import recount_calls, troubled_client

WF = "nw_renewal"
IN_FLIGHT = ("submitting", "pending", "approved", "applying")


def no_sleep(_: float) -> None:
    """Backoff still happens; a test need not wait it out."""


def controller(http, db_path) -> Controller:
    return Controller(http, db_path, sleep=no_sleep)


def pages(db_path) -> dict[str, str]:
    """Active pages, condition -> message."""
    return {a["condition"]: a["message"] for a in queries.health(db_path)["alerts"]}


def assert_store_counts_every_call_the_api_published(client, db_path) -> None:
    oracle = recount_calls(client)
    assert {(r["workflow_id"], r["date"]): (r["dials"], r["connected"], r["engaged"]) for r in queries.daily_kpis(db_path)} == {
        k: (c["dials"], c["connected"], c["engaged"]) for k, c in oracle.items()}


def steps(c: Controller, n: int) -> None:
    for _ in range(n):
        c.step()


def test_health_shows_decisions_per_hour_credit_burn_and_no_alerts_during_a_run(world, db_path, capsys):
    client = world(days=1, history_days=1)
    c = controller(client, db_path)
    steps(c, 44)  # to D0 11:00, inside the dialling day
    credits_before = client.get("/sim/state").json()["credits_remaining"]
    client.post("/validation/runs", json={"workflow_id": WF, "prompt_version": "P3", "n": 300})
    steps(c, 1)

    now = client.get("/sim/state").json()["sim_minute"]
    workflows = [w["workflow_id"] for w in client.get("/workflows").json()]
    expected = sum(1 for w in workflows for d in queries.decisions(db_path, w) if d["minute"] > now - 60)
    assert cli_main(["health", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert re.search(rf"decisions/hour\s+{expected}\b", out)
    assert re.search(rf"credits\s+{credits_before - 3:g} left, 3 spent", out)
    assert re.search(r"alerts\s+none", out)


def test_credits_below_the_reserve_page_and_say_what_to_look_at(world, db_path, capsys):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    c = controller(troubled, db_path)
    trouble.credits_left = 30  # the reserve itself is not below the reserve
    steps(c, 1)
    assert pages(db_path) == {}

    trouble.credits_left = 12
    steps(c, 2)

    message = pages(db_path)["credits_below_reserve"]
    assert "12" in message and "30" in message and "controller health" in message
    assert [a["raised_minute"] for a in queries.health(db_path)["alerts"]] == [30]  # raised once, not every tick
    assert cli_main(["health", "--db", str(db_path)]) == 0
    assert re.search(r"alerts\s+1 active", (out := capsys.readouterr().out)) and message in out


def test_ingest_lag_above_two_ticks_pages_and_clears_with_nothing_lost(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    c = controller(troubled, db_path)
    steps(c, 40)
    decisions_before = len(queries.decisions(db_path, WF))

    trouble.fail("GET", "/events/chunks")
    steps(c, 2)
    assert queries.health(db_path)["ingest_lag_ticks"] == 2 and pages(db_path) == {}
    steps(c, 1)

    message = pages(db_path)["ingest_lag"]
    assert "3 ticks" in message and "/events/chunks" in message
    assert len(queries.decisions(db_path, WF)) == decisions_before  # nothing is decided on a store that is behind

    trouble.recover()
    steps(c, 1)
    assert pages(db_path) == {} and queries.health(db_path)["ingest_lag_ticks"] == 0
    c.run()
    assert client.get("/sim/state").json()["finished"] is True
    assert_store_counts_every_call_the_api_published(client, db_path)


def hallucinating(world, db_path):
    client = world(days=3, history_days=2)
    troubled, trouble = troubled_client(client, approval_only=True)
    trouble.hallucinate[WF] = {"P1": 0.25}
    return client, troubled, trouble, controller(troubled, db_path)


def supervisor_changes_the_prompt(client) -> None:
    """The platform only takes a prompt through approval, so an edit it does allow reads back as a prompt change."""
    current = client.get(f"/workflows/{WF}/config").json()
    current["config"]["allocations"]["default"]["caller_id"] = {"D1": 0.9, "D2": 0.1}
    r = client.put(f"/workflows/{WF}/config", json={
        "proposal_id": f"supervisor-prompt-{current['version']}", "base_version": current["version"],
        "proposed_config": current["config"], "rationale": "a person fixed it"})
    assert r.status_code == 200, r.text


def step_to_detection(c: Controller, db_path, max_ticks: int = 4 * 60) -> dict:
    for _ in range(max_ticks):
        c.step()
        if queries.incidents(db_path, WF):
            return queries.incidents(db_path, WF)[0]
    raise AssertionError("no incident raised")


def step_to_minute(c: Controller, db_path, minute: int) -> None:
    while queries.health(db_path)["sim_minute_seen"] < minute:
        c.step()


def test_an_incident_still_open_four_hours_after_detection_pages_with_where_to_look(world, db_path):
    _, _, trouble, c = hallucinating(world, db_path)
    trouble.validation_fails[WF] = {"P1"}
    trouble.blank_summary = True  # every fix is rejected, so the incident stays open
    incident = step_to_detection(c, db_path)

    step_to_minute(c, db_path, incident["detected_minute"] + 225)
    assert "incident_unfixed" not in pages(db_path)
    step_to_minute(c, db_path, incident["detected_minute"] + 240)

    assert queries.incidents(db_path, WF)[0]["status"] == "open"
    message = pages(db_path)["incident_unfixed"]
    assert incident["incident_id"] in message and "P1" in message and f"controller why {WF}" in message


def test_an_incident_handed_to_a_person_pages_once_four_hours_have_passed_and_clears_when_the_prompt_changes(world, db_path):
    client, _, trouble, c = hallucinating(world, db_path)
    trouble.validation_fails[WF] = {"P1", "P3", "P4", "P5"}  # nothing passes: it escalates at once
    incident = step_to_detection(c, db_path)
    assert incident["status"] == "escalated"
    assert "incident_unfixed" not in pages(db_path)

    step_to_minute(c, db_path, incident["detected_minute"] + 240)
    assert incident["incident_id"] in pages(db_path)["incident_unfixed"]

    supervisor_changes_the_prompt(client)
    steps(c, 2)  # the edit reaches the store through the event stream
    assert "incident_unfixed" not in pages(db_path)


def degraded(world, db_path, days: int = 2):
    client = world(days=days, history_days=1)
    troubled, trouble = troubled_client(client, degrade={WF: "D2"}, approval_only=True)
    return client, trouble, controller(troubled, db_path)


def statuses(db_path) -> list[str]:
    return [p["status"] for p in queries.proposals(db_path, WF)]


def step_until(c: Controller, done, before_each=lambda: None, max_ticks: int = 4 * 96) -> None:
    for _ in range(max_ticks):
        if done():
            return
        before_each()
        c.step()
    raise AssertionError("condition never reached")


def test_a_lapsed_approval_pages_with_the_proposal_to_look_at(world, db_path):
    _, trouble, c = degraded(world, db_path)
    trouble.refuse("POST", "/apply", 410, "approval_lapsed")

    step_until(c, lambda: "lapsed" in statuses(db_path))

    message = pages(db_path)["apply_failed"]
    assert "nw_renewal-caller_id-1" in message and "lapsed" in message and f"controller why {WF}" in message


def test_a_stale_base_loop_pages_only_from_the_third_in_a_row_and_clears_when_it_is_broken(world, db_path):
    _, trouble, c = degraded(world, db_path, days=4)

    def ended() -> list[dict]:
        return [p for p in queries.proposals(db_path, WF) if p["status"] not in IN_FLIGHT]

    def paged_exactly_at_three_in_a_row() -> bool:
        last_three = [p["status"] for p in ended()][-3:]
        paged = "apply_failed" in pages(db_path)
        assert paged == (last_three == ["stale"] * 3)
        return paged

    c.start()
    step_until(c, paged_exactly_at_three_in_a_row, lambda: trouble.refuse("POST", "/apply", 409, "stale_base_version"))

    message = pages(db_path)["apply_failed"]
    assert all(p["proposal_id"] in message for p in ended()[-3:]) and "stale" in message

    trouble.recover()
    step_until(c, lambda: ended()[-1]["status"] != "stale")
    assert "apply_failed" not in pages(db_path)


def notes(db_path) -> list[dict]:
    return queries.health(db_path)["notes"]


def test_a_rejection_is_a_note_not_a_page(world, db_path, capsys):
    _, trouble, c = degraded(world, db_path)
    trouble.blank_summary = True
    c.start()

    step_until(c, lambda: "rejected" in statuses(db_path))

    assert pages(db_path) == {}
    assert [(n["outcome"], n["proposal_id"]) for n in notes(db_path)] == [("rejected", "nw_renewal-caller_id-1")]
    assert cli_main(["health", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert re.search(r"alerts\s+none", out) and re.search(r"notes\s+1 in the last simulated day", out)
    assert re.search(r"nw_renewal\s+rejected\s+nw_renewal-caller_id-1", out)


def test_an_expiry_is_a_note_not_a_page(world, db_path):
    _, trouble, c = degraded(world, db_path)
    trouble.ttl_hours = 0.1
    c.start()

    step_until(c, lambda: "expired" in statuses(db_path))

    assert pages(db_path) == {}
    assert ("expired", "nw_renewal-caller_id-1") in [(n["outcome"], n["proposal_id"]) for n in notes(db_path)]


def tick_with_config_call(c: Controller, trouble, path: str, max_ticks: int = 60) -> None:
    """Step until a tick has asked for this path, which `trouble` has been told to break."""
    before = trouble.requests[path]
    for _ in range(max_ticks):
        c.step()
        if trouble.requests[path] > before:
            return
    raise AssertionError(f"{path} was never called")


def test_a_single_5xx_is_retried_with_backoff_and_is_only_a_note(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    slept: list[float] = []
    c = Controller(troubled, db_path, sleep=slept.append)
    path = f"/workflows/{WF}/config"
    trouble.fail("GET", path, times=1)
    c.start()

    tick_with_config_call(c, trouble, path)

    assert slept == [0.5]
    assert pages(db_path) == {}
    [note] = [n for n in notes(db_path) if n["outcome"] == "retried"]
    assert path in str(note["detail"]["retries"])
    assert not [d for d in queries.decisions(db_path, WF, "loop") if d["outcome"] == "error"]


def other_workflows(client) -> list[str]:
    return [w["workflow_id"] for w in client.get("/workflows").json() if w["workflow_id"] != WF]


def assert_one_workflow_failed_and_the_others_kept_deciding(client, db_path) -> None:
    assert client.get("/sim/state").json()["finished"] is True
    assert [d for d in queries.decisions(db_path, WF, "loop") if d["outcome"] == "error"]
    assert "error" in {n["outcome"] for n in notes(db_path) if n["workflow_id"] == WF}
    assert pages(db_path) == {}
    end = client.get("/sim/state").json()["sim_minute"]
    for other in other_workflows(client):
        decided = [d for d in queries.decisions(db_path, other) if d["kind"] != "loop"]
        assert max(d["minute"] for d in decided) > end - 1440
        assert not queries.decisions(db_path, other, "loop")


def test_a_5xx_that_outlasts_its_retries_on_one_workflow_does_not_stop_the_others(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.fail("GET", f"/workflows/{WF}/config")

    controller(troubled, db_path).run()

    assert_one_workflow_failed_and_the_others_kept_deciding(client, db_path)


def test_an_answer_the_controller_cannot_read_on_one_workflow_does_not_stop_the_others(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.garbled.add(f"/workflows/{WF}/config")

    controller(troubled, db_path).run()

    assert_one_workflow_failed_and_the_others_kept_deciding(client, db_path)


def test_proposals_ending_stale_for_a_reason_other_than_the_base_moving_do_not_page(world, db_path):
    _, trouble, c = degraded(world, db_path, days=4)
    c.start()

    def three_refused_in_a_row() -> bool:
        ended = [p["status"] for p in queries.proposals(db_path, WF) if p["status"] not in IN_FLIGHT]
        return ended[-3:] == ["stale"] * 3

    step_until(c, three_refused_in_a_row, lambda: trouble.refuse("POST", "/apply", 422, "invalid_config"))

    assert "apply_failed" not in pages(db_path)


def test_an_answer_the_controller_cannot_read_from_ingest_pages_as_lag_and_does_not_stop_the_loop(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    c = controller(troubled, db_path)
    c.start()

    trouble.garbled.add("/events/chunks")
    steps(c, 3)

    assert "ingest_lag" in pages(db_path)


def test_an_approved_proposal_is_still_applied_while_ingest_is_down(world, db_path):
    client, trouble, c = degraded(world, db_path)
    c.start()
    step_until(c, lambda: statuses(db_path)[:1] == ["pending"])

    trouble.fail("GET", "/events/chunks")
    step_until(c, lambda: statuses(db_path)[0] in ("applied", "rejected", "expired"))
    assert statuses(db_path)[0] == "applied" and queries.health(db_path)["ingest_lag_ticks"] > 0
