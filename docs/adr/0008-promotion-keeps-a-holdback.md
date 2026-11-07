# Promotion keeps a 20% holdback on control

When a winner is promoted, control keeps 20% of traffic until the winner has gone 7 days without a decay signal. Without a counterfactual, a winner that stops winning is indistinguishable from a bad week, so decay detection becomes guesswork. With a holdback it's a randomised test: a winner is rolled back when a rolling 3-day one-sided test shows it worse than the holdback at α 0.05. The cost is about 20% of the lift during the holdback period. A reader will see a "winner" at only 80% and think promotion is unfinished. It's deliberate.
