# Experiment dimensions follow the primary KPI

The controller only experiments on dimensions that can move a workflow's primary KPI. Welcome message and voice are heard after pickup, so on a pickup-primary workflow (e.g. `nw_kyc`) they are never experimented on, even though engagement gains would lift the conclusion rate. There, scarce approvals go to caller ID and call time. The README tells us to optimise the primary KPI. Experimenting on those dimensions would mean judging winners on the guardrail, and the approval cap (2/day on `nw_kyc`) can't cover both.
