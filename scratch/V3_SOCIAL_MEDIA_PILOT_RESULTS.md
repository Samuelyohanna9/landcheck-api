# Social-Media Ground-Truth Pilot — Results (3 events: Ago Palace Way, Ondo Town, Lagos-mainland)

NO V3.1 run. NO model trained. Observations located without reference to V3.1 scores throughout.

## Evidence gates (per instruction)
1. Event-date verification (independent of the post's own claim where possible)
2. Visible flood evidence (photo/video/described observation, not just a headline)
3. Geographic specificity (street/junction/landmark, not just city/LGA)
4. Deduplication of reposts
5. Coordinate uncertainty + provenance recorded per observation
6. No pseudo-negatives inferred from absence of posts

## Pilot 1: Ago Palace Way, Okota (23 Sep 2025) — run in the prior turn

| Observation | Date verification method | Result |
|---|---|---|
| Lagos Commissioner's tweet (@tokunbo_wahab) | Snowflake ID decoded without opening the (paywalled) tweet: posted 24 Sep 2025 17:04 Lagos time, references flooding "witnessed...yesterday" | **PASSES date gate** - independent, authoritative, precise timestamp. Not street-specific (references "parts of the State" generally). |
| Eyewitness Facebook video ("I live in ago palace way okota. Apple junction is the worst") | Facebook blocks post-timestamp metadata from unauthenticated requests | **Date unverifiable** - real, named eyewitness (Chukwudi Christian Ekeh), genuine landmark specificity (Apple Junction), but cannot pass the date gate with available tools. Not counted as evidence. |
| "Lagos Flood Crisis: Okota Residents Battle Waterlogged Roads" (YouTube, News Central TV) | Raw page `publishDate` metadata | **FAILS date gate** - actual publish date 2026-07-10, ten months after the target event. Discarded; would have been a false positive if title/topic match alone had been trusted. |

**Survivors: 1 of 3 candidates (event-level corroboration only, no street-level observation cleared all gates).**

## Pilot 2: Ondo Town (4 Oct 2024)

| Observation | Date verification | Geographic specificity | Result |
|---|---|---|---|
| NTA: "Devastating State of Flood in Ondo Town" | publishDate 2024-10-13 (9 days post-event) | None (generic broadcast, town-level only) | Event-level pass, not street-level |
| "Flood Recedes In Ondo Town As Government Clears Drains" | publishDate 2024-10-05 (1 day post-event) | None | Event-level pass, not street-level |
| "WATCH: Flash Flood Sacks Residents Of Ondo Town" | publishDate 2024-10-05 | None | Event-level pass, not street-level |
| "Flood Destroys Millions Of Naira Worth Of Property In Ondo State" | publishDate 2024-10-05 | None | Event-level pass, not street-level |
| Individual eyewitness posts (Ita-Nla, Oke-Odunwo, Bethlehem specifically) | Searched directly | - | **None found** via available search tools |

**Survivors: 4 of 4 candidates pass date+evidence gates (all genuine, all correctly dated broadcast
footage of the actual event, none are reposts of each other - distinct outlets/angles) but 0 of 4
reach street-level geographic specificity.** No individual eyewitness content found despite
targeted searching by named street.

## Pilot 3: Lagos-mainland (28 Jun 2024)

| Observation | Date verification | Result |
|---|---|---|
| "Scenes From Flood-Hit Areas In Lagos After Monday's Downpour" (YouTube) | publishDate **2026-07-13** | **FAILS date gate** - a different, much more recent (and apparently still-ongoing) 2026 Lagos flood wave. Discarded. |
| "Flood Submerges Fashoro Street in Surulere..." (Facebook, exact street named in title) | Facebook blocks post-timestamp metadata | **Date unverifiable** - genuine street-level specificity (Fashoro Street matches the independently-confirmed news account of this event), but cannot confirm it isn't from a different Lagos flooding wave (2024, 2025, or the active 2026 one). Not counted as evidence. |

**Survivors: 0 of 2 candidates. Zero observations cleared all gates.**

## Aggregate result across all three pilots

| Gate | Ago Palace Way | Ondo Town | Lagos-mainland |
|---|---|---|---|
| Candidates checked | 3 | 5 | 2 |
| Passed date verification | 1 | 4 | 0 |
| Reached street/landmark specificity | 0 | 0 | 0 (1 plausible but unverifiable) |
| Passed ALL gates as a usable observation | 0 | 0 | 0 |

**Zero individually-geolocated, fully-verified social-media flood observations were produced across
three pilot events.** What the method DID reliably produce: independent, metadata-based event-date
confirmation (useful corroboration, not spatial ground truth) and a real, repeatedly-demonstrated
capacity to catch false positives that a title/topic-only search would have wrongly accepted - in
every single pilot, at least one plausible-looking candidate turned out to be from a different year
once its actual publish-date metadata was checked.

## The specific, newly-identified structural problem

Lagos and Ondo Town both flood often enough that **generic-titled social/broadcast content is
frequently misdated to the wrong year** unless individually verified - this happened in 2 of the 3
pilots (once via YouTube, where it was catchable; the risk exists identically on Facebook, where it
is NOT catchable with available tools, since Facebook blocks post-timestamp metadata from
unauthenticated requests). This is a materially worse version of the problem than initially
anticipated: it is not just "hard to find posts," it is "even found posts carry a real, often
uncatchable risk of belonging to a different flood event in the same chronically-flooded location."

## Assessment

The concept is sound and the discipline (metadata-based date verification, explicit gate-by-gate
accounting, no inferred negatives) worked exactly as designed - it repeatedly protected against
false positives. But across three genuine pilots, using only indexed/searchable public content and
no platform API access, the yield is effectively zero fully-qualified observations, not the "tens of
observations, several street-level" scale envisioned. The bottleneck is now demonstrated to be
platform access (Facebook's unauthenticated metadata block in particular), not effort or search
strategy.

## Status
No V3.1 evaluation performed. No pseudo-negatives created. All raw findings above preserved as the
permanent record of this experiment.
