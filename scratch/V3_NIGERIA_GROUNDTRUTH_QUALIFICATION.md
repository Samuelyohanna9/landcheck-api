# Nigerian Pluvial Ground-Truth Qualification — Running Record (Thread 1)

Testing the FROZEN V3.1 terrain index (`scratch/V3_1_TERRAIN_INDEX_PERMANENT_RECORD.md`) against
genuinely qualified Nigerian pluvial events. **V3.1's formula/weights/normalization are not to be
changed regardless of any event's outcome here** - these events test the hypothesis, they do not
construct it. Priority order: Ondo Town -> Minna -> Lagos inland subset.

## Priority 1: Ondo Town (4 October 2024) — CLOSED

**Classification: HIGH event/mechanism confidence — spatial ground truth unavailable due to
observation timing. Not usable for V3.1 validation.**

### Event/mechanism evidence (all independently verified via live search this session)

- **Event**: heavy downpour began ~3pm Friday, 4 October 2024; "almost all roads in town" affected.
  ~1,000 homes, 25 schools, 20 worship centres, 7,000+ people affected. No fatalities reported.
- **Authoritative confirmation**: NEMA Director-General (Hajia Zubaida Umar) personally conducted an
  on-the-spot assessment of Ondo Town, explicitly citing blocked waterways/drainage channels and
  poor drainage maintenance as contributing causes — a federal disaster agency's own attribution,
  not merely eyewitness reporting.
  [Search coverage: NEMA DG visit reports (NewsPeg, The News Now, Desert Herald, Vanguard,
  ondostate.gov.ng), Businessday NG eyewitness drainage-blockage account.]
- **Mechanism**: predominantly rainfall/drainage. No river, dam, or coastal mechanism found in any
  source searched.
- **Named affected areas** (government + press sources, not individually geocoded): Ita-Nla/Itanla
  Road, Lipakala Road, Oke-Odunwo, Oluwabamikole, Odojomu, Bethlehem, Gani Fawehinmi, Oke-Idera,
  Iranlowo, Ife Garage/St. Andrews, Ife Road, Iluyemi, Odo, Oka, Ijomu, Akure-Ondo Expressway,
  Olorunishola, Fagun, Jilalu, New Town/Gani Street, Yaba Police Station, Ademulegun Road.
- Coordinates used for AOI: Ondo Town centre, 7.088923N, 4.799094E (Wikipedia), 4km buffer.

### Spatial ground-truth attempts (all real, all documented, all negative for feasibility)

1. **Sentinel-1 same-orbit SAR change detection** (`v3_ondo_sentinel1_feasibility.py`) — only 3 S1
   scenes existed within +/-20 days of the event (S1B was decommissioned by this point, leaving only
   S1A's ~12-day revisit). Nearest post-event scene: 2024-10-07, **3.7 days after** the event.
   Resulting "plausible flood" mask: 0.1% of AOI, visually confirmed as pure speckle noise (PNG
   preserved: `v3_ondo_masks/OndoTown_20241004_flood_mask_review.png`). **Conclusion: infeasible, not
   a non-detection** — consistent with a short-duration pluvial flash flood having already receded
   before the only available pass.
2. **Sentinel-2 optical NDWI/MNDWI** (`v3_ondo_sentinel2_feasibility.py`) — checked ACTUAL per-scene
   local cloud fraction over the AOI (via SCL band), not just tile-level metadata. Every scene from
   2024-08-22 through 2024-10-21 had 89-100% local cloud cover over Ondo Town; the closest post-event
   scene (2024-10-06, +2.4 days) was 100% cloud-obscured. First scene with any partial clearing was
   2024-10-26 (+22.4 days, still 72% local cloud) — three weeks post-event, useless for flood
   detection. **Conclusion: infeasible** — Nigeria's October wet season produces persistent
   convective cloud cover exactly when needed. Full scene list preserved:
   `v3_ondo_masks/ondo_s2_scene_list.json`.
3. **Copernicus GFM (Global Flood Monitoring)** — checked via openEO API documentation. Requires
   OIDC authentication; no credentials available in this environment, and registering a new account
   was not attempted (outside session scope). Independently of the auth barrier, GFM's own
   documentation confirms it processes "all incoming Sentinel-1 SAR acquisitions" — i.e. it is built
   from the same S1 archive already checked in (1), which has no scene between 2024-09-25 and
   2024-10-07. **Conclusion: even with access, GFM could not have resolved this specific event** —
   the limitation is physical (no SAR data exists for the needed window), not a processing-method
   limitation GFM's more sophisticated algorithm could work around.
4. **Authoritative map/photo/geolocated product search** — no dedicated flood-extent map, GIS
   product, or geolocated photo set was found for the 4 October event specifically. One
   superficially relevant document exists — a DTM/NEMA/Ondo-SEMA/Red-Cross "Joint Post-Flood
   Situation Report — Ondo State" dated 30-31 December 2024 — but its stated assessment window is
   **3-6 December 2024**, two months after this event. Both ReliefWeb and DTM pages 403-blocked
   WebFetch and a browser-UA curl retry; the underlying PDF could not be located or opened. Given
   the ~2-month gap and the inability to confirm its content, **this report is NOT used** as spatial
   evidence for the 4 October event — using it would risk exactly the date-conflation error flagged
   earlier this session for Akwa Ibom/Uyo. It remains a candidate lead for a *separate*, later
   Ondo-State flood episode if that ever needs its own qualification.

### Verdict

Event and mechanism evidence for 4 October 2024 Ondo Town clears every bar this session has used
(authoritative confirmation, defensible predominantly-pluvial mechanism, no confounding mechanism,
named affected locations). **No usable spatial ground truth exists for this specific event, for a
genuine physical reason (observation timing), not a data-quality or effort failure.** Not used for
V3.1 validation. Kept open in case a future higher-resolution commercial product or a
previously-unfound source surfaces.

### Strategic implication for Thread 1 (per instruction, carried forward)

Sentinel-1's ~6-12 day revisit (now nearer 12 days with only S1A operating) can systematically miss
short-duration urban pluvial flooding that recedes in hours. Future candidate events should be
screened for **either** an already-existing authoritative flood-extent product **or** a
fortuitously-close satellite acquisition, rather than selected purely on the strength of event
documentation quality.

## Priority 2: Minna — UNRESOLVED

**Candidate status: UNRESOLVED.** Event existence at Niger State level confirmed (NIHSA/Lagdo Dam
releases, Niger/Benue river rises, September 2024 riparian-community flooding, 71 locations
assessed 4-8 September 2024 per DTM). But **Minna-specific 2024 location/mechanism is not
independently verified** — do not use for V3.1 validation unless the primary DTM/IOM material
becomes accessible.

### What was found and why it doesn't close the chain

- **DTM/IOM completely inaccessible**: every access pattern tried (HTML report page, direct
  `sites/g/files/...` PDF link, browser-UA curl, WebFetch, Wayback Machine CDX search) returned
  403/blocked or had no archived snapshot. Cannot confirm whether Minna city (vs. purely riparian
  communities elsewhere in Niger State) is even covered by the 28 October-8 November 2024 nationwide
  DTM assessment referenced at the start of this thread.
- **The one detailed Minna urban-flood story found (MYPA bridge/Bosso, Mola bridge/Dutsen Kura
  Hausa, drainage blamed by local officials) is very likely a 2026 event, not 2024** — its own
  article metadata points to a June 2026 publish date, with no independent 2024 date confirmation
  found anywhere. Not used, to avoid exactly the date-conflation risk flagged for Akwa Ibom/Uyo
  earlier in this thread.
- **Even that (likely-2026) story's mechanism is genuinely mixed, not clean pluvial**: washed-out
  bridges imply real watercourse/channel overflow (Minna sits on the Chanchaga River/stream system)
  in addition to drainage failure — a real contamination risk independent of the date problem.
- The clearly-dated September 2024 Niger State flooding found via open sources is explicitly
  riparian/fluvial (Lagdo Dam, Niger/Benue rivers) and covers different communities than Minna's
  urban core — not conflated with Minna here.

### Status

Left open per instruction. Not pursued further unless the primary DTM/IOM material becomes
accessible through some other route.

## Priority 3: Lagos inland subset — IN PROGRESS (event/mechanism qualified, spatial evidence weak/unresolved)

Target: an inland/urban Lagos location where the source supports rainfall + drainage overload
without tidal/coastal dominance, ideally with a usable flood footprint. Generic "Lagos flooded"
evidence is explicitly insufficient - see the standing caution on Lagos's mixed
rainfall+drainage+coastal/tidal interactions.

### Candidate event: Lagos mainland, 28 June 2024

The December 2024 DTM/NEMA/LASEMA/Red Cross assessment (81 locations, 14 LGAs, 275,621 people,
1-6 December 2024) remains inaccessible — same DTM/ReliefWeb 403 wall as Ondo/Minna, confirmed again
this round (direct fetch, browser-UA curl). No LGA-level breakdown could be recovered from secondary
sources either. **Not used** — cannot isolate inland-only locations from it as instructed.

Found instead, independently corroborated across Wikipedia (citing The Punch), search-indexed
Vanguard/Guardian/Premium Times/nigerianbulletin coverage (direct fetch blocked on all of these,
consistent with the rest of this session, but converging search-index summaries were consistent
across independent outlets): a clean, well-documented, clearly-inland event —

- **Date**: heavy rain began Friday 28 June 2024; floodwater described as receding "by Monday"
  (~1 July 2024) — a multi-day event, longer-lived than Ondo's few-hours flash flood.
- **Locations** (Lagos mainland, non-coastal): Gbagada, Mushin, Oshodi, Surulere, Egbeda, Ilupeju,
  Idi-Oro, Iyana Ipaja, Ikorodu Road corridor, Ikeja, Maryland, Ogudu, Agege, Alimosho, Ketu —
  a wide, genuinely inland mainland cluster. (Lekki and Obalende were also named in the same
  reporting wave but excluded here as coastal/lagoon-adjacent, per the standing mechanism-isolation
  goal.)
- **Mechanism**: officials themselves attributed it to "intense rainfall above seasonal norms,
  blocked drains from waste, and infrastructure gaps" — not eyewitness-only.
- **Concrete severity indicator**: floodwater entered Murtala Muhammed International Airport's
  temporary international terminal (departure hall, boarding gates, powerhouse), forcing FAAN to
  cut power and relocate three airlines to Terminal Two — an unusually well-documented, precisely
  geolocatable impact point (airport is in Ikeja, part of the inland cluster).

### Spatial ground-truth attempts

1. **Sentinel-1 same-orbit SAR** — AOI centred on Oshodi (6.5567N, 3.3489E, 6km buffer, covering the
   Mushin/Oshodi/Gbagada/Ikeja/Maryland/Ogudu cluster). Two orbits available: orbit 1 (pre 06-26,
   -1.2d; next same-orbit pass 07-20, +22.8d — unusable) and orbit 95 (pre 06-21, -6.8d; post 07-03,
   **+5.2d**). Ran the +5.2-day same-orbit pair (script inline, not saved as a separate file —
   `scratch/v3_lagos_masks/Lagos_mainland_20240628_flood_mask_review.png`): plausible-flood area
   0.22% of AOI. Visual review shows weak, mostly linear structure (possibly road/rail infrastructure
   change-detection artifacts, not confirmed as water) rather than a coherent extent matching the
   reported multi-neighbourhood flooding. **Treated as inconclusive, not a usable mask** — consistent
   with the same timing problem as Ondo (5.2 days is later than Ondo's already-too-late 3.7 days),
   though not as clean a null as Ondo's pure-speckle result.
2. **Sentinel-2 optical** — checked actual AOI-local cloud fraction (SCL band) for every scene
   -30/+20 days around the event. Every scene from 2024-06-08 through 2024-07-13 is 86-100% locally
   cloud-obscured, including the scene falling exactly on the event date (2024-06-28, 96.4% local
   cloud). Best pre-event reference is 24.6 days before the event (2024-06-03, 16.6% local cloud) —
   usable as a baseline but too far removed in time to pair with any usably-clear post-event scene,
   since none exists. **Infeasible**, same wet-season cloud problem as Ondo.
3. **Authoritative event-specific map/GIS product** — searched specifically for a 28 June 2024 flood
   extent map. Found only general Lagos flood-*susceptibility*/vulnerability-mapping academic
   literature (e.g. a Scientific Reports land-use/flood-risk *model* paper, various
   vulnerability-mapping papers) — these are risk models, not observed extent for this event, and
   mix coastal + inland LGAs in their risk rankings, so not usable here regardless. **No
   event-specific spatial product found.**

### Status

Event/mechanism evidence is strong and clearly inland. Spatial ground truth is currently
**unresolved** for the same reason as Ondo: no satellite pass fell close enough to the event to
reliably catch the water, and no authoritative flood-extent map for this specific date has been
found. The December 2024 81-location DTM assessment remains the richest lead but is blocked at the
source. Awaiting direction on whether to pursue the December event further (if DTM access can be
obtained some other way) or continue searching for a different qualifying Lagos inland event.
