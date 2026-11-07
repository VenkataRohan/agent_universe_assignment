# Customer-facing dimensions always go through approval

The controller never auto-applies a change to `welcome_message`, `voice`, `prompt_version` or `cohorts`, even where a workflow's policy would allow it (e.g. `ks_cancel` permits auto-apply on welcome and voice within `max_shift` 0.2). Auto-apply is used only for operational dimensions (`schedule`, `caller_id`) and only where policy permits. Northwind Compliance requires a named human approval for anything a customer hears. Kestrel's supervisor asks to be told before anything changes on a running campaign. The CTO values explainable changes over bigger ones. The cost is slower experiment starts on workflows that would have allowed auto-apply. That cost is accepted.

## Consequences

- A future engineer reading `ks_cancel`'s policy will expect auto-apply on voice and welcome. This ADR is why it's not used.
- Approval budget (`max_requests_per_24h`, tightened by the client preference overlay) becomes the scarce resource for experiments, so changes must be batched into as few requests as possible.
- A tenant whose preference overlay says "notify before change" (Kestrel) gets no auto-apply at all, operational dimensions included. Its changes are batched into its approval requests. The one exception is moving weight off a degraded caller ID, which is incident response and is labelled as such on the tenant's status view. Other tenants' auto-applied operational changes are listed in a weekly summary (Northwind Compliance memo, point 1).
