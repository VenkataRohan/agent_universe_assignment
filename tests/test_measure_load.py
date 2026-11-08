"""The load-measurement script's own plumbing (the measurement itself is a slow, manual run)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("measure_load", ROOT / "scripts" / "measure_load.py")
measure_load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure_load)


def test_shrink_changes_only_the_days_line(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text("name: x\ndays: 5\nhistory_days: 7\ntenants: []\n")
    (tmp_path / "out").mkdir()
    out = measure_load.shrink(src, 1, tmp_path / "out")
    assert out.read_text() == "name: x\ndays: 1\nhistory_days: 7\ntenants: []\n"


def test_percentile_picks_a_member_and_handles_empty():
    assert measure_load.pct([], .95) == 0.0
    assert measure_load.pct([1.0, 2.0, 3.0, 4.0], .95) == 4.0
    assert measure_load.pct([5.0], .5) == 5.0
