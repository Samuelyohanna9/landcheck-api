# Systematic UNOSAT + GDACS/JRC Nigeria Flood Product Enumeration

Complete inventory of every UNOSAT Nigeria flood-water product findable via HDX's CKAN API
(`data.humdata.org/api/3/action/package_search`, which bypasses the frontend's bot-block entirely -
a real, reusable access route discovered this session), cross-referenced against GDACS/JRC's own
infrastructure. NO V3.1 calculation performed. NO model trained. Provenance established before
anything is called ground truth, per instruction.

## GDACS/JRC (event 1102720, the cumulative Jun-Sep 2024 Nigeria flood)

| Layer | Source | Verdict |
|---|---|---|
| `Shape_1102720_*.zip` / `dynamic_map_event` | **GLOFAS** (confirmed via explicit `"source":"GLOFAS"` field in the actual GeoJSON) | Modeled forecast extent, NOT observed. Rejected. |
| "Observed flood Overall" (GFM/Sentinel-1) | JRC ArcGIS `gdacs_flood/MapServer` | **Service not started** (HTTP 500, backend confirmed offline) - both this route and Copernicus GFM's own OIDC-walled API are currently inaccessible. Genuine infrastructure gap, not a search failure. |
| Report-page embedded sitreps | ECHO/OCHA text | Real, dated: Jigawa (24 Aug, 33 dead/44,000 displaced), Adamawa (23 Aug), and explicitly **"floods and dam overflow"** by 21 Sep - confirms the event's dominant later phase is fluvial/dam-related, not pluvial. |

## UNOSAT (16 Nigeria-tagged products found via `groups:nga AND organization:unosat`; 2 are IDP-shelter, non-flood, excluded below)

| Product | Date | Sensor/Res. | Geometry | Feature count | Permanent water distinguishable? | Mechanism (geographic/naming inference only - NOT yet independently verified) |
|---|---|---|---|---|---|---|
| Gujba LGA, Yobe | 21 Jul 2022 | **Sentinel-1** (~10-20m, SAR-observed) | Polygon, LGA-scale extent (~20km bbox) | 1 (dissolved multipolygon) | Not confirmed from schema alone | Yobe/Komadugu-Yobe basin - fluvial-leaning by geography, unverified |
| Gashua, Yobe (part of the "Komadugu Gana River" activation) | 13 Aug 2024 | **Pléiades (PHR)**, sub-meter/very high-res, real optical-classified water | Polygon, ~26km x 18km extent | 1 (dissolved) | Schema has `Water_Clas`/`Confidence`/`FieldValid` (all "not yet field validated") | River explicitly named in the parent product title - fluvial |
| Bursari State, Yobe | 15 Aug 2024 | **Pléiades (PHR)**, same as above | Polygon | 1 | Same schema, unvalidated | Same - Komadugu Gana River, fluvial |
| Dikwa Town, Borno | 9 & 19 Sep 2024 | **Sentinel-2** (~10m) + VIIRS (375m) bundled in same archive | Polygon + `PotentiallyAffectedRoad`/`PotentiallyAffectedStructures` layers present | Not fully inspected (large file) | VIIRS layer has explicit `PermanentWater` in the Oct-2022 sibling product (see below) - likely same convention | Not yet independently verified - part of the same Sept 2024 dam-overflow-associated national event per GDACS sitrep |
| Nationwide (multiple: 5-9 Sep, 9-13 Nov 2024, three Oct 2022 windows) | Various 2022/2024 | **VIIRS only**, 375m | Polygon (`MaximumFloodExtent`, `AnalysisExtent`, `CloudObstruction`) | - | **Yes** - Oct-2022 product has an explicit separate `PermanentWater_Nigeria.shp` layer | Too coarse for parcel/neighbourhood validation - **rejected per your own standing instruction**, catalogued only |
| Mokwa Town, Niger State | 2 Jun 2025 (post-event; event 29 May) | **GeoEye-1** (sub-meter commercial) + Sentinel-2 pre-event water check | `FloodExtent` polygon + **`BuildingDamageAssessment` POINT layer, 669 individual buildings** | 669 points | N/A (building points, not water polygon) | **Confirmed dam-related/mixed** (rainfall + dam collapse + railway embankment failure, per independently-verified news) - explicitly NOT pluvial, catalogue for floodplain/fluvial branch only |
| Adamawa/Borno/Gombe/Taraba/Yobe (5 states) | 29 Jul 2025 | **Sentinel-2** (~10m) | `FloodExtent`/`WaterExtent`/`CloudObstruction`, Nigeria-wide extent within the 5 states | - | Not yet confirmed | Not yet independently verified per-state/per-LGA - regional NE Nigeria, fluvial-leaning by history |

## The standout technical finding: Mokwa's 669-point building-damage layer

This is real, individually-geolocated observed-impact data - not an aggregate count. Attribute
schema: `Main_Dmg` (Damage / Possible damage), `Grouped_Da` (Damaged Buildings), `Confidence` ("To
Be Evaluated" for every record checked), `FieldValid` ("Not yet field validated" for every record
checked). **Important limitation for the exact test you proposed**: every one of the ~15 records
inspected is classified as damaged - this appears to be a **positive-only** layer (flagged/damaged
buildings), not a full building census with damaged-vs-undamaged status. Testing "does V3.1 rank
affected buildings higher than nearby unaffected ones" would require a separate building-footprint
source (e.g. OSM) to supply candidate negatives, with the same "absence from the damage layer is
not proof of non-damage" caution you've already flagged applying here too. Moot for pluvial
validation regardless, since Mokwa's mechanism is independently confirmed dam-related/mixed.

## Net assessment against the actual target

**Every genuinely observed, higher-resolution (non-VIIRS) UNOSAT Nigeria product found is
either explicitly river-named (Komadugu Gana River) or independently confirmed dam-related (Mokwa)
- none is yet a defensible pluvial/urban-drainage candidate.** This mirrors the pattern already
established for GDACS's own sitrep text. The systematic enumeration itself is complete and
reusable (the CKAN API route works for any future UNOSAT/HDX search); what it has not yet produced
is the specific intersection required: observed spatial geometry + known date/location + defensible
pluvial mechanism.

## Preserved artifacts
All shapefiles downloaded and inspected in
`scratchpad/GDACS_Nigeria/unosat/` (7 products, ogrinfo-verified geometry/schema for the
higher-resolution ones). HDX CKAN API query pattern documented above for reuse.

## Status
No prior-use exclusions violated. No V3.1 evaluation performed. Not yet done: cross-referencing
the African flood-monitoring dataset (156,000+ records) for additional event-discovery leads, and
full independent mechanism verification (news/agency sourcing) for the Gujba/Gashua/Bursari/Dikwa/
5-state events beyond the geographic/naming inference above.
