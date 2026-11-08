"""Ticket 11: the supervisor's single screen, driven through Seam 1 against the real simulator."""
from __future__ import annotations

from controller import queries
from controller.__main__ import main as cli_main
from controller.loop import Controller

from .test_hallucination import WF, hallucinating_world


def test_status_shows_the_incident_its_fix_and_todays_changes_on_one_screen(world, db_path, capsys):
    client, troubled, trouble = hallucinating_world(world, {"P1": 0.25})
    c = Controller(troubled, db_path, sleep=lambda _: None)
    for _ in range(400):
        c.step()
        if any(i["applied_minute"] for i in queries.incidents(db_path, WF)):
            break
    c.step()
    assert cli_main(["status", WF, "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "incident" in out and "changed today" in out
    assert "incident response" in out
    assert "chose not to act" in out and "needs your approval" in out


def test_without_a_workflow_every_workflow_gets_a_compact_summary(world, db_path, capsys):
    client = world(days=2, history_days=2)
    c = Controller(client, db_path, sleep=lambda _: None)
    for _ in range(5):
        c.step()
    assert cli_main(["status", "--db", str(db_path)]) == 0
    assert capsys.readouterr().out.count("summary ") >= 1


def test_supervisor_is_none_before_the_controller_has_analysed_the_workflow(db_path):
    assert queries.supervisor(db_path, WF) is None
