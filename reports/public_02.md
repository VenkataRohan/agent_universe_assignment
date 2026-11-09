# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.356 | +51.1% | 0.242 | +33.8% | 0.500 | +6.9% | 34% | 110 (348) |
| nw_kyc | 0.432 | +15.7% | 0.274 | +16.8% | 0.597 | -1.8% | 0% | 26 (29) |
| nw_renewal | 0.477 | +10.2% | 0.291 | +11.0% | 0.546 | -1.5% | 19% | 68 (54) |
| ALL | 0.413 | +24.8% | 0.265 | +20.9% | 0.537 | +0.8% | 25% | 204 (431) |

## Changes applied by the controller

20 applied — neutral: 8, beneficial: 11, harmful: 1

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.018 → **neutral**
- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.015 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.032 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.044 → **beneficial**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.040 → **beneficial**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- ks_cancel v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.031 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.013 → **neutral**
- nw_renewal v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.012 → **neutral**
- ks_cancel v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.069 didΔ=+0.000 → **beneficial**
- ks_cancel v8 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.092 → **beneficial**
- ks_cancel v9 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- nw_renewal v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.039 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.142 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.012 → **neutral**
- ks_cancel v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.023 didΔ=+0.000 → **harmful**
- nw_kyc v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.134 → **beneficial**
- ks_cancel v11 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**

## Governance

```
{
  "approval_requests": 18,
  "decisions": {
    "approved": 15,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 14,
  "auto_applied": 6,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 2,
    "ks_cancel": 3
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.128,
      "3": 0.083,
      "4": 0.0,
      "5": 0.0,
      "6": 0.0,
      "7": 0.0,
      "8": 0.0,
      "9": 0.0
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 1455,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.477,
    "3": 0.501,
    "4": 0.507,
    "5": 0.085,
    "6": 0.0,
    "7": 0.0,
    "8": 0.0,
    "9": 0.0
  }
}
```

Credits used: 3 · validation runs: 1 · LLM fix calls: 0