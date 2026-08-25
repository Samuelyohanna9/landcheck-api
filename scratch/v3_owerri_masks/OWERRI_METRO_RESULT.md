# Owerri Metro storm event (NGA_2021_08_18_OwerriMetro) — Final status: SUGGESTIVE / UNCONFIRMED

**Not used for V3.1 evaluation.**

## Event evidence
Contemporary reporting: prolonged rainfall submerged major streets in Owerri metropolis around
17-18 August 2021, naming Item Street (Ikenegbu), Works Layout, Akwakuma, and Ihiagwa - one storm
event, four named locations (kept statistically grouped, never split into separate events).

## Satellite timing
Best-ever timing found this session: same-orbit post-event scenes at +1.2d (orbit 22) and +1.7d
(orbit 30), both on 2021-08-19.

## Why it's unconfirmed despite excellent timing
The two same-orbit pairs disagree substantially:

| | Orbit 22 (05:22 UTC, ~1.2d post-event) | Orbit 30 (17:45 UTC, ~1.7d post-event) |
|---|---|---|
| Whole-AOI plausible flood | 0.05% | 3.18% |
| Item St/Works Layout (600m) | 0.00% | 7.84% |
| Akwakuma (600m) | 0.00% | 11.52% |
| Ihiagwa/FUTO (600m) | 0.36% | 3.37% |
| Visual pattern | Faint single meandering line (likely the real stream channel, unremarkable) | Dense dendritic branching across the whole tributary network - ambiguous |

CHIRPS confirms 19.7mm of rain fell on 19 August itself - the same calendar day as BOTH passes,
~12.5 hours apart (dawn vs evening). West African convective rain is typically afternoon-driven, so
the evening pass (orbit 30) plausibly sampled a materially different hydrometeorological state than
the dawn pass (orbit 22) - not because one measurement is "wrong," but because a real rain event
likely occurred *between* them. This means orbit 30's dendritic signal cannot be cleanly attributed
to the reported 17-18 August event specifically - it could equally be standing water from that event,
fresh wetting from the 19 August rain, or vegetation/soil-moisture backscatter change from that rain
falling on canopy. Investigating a rain-free-day alternative pass would not resolve this - it would
only tell us whether the dendritic signal persists, not which of the three explanations produced it.
**Decision: do not spend further effort trying to disambiguate this specific event window.**

## Permanent methodological lesson (applies to all future Nigerian SAR ground-truth work)

**Same-day rainfall between SAR acquisitions can materially confound pluvial flood change-detection,
even when satellite timing is otherwise excellent.** A same-orbit pair only 12-24 hours apart is not
automatically clean - CHIRPS (or an equivalent rainfall record) should be checked for BOTH the pre
and post acquisition windows before treating any mask as representing a single, specific reported
event. This is a new, generalizable finding from this session, distinct from (and in addition to)
the earlier lesson that transient pluvial floods can recede entirely before ANY same-orbit pass is
available (Ondo, Lagos).
