"""Phase 4a: FLUVIAL-specific blind validation location set - LOCKED BEFORE ANY PREDICTION IS RUN.

Tests the River (GloFAS) and Floodplain (HAND) branches specifically against precise, documented
river-flood locations and MATCHED non-flooded controls (elevated/interfluve towns in the same
broader region and climate zone, not floodplain settlements - avoiding the Phase 3 "dry Sahel city
vs rural river-floodplain LGA" mismatch by keeping both groups in comparable humid/sub-humid
southern and middle-belt Nigeria rather than contrasting climate zones).

Excludes all 40 locations previously used in V1/V2 development, diagnosis, or the failed Phase 3
blind validation (the original 19-site study, the 7 real-parcel-diagnostic locations, Malam
Madori, and all 20 Phase 3 locations).

DO NOT EDIT AFTER predictions have been run.

FLUVIAL (8) - documented river-floodplain LGAs, 2022 Nigeria floods (NEMA-documented) unless noted:
- Guma LGA, Benue State - catastrophic 2022 Benue flooding, major IDP-camp displacement, widely
  reported (BBC/Reuters international coverage).
- Agatu LGA, Benue State - 2022 Benue flooding.
- Ndokwa East LGA, Delta State - 2022 NEMA-documented, Niger floodplain.
- Patani LGA, Delta State - 2022 NEMA-documented, Forcados river floodplain.
- Ahoada West LGA, Rivers State - 2022 Niger Delta flooding.
- Ibi LGA, Taraba State - 2022 Kiri/Lagdo Dam release flooding along River Benue (same event as
  Fufore, Adamawa - already used - but a distinct state/location).
- Nembe LGA, Bayelsa State - 2022 Bayelsa floods.
- Yakurr LGA, Cross River State - river flooding.

CONTROL (8) - non-floodplain, matched by broader region/climate rather than by a different climate
zone entirely:
- Kabba, Kogi State - elevated Yoruba hill town, west of the Niger, away from the Niger/Benue
  confluence floodplain LGAs.
- Gboko, Benue State - elevated town away from the immediate River Benue floodplain, matched to
  Guma/Agatu (same state).
- Agbor, Delta State - inland elevated town, matched to Ndokwa East/Patani (same state).
- Owerri, Imo State - inland, southeastern rainforest zone, same broader climate as the Delta/
  Rivers/Bayelsa flooded sites.
- Etche, Rivers State - elevated relative to the core Niger Delta floodplain, matched to Ahoada
  West (same state).
- Bali, Taraba State - lowland/piedmont town distinct from both the River Benue floodplain and
  from Mambilla Plateau (already used), matched to Ibi (same state).
- Aba, Abia State - inland, southeastern rainforest zone, matched to Nembe's broader climate.
- Ogoja, Cross River State - elevated inland town, matched to Yakurr (same state).

Run: python scratch/phase4_fluvial_locations.py   (prints the locked list only)
"""

LOCATIONS = [
    # --- FLUVIAL FLOODED --------------------------------------------------------------------------
    {"name": "Guma LGA (Benue State)", "lat": 7.9500, "lon": 8.6667, "group": "flooded",
     "event_date": "2022", "source": "2022 Nigeria floods (NEMA); major IDP-camp displacement, BBC/Reuters",
     "source_url": "https://floodlist.com/africa/nigeria-floods-2022", "precision": "LGA HQ (Gbajimba) town-centroid estimate"},
    {"name": "Agatu LGA (Benue State)", "lat": 7.3167, "lon": 7.9500, "group": "flooded",
     "event_date": "2022", "source": "2022 Nigeria floods (NEMA)",
     "source_url": "https://floodlist.com/africa/nigeria-floods-2022", "precision": "LGA HQ (Obagaji) town-centroid estimate"},
    {"name": "Ndokwa East LGA (Delta State)", "lat": 5.6500, "lon": 6.5667, "group": "flooded",
     "event_date": "2022", "source": "2022 Nigeria floods (NEMA), River Niger floodplain",
     "source_url": "https://floodlist.com/africa/nigeria-floods-2022", "precision": "LGA HQ (Aboh) town-centroid estimate"},
    {"name": "Patani LGA (Delta State)", "lat": 5.4167, "lon": 6.0167, "group": "flooded",
     "event_date": "2022", "source": "2022 Nigeria floods (NEMA), Forcados river floodplain",
     "source_url": "https://floodlist.com/africa/nigeria-floods-2022", "precision": "LGA HQ town-centroid estimate"},
    {"name": "Ahoada West LGA (Rivers State)", "lat": 4.9500, "lon": 6.6167, "group": "flooded",
     "event_date": "2022", "source": "2022 Nigeria floods (NEMA), Niger Delta",
     "source_url": "https://floodlist.com/africa/nigeria-floods-2022", "precision": "LGA HQ (Akinima) town-centroid estimate"},
    {"name": "Ibi LGA (Taraba State)", "lat": 8.1833, "lon": 9.7500, "group": "flooded",
     "event_date": "2022", "source": "2022 Kiri/Lagdo Dam release flooding along River Benue",
     "source_url": "https://www.icirnigeria.org/lagdo-dam-not-entirely-responsible-for-2022-flooding-nema/", "precision": "Town-centroid estimate"},
    {"name": "Nembe LGA (Bayelsa State)", "lat": 4.5333, "lon": 6.4000, "group": "flooded",
     "event_date": "2022", "source": "2022 Bayelsa State floods",
     "source_url": "https://en.wikipedia.org/wiki/2022_Bayelsa_State_floods", "precision": "Town-centroid estimate"},
    {"name": "Yakurr LGA (Cross River State)", "lat": 5.7833, "lon": 8.0833, "group": "flooded",
     "event_date": "2022", "source": "2022 Nigeria floods (NEMA), river flooding",
     "source_url": "https://floodlist.com/africa/nigeria-floods-2022", "precision": "LGA HQ (Ugep) town-centroid estimate"},
    # --- FLUVIAL CONTROL ----------------------------------------------------------------------------
    {"name": "Kabba (Kogi State)", "lat": 7.8333, "lon": 6.0667, "group": "control",
     "event_date": "n/a", "source": "Elevated hill town, west of the Niger, no documented flood history",
     "source_url": "", "precision": "Town-centroid estimate; geographic basis is general knowledge, not a cited source"},
    {"name": "Gboko (Benue State)", "lat": 7.3167, "lon": 9.0000, "group": "control",
     "event_date": "n/a", "source": "Elevated town away from the immediate River Benue floodplain, matched to Guma/Agatu",
     "source_url": "", "precision": "Town-centroid estimate; geographic basis is general knowledge, not a cited source"},
    {"name": "Agbor (Delta State)", "lat": 6.2500, "lon": 6.2000, "group": "control",
     "event_date": "n/a", "source": "Inland elevated town, matched to Ndokwa East/Patani",
     "source_url": "", "precision": "Town-centroid estimate; geographic basis is general knowledge, not a cited source"},
    {"name": "Owerri (Imo State)", "lat": 5.4833, "lon": 7.0333, "group": "control",
     "event_date": "n/a", "source": "Inland southeastern rainforest-zone city, no strong flood-history reputation",
     "source_url": "", "precision": "City-centroid estimate; geographic basis is general knowledge, not a cited source"},
    {"name": "Etche (Rivers State)", "lat": 5.1000, "lon": 7.0167, "group": "control",
     "event_date": "n/a", "source": "Elevated relative to the core Niger Delta floodplain, matched to Ahoada West",
     "source_url": "", "precision": "Town-centroid estimate; geographic basis is general knowledge, not a cited source"},
    {"name": "Bali (Taraba State)", "lat": 7.8500, "lon": 10.9833, "group": "control",
     "event_date": "n/a", "source": "Lowland/piedmont town distinct from the River Benue floodplain, matched to Ibi",
     "source_url": "", "precision": "Town-centroid estimate; geographic basis is general knowledge, not a cited source"},
    {"name": "Aba (Abia State)", "lat": 5.1167, "lon": 7.3667, "group": "control",
     "event_date": "n/a", "source": "Inland southeastern rainforest-zone city, matched to Nembe's broader climate",
     "source_url": "", "precision": "City-centroid estimate; geographic basis is general knowledge, not a cited source"},
    {"name": "Ogoja (Cross River State)", "lat": 6.6667, "lon": 8.8000, "group": "control",
     "event_date": "n/a", "source": "Elevated inland town, matched to Yakurr",
     "source_url": "", "precision": "Town-centroid estimate; geographic basis is general knowledge, not a cited source"},
]


if __name__ == "__main__":
    flooded = [l for l in LOCATIONS if l["group"] == "flooded"]
    control = [l for l in LOCATIONS if l["group"] == "control"]
    print(f"Fluvial validation set locked: {len(flooded)} flooded, {len(control)} control, {len(LOCATIONS)} total\n")
    for l in LOCATIONS:
        print(f"  [{l['group']}] {l['name']} ({l['lat']}, {l['lon']}) - {l['source']}")
