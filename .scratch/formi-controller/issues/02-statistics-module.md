# 02: Statistics module

**What to build:** The pure, I/O-free statistics the whole controller decides with (Seam 2):
- sample size for a two-proportion test
- the one-sided two-proportion z-test
- guardrail non-inferiority (lower bound of the difference against −max_drop)
- the cohort harm check (one-sided α 0.10)
- screen elimination and candidate choice
- the one-sided binomial hallucination detector (rate ≥ 2× baseline and p < 0.001 over at least 150 connects)
- the caller-ID degradation test (at least 15% relative drop, p < 0.001, at least 300 dials)
- decay against the holdback (one-sided α 0.05)
- conservative lower confidence bounds for impact

Every function is deterministic: identical inputs give identical outputs.

Spec: user stories 22–26, 31, 37, 45, 58; ADR-0006, ADR-0008.

**Blocked by:** None (can start immediately)

**Status:** done

- [x] Sample sizes match known reference values (e.g. ≈1,020 per arm for p=0.544, +10% relative, α 0.05 one-sided, power 0.8)
- [x] Each test accepts and rejects correctly on either side of its boundary
- [x] Over many simulated null experiments, each test's false-positive rate is at or below its stated α
- [x] Confirm-stage per-look α is 0.05/3 and the sample size reflects it
- [x] Identical inputs give identical outputs (no randomness)
