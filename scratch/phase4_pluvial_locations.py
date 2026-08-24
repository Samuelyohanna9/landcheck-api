"""Phase 4b: PLUVIAL-specific blind validation location set - LOCKED BEFORE ANY PREDICTION IS RUN.

Tests the Pluvial (terrain+runoff, rainfall/surface-water) branch specifically against documented
URBAN rainfall/drainage-failure flood locations and MATCHED URBAN controls from comparable
rainfall/land-use settings - directly addressing the Phase 3 finding that the pluvial branch's
prior AUC was invalid because the positive set contained no confirmed pluvial events and the
control set was strongly urbanized, creating a runoff/imperviousness confound that had nothing to
do with the pluvial engine's real discrimination power.

Where possible, flooded/control pairs are SAME-CITY (Lekki vs Ikeja GRA in Lagos; Kubwa vs Wuse 2
in Abuja) - the strongest form of matching, since it holds regional rainfall climatology and city-
level infrastructure context constant and isolates drainage/exposure differences specifically.
Where a same-city pair wasn't available, controls are matched by state/broader climate zone
instead.

Excludes all 40 locations previously used in V1/V2 development, diagnosis, or the failed Phase 3
blind validation, PLUS all 16 locations in the separate phase4_fluvial_locations.py set (a fully
independent dataset, per instruction).

Mechanism honesty: Ilorin's documented flooding involves both urban drainage failure AND the Asa
River channel running through the city (a mixed urban/river mechanism, similar to Ibadan's Ogunpa
corridor already used in earlier development) - flagged here rather than presented as pure pluvial.

DO NOT EDIT AFTER predictions have been run.

Run: python scratch/phase4_pluvial_locations.py   (prints the locked list only)
"""

LOCATIONS = [
    # --- PLUVIAL FLOODED (urban rainfall/drainage-failure) ------------------------------------------
    {"name": "Lekki (Lagos State)", "lat": 6.4650, "lon": 3.5658, "group": "flooded",
     "event_date": "recurrent", "source": "Recurrent urban flash flooding, poor drainage infrastructure, heavy rainfall",
     "source_url": "", "precision": "District-centroid estimate", "mechanism": "pluvial/urban drainage"},
    {"name": "Kubwa (FCT, Abuja)", "lat": 9.1500, "lon": 7.3333, "group": "flooded",
     "event_date": "recurrent", "source": "Recurrent urban flash flooding, unplanned development, inadequate drainage",
     "source_url": "", "precision": "District-centroid estimate", "mechanism": "pluvial/urban drainage"},
    {"name": "Benin City (Edo State)", "lat": 6.3350, "lon": 5.6037, "group": "flooded",
     "event_date": "recurrent", "source": "Recurrent urban flooding, heavy rainfall + inadequate drainage infrastructure",
     "source_url": "", "precision": "City-centroid estimate", "mechanism": "pluvial/urban drainage"},
    {"name": "Uyo (Akwa Ibom State)", "lat": 5.0333, "lon": 7.9167, "group": "flooded",
     "event_date": "recurrent", "source": "Documented urban flooding, heavy rainfall + drainage capacity issues",
     "source_url": "", "precision": "City-centroid estimate", "mechanism": "pluvial/urban drainage"},
    {"name": "Calabar (Cross River State)", "lat": 4.9500, "lon": 8.3167, "group": "flooded",
     "event_date": "recurrent", "source": "Documented urban flooding, heavy rainfall + drainage capacity issues",
     "source_url": "", "precision": "City-centroid estimate", "mechanism": "pluvial/urban drainage"},
    {"name": "Ilorin (Kwara State)", "lat": 8.4966, "lon": 4.5426, "group": "flooded",
     "event_date": "recurrent", "source": "Documented urban flooding - Asa River channel + urban drainage failure",
     "source_url": "", "precision": "City-centroid estimate",
     "mechanism": "mixed urban drainage/river-channel - NOT pure pluvial, disclosed"},
    # --- PLUVIAL CONTROL (urban, matched rainfall/land-use, non-flooded) --------------------------
    {"name": "Ikeja GRA (Lagos State)", "lat": 6.6018, "lon": 3.3515, "group": "control",
     "event_date": "n/a", "source": "Same city as Lekki (matched rainfall regime), planned low-density district with better drainage infrastructure",
     "source_url": "", "precision": "District-centroid estimate"},
    {"name": "Wuse 2 (FCT, Abuja)", "lat": 9.0765, "lon": 7.4898, "group": "control",
     "event_date": "n/a", "source": "Same city as Kubwa (matched rainfall regime), planned central district with better drainage infrastructure",
     "source_url": "", "precision": "District-centroid estimate"},
    {"name": "Akure (Ondo State)", "lat": 7.2500, "lon": 5.2000, "group": "control",
     "event_date": "n/a", "source": "Same humid rainforest climate zone as Benin City, no strong flood-history reputation",
     "source_url": "", "precision": "City-centroid estimate"},
    {"name": "Ado-Ekiti (Ekiti State)", "lat": 7.6167, "lon": 5.2167, "group": "control",
     "event_date": "n/a", "source": "Comparable humid-zone rainfall to Uyo, elevated terrain, no strong flood-history reputation",
     "source_url": "", "precision": "City-centroid estimate"},
    {"name": "Abakaliki (Ebonyi State)", "lat": 6.3167, "lon": 8.1167, "group": "control",
     "event_date": "n/a", "source": "Comparable humid-zone rainfall to Calabar, elevated terrain, no strong flood-history reputation",
     "source_url": "", "precision": "City-centroid estimate"},
    {"name": "Offa (Kwara State)", "lat": 8.1500, "lon": 4.7167, "group": "control",
     "event_date": "n/a", "source": "Same state/climate as Ilorin, no strong flood-history reputation, not on the Asa River channel",
     "source_url": "", "precision": "Town-centroid estimate"},
]


if __name__ == "__main__":
    flooded = [l for l in LOCATIONS if l["group"] == "flooded"]
    control = [l for l in LOCATIONS if l["group"] == "control"]
    print(f"Pluvial validation set locked: {len(flooded)} flooded, {len(control)} control, {len(LOCATIONS)} total\n")
    for l in LOCATIONS:
        print(f"  [{l['group']}] {l['name']} ({l['lat']}, {l['lon']}) - {l['source']}")
