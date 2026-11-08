# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.367 | +7.5% | 0.258 | +5.5% | 0.497 | +0.9% | 4% | 62 (57) |
| nw_kyc | 0.395 | +7.6% | 0.262 | +8.2% | 0.590 | -1.2% | 0% | 21 (21) |
| nw_renewal | 0.467 | +12.3% | 0.281 | +6.4% | 0.565 | +4.3% | 13% | 64 (54) |
| ALL | 0.406 | +9.3% | 0.267 | +6.3% | 0.539 | +1.8% | 7% | 147 (132) |

## Changes applied by the controller

16 applied — neutral: 4, beneficial: 10, harmful: 2

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.018 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.003 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.042 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.040 → **beneficial**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.039 → **harmful**
- ks_cancel v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.030 → **beneficial**
- nw_renewal v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.042 → **beneficial**
- ks_cancel v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.124 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.117 → **beneficial**
- ks_cancel v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.014 didΔ=+0.000 → **harmful**
- nw_renewal v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**
- nw_renewal v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.003 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 14,
  "decisions": {
    "approved": 12,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 12,
  "auto_applied": 4,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 3,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.5,
    "4": 0.496,
    "5": 0.494,
    "6": 0.338,
    "7": 0.241
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0