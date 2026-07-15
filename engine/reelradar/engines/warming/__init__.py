"""Account-warming engine (warming PRD §4).

The ONLY engine that performs deliberate light writes (C1). Harvest engines stay
read-only; warming ramps a cold account to harvest-ready and sustains it. In P0
this package is dwell-only — it observes the home feed under human pacing and
emits ZERO write actions (the ramp budget gates writes off until P1).
"""
