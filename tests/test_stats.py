"""Ticket 02: the statistics module, through Seam 2 (pure functions over synthetic counts).

Expected values come from outside the module: R's documented `power.prop.test` output, z statistics
worked by hand from the pooled formula, and binomial tails computed exactly with rational arithmetic.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller import stats


def test_sample_size_matches_the_reference_for_the_measured_engagement_rate():
    # (1.645*sqrt(2*0.5712*0.4288) + 0.8416*sqrt(0.544*0.456 + 0.5984*0.4016))^2 / 0.0544^2 = 1022.3
    assert stats.sample_size_per_arm(0.544, relative_mde=0.10, alpha=0.05, power=0.80) == 1023


def test_sample_size_matches_r_power_prop_test():
    # R: power.prop.test(p1=.50, p2=.75, power=.90) => n = 76.7; two-sided .05 uses the z of one-sided .025.
    assert stats.sample_size_per_arm(0.50, relative_mde=0.50, alpha=0.025, power=0.90) == 77


def test_a_baseline_with_no_room_for_the_lift_has_no_sample_size():
    with pytest.raises(ValueError):
        stats.sample_size_per_arm(0.95, relative_mde=0.10, alpha=0.05, power=0.80)


def test_confirm_splits_alpha_over_three_looks_and_sizes_for_it():
    assert stats.CONFIRM_ALPHA_PER_LOOK == pytest.approx(0.05 / 3)
    # (2.1280*0.6999 + 0.8416*0.6989)^2 / 0.0544^2 = 1458.5, up from 1,023 at a single-look 0.05.
    assert stats.confirm_sample_size_per_arm(0.544) == 1459


def test_confirm_look_wins_only_past_the_per_look_threshold():
    # Critical z at 0.05/3 is 2.128. Pooled z against 500/1000 is 2.104 for 547 and 2.149 for 548.
    control = stats.Counts(successes=500, trials=1000)
    assert not stats.beats_control(stats.Counts(547, 1000), control).holds
    assert stats.beats_control(stats.Counts(548, 1000), control).holds


def test_beats_control_reports_the_one_sided_p_value():
    # z = 2.688 for 560/1000 against 500/1000; the normal table gives 0.0036.
    verdict = stats.beats_control(stats.Counts(560, 1000), stats.Counts(500, 1000))
    assert verdict.p_value == pytest.approx(0.0036, abs=0.0001)


def test_a_challenger_below_control_never_wins():
    assert not stats.beats_control(stats.Counts(400, 1000), stats.Counts(500, 1000)).holds


@pytest.mark.parametrize("challenger, control", [
    (stats.Counts(0, 0), stats.Counts(500, 1000)),  # the arm never ran
    (stats.Counts(500, 1000), stats.Counts(0, 0)),
    (stats.Counts(0, 1000), stats.Counts(0, 1000)),  # no variance to test against
])
def test_no_evidence_is_never_significant(challenger, control):
    verdict = stats.beats_control(challenger, control)
    assert not verdict.holds and verdict.p_value == 1.0


@pytest.mark.parametrize("successes, trials", [(-1, 10), (11, 10), (1, -1)])
def test_impossible_counts_are_rejected(successes, trials):
    with pytest.raises(ValueError):
        stats.Counts(successes, trials)


def test_guardrail_passes_only_when_the_lower_bound_clears_the_max_drop():
    # SE = sqrt(2*0.25/1000) = 0.02236; lower bound at one-sided 0.05 = -1.6449*0.02236 = -0.03678.
    arm = stats.Counts(500, 1000)
    assert stats.guardrail_non_inferior(arm, arm, max_drop=0.0368).passed
    held = stats.guardrail_non_inferior(arm, arm, max_drop=0.0367)
    assert not held.passed
    assert held.lower_bound == pytest.approx(-0.03678, abs=1e-5)


def test_guardrail_uses_each_arms_own_variance():
    # SE = sqrt(0.9*0.1/1000 + 0.5*0.5/4000) = 0.012349; diff = 0.40; bound = 0.40 - 1.6449*0.012349 = 0.37969.
    lower = stats.guardrail_non_inferior(stats.Counts(900, 1000), stats.Counts(2000, 4000), max_drop=0.02).lower_bound
    assert lower == pytest.approx(0.37969, abs=1e-4)


def test_a_candidate_clearly_past_the_max_drop_fails_the_guardrail():
    assert not stats.guardrail_non_inferior(stats.Counts(400, 1000), stats.Counts(500, 1000), max_drop=0.02).passed


@pytest.mark.parametrize("candidate, control", [
    (stats.Counts(0, 0), stats.Counts(500, 1000)),
    (stats.Counts(500, 1000), stats.Counts(0, 0)),
])
def test_guardrail_with_no_data_fails(candidate, control):
    assert not stats.guardrail_non_inferior(candidate, control, max_drop=0.02).passed


def test_a_cohort_is_harmed_only_past_the_harm_threshold():
    # Critical z at 0.10 is 1.2816. Against 500/1000 for the candidate: control 530 gives z = 1.342, 528 gives 1.253.
    verdicts = stats.cohort_harm({
        "senior": (stats.Counts(500, 1000), stats.Counts(530, 1000)),
        "young": (stats.Counts(500, 1000), stats.Counts(528, 1000)),
    })
    assert verdicts["senior"].holds
    assert not verdicts["young"].holds


def test_a_winner_has_decayed_only_when_significantly_worse_than_its_holdback():
    # Critical z at 0.05 is 1.645. Winner 500/1000 against holdback 537 gives z = 1.656, against 536 gives 1.611.
    assert stats.decayed(stats.Counts(500, 1000), stats.Counts(537, 1000)).holds
    assert not stats.decayed(stats.Counts(500, 1000), stats.Counts(536, 1000)).holds


def test_screen_picks_the_best_challenger_and_eliminates_the_clearly_worse():
    result = stats.screen(stats.Counts(540, 1000), {
        "W1": stats.Counts(500, 1000),  # z = -1.79, clearly worse
        "W2": stats.Counts(560, 1000),
        "W3": stats.Counts(580, 1000),
    })
    assert result.candidate == "W3"
    assert result.eliminated == ("W1",)


def test_screen_keeps_a_challenger_not_clearly_worse():
    # z = -1.611 against 536: below control, but short of the 1.645 needed to call it clearly worse.
    result = stats.screen(stats.Counts(536, 1000), {"W1": stats.Counts(500, 1000)})
    assert result.eliminated == ()


def test_screen_has_no_candidate_when_nothing_beats_control_on_its_point_estimate():
    result = stats.screen(stats.Counts(500, 1000), {"W1": stats.Counts(490, 1000), "W2": stats.Counts(500, 1000)})
    assert result.candidate is None


def test_screen_breaks_ties_the_same_way_whatever_order_arms_arrive_in():
    tied = {"W2": stats.Counts(550, 1000), "W1": stats.Counts(550, 1000)}
    control = stats.Counts(500, 1000)
    assert stats.screen(control, tied).candidate == "W1"
    assert stats.screen(control, dict(reversed(list(tied.items())))).candidate == "W1"


def test_an_arm_that_never_ran_contributes_nothing_to_the_screen():
    result = stats.screen(stats.Counts(500, 1000), {"W1": stats.Counts(0, 0)})
    assert result.candidate is None and result.eliminated == ()


def test_hallucination_spike_needs_binomial_significance():
    # Exact tails for n=150, p=0.015: P(X>=9) = 0.00047890, P(X>=8) = 0.00205444.
    spike = stats.hallucination_spike(stats.Counts(9, 150), baseline_rate=0.015)
    assert spike.holds
    assert spike.p_value == pytest.approx(0.00047890, rel=1e-4)
    calm = stats.hallucination_spike(stats.Counts(8, 150), baseline_rate=0.015)
    assert not calm.holds
    assert calm.p_value == pytest.approx(0.00205444, rel=1e-4)


def test_hallucination_spike_needs_twice_the_baseline_even_when_significant():
    # Exact tail for n=1000, p=0.015: P(X>=29) = 0.00077981, significant, but 2.9% is under 2 x 1.5%.
    assert not stats.hallucination_spike(stats.Counts(29, 1000), baseline_rate=0.015).holds
    assert stats.hallucination_spike(stats.Counts(30, 1000), baseline_rate=0.015).holds


def test_hallucination_spike_needs_a_population():
    assert not stats.hallucination_spike(stats.Counts(50, 149), baseline_rate=0.015).holds
    assert stats.hallucination_spike(stats.Counts(50, 150), baseline_rate=0.015).holds


def test_one_flag_is_never_an_incident_even_with_a_zero_baseline():
    assert not stats.hallucination_spike(stats.Counts(1, 150), baseline_rate=0.0).holds
    assert stats.hallucination_spike(stats.Counts(20, 150), baseline_rate=0.0).holds


def test_binomial_tail_survives_large_windows():
    # A window far below baseline: the tail is ~1 (its single-term start underflows if summed upward).
    below = stats.hallucination_spike(stats.Counts(1, 100_000), baseline_rate=0.015)
    assert below.p_value == pytest.approx(1.0) and not below.holds
    above = stats.hallucination_spike(stats.Counts(3000, 100_000), baseline_rate=0.015)
    assert above.p_value < 1e-100 and above.holds


def test_caller_id_degraded_needs_the_dial_minimum():
    others = stats.Counts(4000, 10_000)
    assert not stats.caller_id_degraded(stats.Counts(60, 299), others).holds
    assert stats.caller_id_degraded(stats.Counts(60, 300), others).holds


def test_caller_id_degraded_needs_significance():
    # 93/300 against 4000/10000: pooled z = 3.139, p = 0.00085. 94/300: z = 3.023, p = 0.00125.
    others = stats.Counts(4000, 10_000)
    assert stats.caller_id_degraded(stats.Counts(93, 300), others).holds
    assert not stats.caller_id_degraded(stats.Counts(94, 300), others).holds


def test_caller_id_degraded_needs_a_material_drop_even_when_significant():
    # Others at 40%: a 15% drop is 34% exactly. 17000/50000 is exactly on the boundary; 17001 is short of it.
    others = stats.Counts(20_000, 50_000)
    assert stats.caller_id_degraded(stats.Counts(17_000, 50_000), others).holds
    assert not stats.caller_id_degraded(stats.Counts(17_001, 50_000), others).holds


def test_caller_id_with_no_comparison_is_not_degraded():
    assert not stats.caller_id_degraded(stats.Counts(10, 1000), stats.Counts(0, 0)).holds


def test_lower_bound_is_the_conservative_end_of_the_difference():
    # SE = sqrt(0.2464/1000 + 0.25/1000) = 0.02228; 0.06 - 0.8416*0.02228 = 0.04125.
    assert stats.difference_lower_bound(stats.Counts(560, 1000), stats.Counts(500, 1000)) \
        == pytest.approx(0.04125, abs=1e-4)


def test_lower_bound_with_no_data_assumes_the_worst():
    assert stats.difference_lower_bound(stats.Counts(0, 0), stats.Counts(500, 1000), confidence=0.80) == -1.0


@pytest.mark.parametrize("lower_bound, share, incident, expected", [
    (0.04, 0.5, False, 0.4),   # 4 points x half the traffic / 5 points
    (0.20, 1.0, False, 1.0),   # clipped above
    (-0.03, 1.0, False, 0.0),  # clipped below
    (0.01, 0.5, True, 0.9),    # incident fixes are floored
    (0.20, 1.0, True, 1.0),    # the floor doesn't cap a large fix
    (-1.0, 1.0, True, 0.9),
])
def test_impact_score(lower_bound, share, incident, expected):
    assert stats.impact_score(lower_bound, share, incident=incident) == pytest.approx(expected)


# ---- false-positive rates over simulated null experiments


def _within_alpha(false_positives: int, runs: int, alpha: float) -> bool:
    """At or below alpha, allowing three standard errors of Monte Carlo noise."""
    return false_positives / runs <= alpha + 3 * math.sqrt(alpha * (1 - alpha) / runs)


def _arm(rng: np.random.Generator, n: int, rate: float, runs: int) -> list[stats.Counts]:
    return [stats.Counts(int(s), n) for s in rng.binomial(n, rate, runs)]


def test_confirm_keeps_the_false_winner_rate_at_five_percent_over_three_looks():
    # Looks at 1/2, 3/4 and all of the planned sample, on cumulative data, as the experiment engine runs them.
    rng, runs, rate = np.random.default_rng(2), 20_000, 0.544
    planned = stats.confirm_sample_size_per_arm(rate)
    looks = (planned // 2, planned * 3 // 4, planned)
    increments = np.diff((0, *looks))
    challenger = rng.binomial(increments, rate, (runs, 3)).cumsum(axis=1)
    control = rng.binomial(increments, rate, (runs, 3)).cumsum(axis=1)
    false_winners = sum(
        any(stats.beats_control(stats.Counts(int(c), n), stats.Counts(int(ctl), n)).holds
            for c, ctl, n in zip(challenger_path, control_path, looks))
        for challenger_path, control_path in zip(challenger, control))
    assert _within_alpha(false_winners, runs, 0.05)


def test_guardrail_false_pass_rate_at_the_margin():
    # True difference exactly at -max_drop: passing here is the error the test bounds.
    rng, runs, n = np.random.default_rng(3), 20_000, 3000
    candidates, controls = _arm(rng, n, 0.48, runs), _arm(rng, n, 0.50, runs)
    passes = sum(stats.guardrail_non_inferior(c, ctl, max_drop=0.02).passed for c, ctl in zip(candidates, controls))
    assert _within_alpha(passes, runs, 0.05)


def test_cohort_harm_false_positive_rate():
    rng, runs, n = np.random.default_rng(4), 20_000, 150  # a small cohort: about one day of the 60+ band
    candidates, controls = _arm(rng, n, 0.544, runs), _arm(rng, n, 0.544, runs)
    harmed = sum(stats.cohort_harm({"c": (a, b)})["c"].holds for a, b in zip(candidates, controls))
    assert _within_alpha(harmed, runs, 0.10)


def test_decay_false_positive_rate_on_the_80_20_split():
    rng, runs = np.random.default_rng(5), 20_000
    winners, holdbacks = _arm(rng, 2400, 0.544, runs), _arm(rng, 600, 0.544, runs)
    assert _within_alpha(sum(stats.decayed(w, h).holds for w, h in zip(winners, holdbacks)),
                         runs, 0.05)


def test_screen_rarely_eliminates_a_challenger_that_is_as_good_as_control():
    rng, runs, n = np.random.default_rng(6), 20_000, 500
    controls, challengers = _arm(rng, n, 0.544, runs), _arm(rng, n, 0.544, runs)
    eliminated = sum(bool(stats.screen(ctl, {"W1": c}).eliminated) for ctl, c in zip(controls, challengers))
    assert _within_alpha(eliminated, runs, 0.05)


def test_hallucination_false_positive_rate():
    rng, runs = np.random.default_rng(7), 200_000
    windows = _arm(rng, 150, 0.015, runs)
    assert _within_alpha(sum(stats.hallucination_spike(w, 0.015).holds for w in windows), runs, 0.001)


def test_caller_id_false_positive_rate():
    rng, runs = np.random.default_rng(8), 200_000
    callers, others = _arm(rng, 300, 0.40, runs), _arm(rng, 3000, 0.40, runs)
    assert _within_alpha(sum(stats.caller_id_degraded(c, o).holds for c, o in zip(callers, others)), runs, 0.001)


# ---- determinism


def test_identical_inputs_give_identical_outputs():
    a, b = stats.Counts(560, 1000), stats.Counts(500, 1000)
    def decide():
        return (stats.sample_size_per_arm(0.544, 0.10, 0.05, 0.80), stats.confirm_sample_size_per_arm(0.544),
                stats.beats_control(a, b), stats.guardrail_non_inferior(a, b, 0.02),
                stats.cohort_harm({"x": (a, b), "y": (b, a)}), stats.decayed(a, b), stats.screen(b, {"W": a, "V": b}),
                stats.hallucination_spike(stats.Counts(9, 150), 0.015), stats.caller_id_degraded(b, a),
                stats.difference_lower_bound(a, b, 0.80), stats.impact_score(0.04, 0.5))
    assert decide() == decide()


def test_screen_with_no_control_data_has_no_candidate():
    assert stats.screen(stats.Counts(0, 0), {"W1": stats.Counts(500, 1000)}).candidate is None


def test_a_stratified_screen_is_not_fooled_by_an_arm_mix_that_differs_between_strata():
    # Control 56% pooled against 34%, but the challenger is 10 points better inside each stratum.
    high = (stats.Counts(540, 900), {"W2": stats.Counts(70, 100)})
    low = (stats.Counts(20, 100), {"W2": stats.Counts(270, 900)})

    result = stats.screen_strata([high, low])

    assert result.candidate == "W2" and result.eliminated == ()


def test_a_stratified_screen_eliminates_a_challenger_worse_in_every_stratum():
    one = (stats.Counts(300, 500), {"W2": stats.Counts(80, 250)})
    two = (stats.Counts(150, 500), {"W2": stats.Counts(30, 250)})

    assert stats.screen_strata([one, two]) == stats.ScreenResult(None, ("W2",))


def test_a_single_stratum_decides_exactly_as_the_plain_screen_does():
    control = stats.Counts(540, 1000)
    challengers = {"W1": stats.Counts(450, 1000), "W2": stats.Counts(560, 1000), "W3": stats.Counts(545, 1000)}

    assert stats.screen_strata([(control, challengers)]) == stats.screen(control, challengers)


def test_a_stratum_where_an_arm_never_ran_contributes_nothing_to_that_arm():
    ran = (stats.Counts(500, 1000), {"W2": stats.Counts(550, 1000)})
    empty = (stats.Counts(0, 0), {"W2": stats.Counts(0, 500)})
    no_challenger = (stats.Counts(900, 1000), {"W2": stats.Counts(0, 0)})

    assert stats.screen_strata([ran, empty, no_challenger]) == stats.screen(*ran)


# -- the confirm stage, over strata ----------------------------------------------------------------
def test_stratified_confirm_test_with_one_stratum_is_the_plain_z_test():
    challenger, control = stats.Counts(330, 600), stats.Counts(270, 600)
    assert stats.beats_control_strata([(challenger, control)]) == stats.beats_control(challenger, control)


def test_stratified_confirm_test_matches_a_worked_example():
    # Strata of (60/100 vs 50/100) and (30/100 vs 20/100): each has weight 50 and difference 0.10, so the
    # combined difference is 0.10. Pooled rates 0.55 and 0.25 give variance 21.75/10000, so z = 2.1442.
    verdict = stats.beats_control_strata([(stats.Counts(60, 100), stats.Counts(50, 100)),
                                          (stats.Counts(30, 100), stats.Counts(20, 100))])
    assert verdict.p_value == pytest.approx(0.01601, abs=2e-4)
    assert verdict.holds  # below 0.05/3 = 0.01667


def test_a_config_change_between_strata_cannot_pass_for_a_difference_between_arms():
    # Inside each stratum the arms are level; the challenger just ran mostly in the better one.
    good, bad = (stats.Counts(720, 900), stats.Counts(80, 100)), (stats.Counts(20, 100), stats.Counts(180, 900))
    assert stats.beats_control(stats.Counts(740, 1000), stats.Counts(260, 1000)).holds
    assert stats.beats_control_strata([good, bad]).p_value == pytest.approx(0.5)


def test_stratified_lower_bound_with_one_stratum_is_the_plain_bound():
    a, b = stats.Counts(300, 500), stats.Counts(250, 500)
    assert stats.difference_lower_bound_strata([(a, b)], 0.8) == pytest.approx(stats.difference_lower_bound(a, b, 0.8))


def test_stratified_lower_bound_matches_a_worked_example():
    # Equal weights: difference 0.10, variance (0.24/100 + 0.25/100 + 0.21/100 + 0.16/100) / 4 = 0.00215,
    # so the 80% bound is 0.10 - 0.8416 * 0.04637 = 0.06097.
    bound = stats.difference_lower_bound_strata([(stats.Counts(60, 100), stats.Counts(50, 100)),
                                                 (stats.Counts(30, 100), stats.Counts(20, 100))], 0.8)
    assert bound == pytest.approx(0.06097, abs=2e-4)


def test_stratified_lower_bound_with_no_data_is_the_worst_case():
    assert stats.difference_lower_bound_strata([(stats.Counts(0, 0), stats.Counts(5, 10))], 0.8) == -1.0


def test_stratified_guardrail_fails_when_the_bound_is_below_the_allowed_drop():
    level = [(stats.Counts(250, 1000), stats.Counts(250, 1000)), (stats.Counts(500, 2000), stats.Counts(500, 2000))]
    # Level arms over 3000 dials each: bound = -1.645 * sqrt(2 * 0.1875 / 3000) = -0.01839, ignoring the rounding of weights.
    assert stats.guardrail_non_inferior_strata(level, 0.02).passed
    failed = stats.guardrail_non_inferior_strata(level, 0.015)
    assert not failed.passed and failed.lower_bound == pytest.approx(-0.0184, abs=3e-4)


def test_stratified_harm_check_names_only_the_harmed_cohort():
    harm = stats.cohort_harm_strata({
        "young": [(stats.Counts(40, 100), stats.Counts(60, 100))],
        "old": [(stats.Counts(50, 100), stats.Counts(50, 100))]})
    assert harm["young"].holds and not harm["old"].holds


# -- decay against the holdback, over strata ------------------------------------------------------
def test_one_stratum_of_decay_is_the_plain_decay_test():
    winner, holdback = stats.Counts(500, 1000), stats.Counts(537, 1000)
    assert stats.decayed_strata([(winner, holdback)]) == stats.decayed(winner, holdback)


def test_a_config_change_between_strata_cannot_pass_for_decay():
    # Pooled, the winner is 540/1000 against the holdback's 500/600, but inside each version the two are level.
    high = (stats.Counts(90, 100), stats.Counts(450, 500))
    low = (stats.Counts(450, 900), stats.Counts(50, 100))
    assert stats.decayed(stats.Counts(540, 1000), stats.Counts(500, 600)).holds
    verdict = stats.decayed_strata([high, low])
    assert verdict.p_value == pytest.approx(0.5) and not verdict.holds


def test_a_stratum_with_no_holdback_calls_adds_nothing_to_decay():
    only = (stats.Counts(500, 1000), stats.Counts(537, 1000))
    assert stats.decayed_strata([only, (stats.Counts(40, 80), stats.Counts(0, 0))]) == stats.decayed(*only)
    assert stats.decayed_strata([]) == stats.Verdict(False, 1.0)
