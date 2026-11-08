"""The statistics the controller decides with. Pure functions, no I/O, no randomness (Seam 2)."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist

_NORMAL = NormalDist()

CONFIRM_ALPHA_PER_LOOK = 0.05 / 3  # at most 3 daily looks share the 5% false-winner rate
CONFIRM_RELATIVE_MDE = 0.10
CONFIRM_POWER = 0.80
CONFIRM_MAX_LOOKS = 3
CONFIRM_MAX_DAYS = 7  # how long a confirm may wait for half its planned sample before it ends undecided (ADR-0011)
GUARDRAIL_ALPHA = 0.05
HARM_ALPHA = 0.10  # per cohort, deliberately uncorrected: a harm check should err towards blocking
DECAY_ALPHA = 0.05
SCREEN_ELIMINATION_ALPHA = 0.05
SCREEN_MIN_TRAFFIC_PER_ARM = 200  # about two days for the smallest arm of a split at the traffic we measured (ADR-0010)
SCREEN_MAX_DAYS = 4               # how long a screen may wait for that traffic before it ends without a candidate

HALLUCINATION_ALPHA = 0.001
HALLUCINATION_MIN_CONNECTS = 150
HALLUCINATION_MIN_RATIO = 2
MIN_BASELINE_FLAG_RATE = 0.001  # a workflow with no flags in history must not page on its first one

CALLER_ID_ALPHA = 0.001
CALLER_ID_MIN_DIALS = 300
CALLER_ID_MIN_DROP_PERCENT = 15

IMPACT_CONFIDENCE = 0.80
IMPACT_FULL_SCALE = 0.05  # 5 percentage points of conclusion rate scores 1.0
INCIDENT_IMPACT_FLOOR = 0.9


@dataclass(frozen=True)
class Counts:
    """Successes out of trials for one arm or population."""
    successes: int
    trials: int

    def __post_init__(self) -> None:
        if not 0 <= self.successes <= self.trials:
            raise ValueError(f"impossible counts: {self.successes} of {self.trials}")

    @property
    def rate(self) -> float:
        return self.successes / self.trials if self.trials else 0.0


@dataclass(frozen=True)
class Verdict:
    """A test's decision and the one-sided p-value behind it, for the audit trail."""
    holds: bool  # the test's whole condition is met, including any minimum sample or effect size
    p_value: float


def _p_greater(a: Counts, b: Counts) -> float:
    """One-sided pooled z-test p-value for "a's rate is above b's". 1.0 when there is nothing to test."""
    if not a.trials or not b.trials:
        return 1.0
    pooled = (a.successes + b.successes) / (a.trials + b.trials)
    variance = pooled * (1 - pooled) * (1 / a.trials + 1 / b.trials)
    if variance == 0:
        return 1.0
    return _NORMAL.cdf(-(a.rate - b.rate) / math.sqrt(variance))


def _one_sided(better: Counts, worse: Counts, alpha: float) -> Verdict:
    p = _p_greater(better, worse)
    return Verdict(p < alpha, p)


def beats_control(challenger: Counts, control: Counts) -> Verdict:
    """One-sided two-proportion z-test that the challenger is better than control, at the per-look alpha."""
    return _one_sided(challenger, control, CONFIRM_ALPHA_PER_LOOK)


def beats_control_strata(pairs: Sequence[tuple[Counts, Counts]]) -> Verdict:
    """`beats_control` over (challenger, control) strata. With one stratum it is the same test."""
    p = _p_greater_strata(pairs)
    return Verdict(p < CONFIRM_ALPHA_PER_LOOK, p)


def _p_greater_strata(pairs: Sequence[tuple[Counts, Counts]]) -> float:
    """One-sided p-value for "the first of each pair is above the second", combined over strata."""
    ran = [(a, b) for a, b in pairs if a.trials and b.trials]
    if not ran:
        return 1.0
    difference, variance = _stratified_difference(ran)
    return _NORMAL.cdf(-difference / math.sqrt(variance)) if variance else 1.0


def sample_size_per_arm(baseline_rate: float, relative_mde: float, alpha: float, power: float) -> int:
    """Calls each arm needs for a one-sided two-proportion test to detect a relative lift."""
    lifted = baseline_rate * (1 + relative_mde)
    if not 0 < baseline_rate < 1 or not 0 < lifted < 1:
        raise ValueError(f"no room for a {relative_mde:.0%} lift on a baseline of {baseline_rate}")
    pooled = (baseline_rate + lifted) / 2
    z_alpha, z_power = _NORMAL.inv_cdf(1 - alpha), _NORMAL.inv_cdf(power)
    root = (z_alpha * math.sqrt(2 * pooled * (1 - pooled))
            + z_power * math.sqrt(baseline_rate * (1 - baseline_rate) + lifted * (1 - lifted)))
    return math.ceil(root ** 2 / (lifted - baseline_rate) ** 2)


def confirm_sample_size_per_arm(baseline_rate: float) -> int:
    """Planned calls per arm for the confirm stage, sized at the per-look alpha."""
    return sample_size_per_arm(baseline_rate, CONFIRM_RELATIVE_MDE, CONFIRM_ALPHA_PER_LOOK, CONFIRM_POWER)


@dataclass(frozen=True)
class GuardrailCheck:
    passed: bool
    lower_bound: float  # on candidate rate minus control rate


def difference_lower_bound(a: Counts, b: Counts, confidence: float = IMPACT_CONFIDENCE) -> float:
    """One-sided lower confidence bound on a's rate minus b's. -1.0 (the worst case) if either has no data."""
    if not a.trials or not b.trials:
        return -1.0
    variance = a.rate * (1 - a.rate) / a.trials + b.rate * (1 - b.rate) / b.trials
    return a.rate - b.rate - _NORMAL.inv_cdf(confidence) * math.sqrt(variance)


def difference_lower_bound_strata(pairs: Sequence[tuple[Counts, Counts]], confidence: float = IMPACT_CONFIDENCE) -> float:
    """`difference_lower_bound` over strata, with the same Mantel-Haenszel weights as the tests."""
    ran = [(a, b) for a, b in pairs if a.trials and b.trials]
    if not ran:
        return -1.0
    weights = [a.trials * b.trials / (a.trials + b.trials) for a, b in ran]
    total = sum(weights)
    difference = sum(w * (a.rate - b.rate) for w, (a, b) in zip(weights, ran)) / total
    variance = sum(w ** 2 * (a.rate * (1 - a.rate) / a.trials + b.rate * (1 - b.rate) / b.trials)
                   for w, (a, b) in zip(weights, ran)) / total ** 2
    return difference - _NORMAL.inv_cdf(confidence) * math.sqrt(variance)


def guardrail_non_inferior(candidate: Counts, control: Counts, max_drop: float,
                           alpha: float = GUARDRAIL_ALPHA) -> GuardrailCheck:
    """Non-inferiority: the lower bound of the guardrail difference must not fall below -max_drop."""
    lower = difference_lower_bound(candidate, control, 1 - alpha)
    return GuardrailCheck(lower >= -max_drop, lower)


def guardrail_non_inferior_strata(pairs: Sequence[tuple[Counts, Counts]], max_drop: float) -> GuardrailCheck:
    """`guardrail_non_inferior` over (candidate, control) strata."""
    lower = difference_lower_bound_strata(pairs, 1 - GUARDRAIL_ALPHA)
    return GuardrailCheck(lower >= -max_drop, lower)


def cohort_harm(arms: Mapping[str, tuple[Counts, Counts]]) -> dict[str, Verdict]:
    """For each cohort's (candidate, control) counts: is the candidate significantly worse there?"""
    return {cohort: _one_sided(control, candidate, HARM_ALPHA)
            for cohort, (candidate, control) in sorted(arms.items())}


def cohort_harm_strata(arms: Mapping[str, Sequence[tuple[Counts, Counts]]]) -> dict[str, Verdict]:
    """`cohort_harm` where each cohort has (candidate, control) strata."""
    out = {}
    for cohort, pairs in sorted(arms.items()):
        p = _p_greater_strata([(control, candidate) for candidate, control in pairs])
        out[cohort] = Verdict(p < HARM_ALPHA, p)
    return out


def decayed(winner: Counts, holdback: Counts) -> Verdict:
    """One-sided test that a promoted winner is now worse than its holdback."""
    return _one_sided(holdback, winner, DECAY_ALPHA)


@dataclass(frozen=True)
class ScreenResult:
    candidate: str | None        # the challenger to confirm; None ends the experiment
    eliminated: tuple[str, ...]  # challengers clearly worse than control, by name


def screen(control: Counts, challengers: Mapping[str, Counts]) -> ScreenResult:
    """The screen over one population. See `screen_strata`."""
    return screen_strata([(control, challengers)])


def screen_strata(strata: Sequence[tuple[Counts, Mapping[str, Counts]]]) -> ScreenResult:
    """Drop the clearly worse, then take the best remaining challenger if it beats control on its point estimate.

    `strata`: (control, challengers) per stratum. Arms are compared only inside a stratum, then combined, so a
    change between strata can't pass for a difference between arms. Ties go to the alphabetically first name. A
    stratum where a challenger or control never ran says nothing about that challenger.
    """
    ran = {}
    for name in sorted({name for _, challengers in strata for name in challengers}):
        pairs = [(challengers[name], control) for control, challengers in strata
                 if name in challengers and challengers[name].trials and control.trials]
        if pairs:
            ran[name] = _stratified_difference(pairs)
    eliminated = tuple(name for name, (difference, variance) in ran.items()
                       if variance and _NORMAL.cdf(difference / math.sqrt(variance)) < SCREEN_ELIMINATION_ALPHA)
    remaining = [name for name in ran if name not in eliminated]
    best = max(remaining, key=lambda name: ran[name][0], default=None)  # max keeps the first of equals
    return ScreenResult(best if best is not None and ran[best][0] > 0 else None, eliminated)


def _stratified_difference(pairs: Sequence[tuple[Counts, Counts]]) -> tuple[float, float]:
    """Challenger's rate minus control's, Mantel-Haenszel weighted over (challenger, control) strata, and its
    variance under "no difference". With one stratum this is the pooled two-proportion test."""
    weights = [a.trials * b.trials / (a.trials + b.trials) for a, b in pairs]
    total = sum(weights)
    pooled = [(a.successes + b.successes) / (a.trials + b.trials) for a, b in pairs]
    difference = sum(w * (a.rate - b.rate) for w, (a, b) in zip(weights, pairs)) / total
    return difference, sum(w * p * (1 - p) for w, p in zip(weights, pooled)) / total ** 2


def _binomial_p_at_least(k: int, n: int, p: float) -> float:
    """Exact P(X >= k) for X ~ Binomial(n, p).

    Sums outward from k, away from the mean, so terms only shrink. A term that underflows is then
    truly negligible, whichever tail is being summed.
    """
    if k <= 0 or p >= 1:
        return 1.0
    upper = k > n * p
    i = k if upper else k - 1
    term = math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                    + i * math.log(p) + (n - i) * math.log1p(-p))
    odds = p / (1 - p)
    total = 0.0
    while 0 <= i <= n and term > total * 1e-17:
        total += term
        if upper:
            term *= (n - i) / (i + 1) * odds
            i += 1
        else:
            term *= i / (n - i + 1) / odds
            i -= 1
    return min(total, 1.0) if upper else max(1.0 - total, 0.0)


def hallucination_spike(window: Counts, baseline_rate: float) -> Verdict:
    """Flags out of connects in a rolling window: enough connects, at least 2x baseline, one-sided binomial p."""
    baseline = max(baseline_rate, MIN_BASELINE_FLAG_RATE)
    p = _binomial_p_at_least(window.successes, window.trials, baseline)
    return Verdict(window.trials >= HALLUCINATION_MIN_CONNECTS
                   and window.successes >= HALLUCINATION_MIN_RATIO * baseline * window.trials
                   and p < HALLUCINATION_ALPHA, p)


def caller_id_degraded(caller: Counts, others: Counts) -> Verdict:
    """A caller ID's pickup against the workflow's other caller IDs: enough dials, a material drop, one-sided z."""
    p = _p_greater(others, caller)
    # Integer arithmetic: the 15% boundary must not depend on how a rate rounds.
    material = (caller.successes * others.trials * 100
                <= (100 - CALLER_ID_MIN_DROP_PERCENT) * others.successes * caller.trials)
    return Verdict(caller.trials >= CALLER_ID_MIN_DIALS and material and p < CALLER_ID_ALPHA, p)


def impact_score(lower_bound: float, affected_share: float, incident: bool = False) -> float:
    """Conservative expected change in conclusion rate: bound x affected share / 5 points, clipped to [0, 1]."""
    score = min(max(lower_bound * affected_share / IMPACT_FULL_SCALE, 0.0), 1.0)
    return max(score, INCIDENT_IMPACT_FLOOR) if incident else score
