"""
CO2 sequestration estimation engine for LandCheck Green.

Uses IPCC Tier 1 defaults with species-specific growth curves based on:
- Chave et al. (2014) pantropical allometric equations
- IPCC Good Practice Guidance default values
- FAO/GlobAllomeTree species parameters

Growth model: Chapman-Richards sigmoid
    DBH(t) = DBH_max * (1 - exp(-k * t))^p

Biomass chain:
    AGB -> BGB (root:shoot) -> Total Biomass -> Carbon (*0.47) -> CO2 (*3.667)
"""

import math
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# Species carbon lookup table
# Sources: IPCC defaults, GlobAllomeTree, FAO tropical species references
#
# Fields:
#   growth_class: fast / medium / slow
#   wood_density: g/cm3 (specific gravity)
#   max_dbh_cm: maximum diameter at breast height
#   max_height_m: maximum tree height
#   growth_k: Chapman-Richards growth rate parameter
#   growth_p: Chapman-Richards shape parameter
#   root_shoot_ratio: belowground/aboveground biomass ratio
#   carbon_fraction: proportion of biomass that is carbon
# ---------------------------------------------------------------------------

SPECIES_CARBON_DB: dict[str, dict] = {
    # --- Fast-growing tropical ---
    "eucalyptus": {
        "label": "Eucalyptus (general)",
        "growth_class": "fast",
        "wood_density": 0.52,
        "max_dbh_cm": 60,
        "max_height_m": 35,
        "growth_k": 0.12,
        "growth_p": 1.2,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "acacia_mangium": {
        "label": "Acacia mangium",
        "growth_class": "fast",
        "wood_density": 0.46,
        "max_dbh_cm": 50,
        "max_height_m": 30,
        "growth_k": 0.14,
        "growth_p": 1.1,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "gmelina_arborea": {
        "label": "Gmelina arborea",
        "growth_class": "fast",
        "wood_density": 0.42,
        "max_dbh_cm": 55,
        "max_height_m": 30,
        "growth_k": 0.13,
        "growth_p": 1.15,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "leucaena": {
        "label": "Leucaena leucocephala",
        "growth_class": "fast",
        "wood_density": 0.54,
        "max_dbh_cm": 35,
        "max_height_m": 20,
        "growth_k": 0.15,
        "growth_p": 1.1,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
    "moringa": {
        "label": "Moringa oleifera",
        "growth_class": "fast",
        "wood_density": 0.35,
        "max_dbh_cm": 30,
        "max_height_m": 12,
        "growth_k": 0.18,
        "growth_p": 1.0,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
    # --- Medium-growth tropical ---
    "teak": {
        "label": "Tectona grandis (Teak)",
        "growth_class": "medium",
        "wood_density": 0.55,
        "max_dbh_cm": 55,
        "max_height_m": 35,
        "growth_k": 0.08,
        "growth_p": 1.3,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "grevillea": {
        "label": "Grevillea robusta",
        "growth_class": "medium",
        "wood_density": 0.51,
        "max_dbh_cm": 50,
        "max_height_m": 30,
        "growth_k": 0.09,
        "growth_p": 1.2,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "neem": {
        "label": "Azadirachta indica (Neem)",
        "growth_class": "medium",
        "wood_density": 0.58,
        "max_dbh_cm": 45,
        "max_height_m": 20,
        "growth_k": 0.07,
        "growth_p": 1.2,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
    "cashew": {
        "label": "Anacardium occidentale (Cashew)",
        "growth_class": "medium",
        "wood_density": 0.40,
        "max_dbh_cm": 40,
        "max_height_m": 14,
        "growth_k": 0.08,
        "growth_p": 1.1,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
    "casuarina": {
        "label": "Casuarina equisetifolia",
        "growth_class": "medium",
        "wood_density": 0.60,
        "max_dbh_cm": 40,
        "max_height_m": 25,
        "growth_k": 0.10,
        "growth_p": 1.2,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    # --- Fruit trees ---
    "mango": {
        "label": "Mangifera indica (Mango)",
        "growth_class": "medium",
        "wood_density": 0.52,
        "max_dbh_cm": 50,
        "max_height_m": 25,
        "growth_k": 0.07,
        "growth_p": 1.2,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
    "avocado": {
        "label": "Persea americana (Avocado)",
        "growth_class": "medium",
        "wood_density": 0.42,
        "max_dbh_cm": 40,
        "max_height_m": 20,
        "growth_k": 0.08,
        "growth_p": 1.15,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
    "orange": {
        "label": "Citrus sinensis (Orange)",
        "growth_class": "slow",
        "wood_density": 0.60,
        "max_dbh_cm": 25,
        "max_height_m": 10,
        "growth_k": 0.07,
        "growth_p": 1.1,
        "root_shoot_ratio": 0.30,
        "carbon_fraction": 0.47,
    },
    "oil_palm": {
        "label": "Elaeis guineensis (Oil Palm)",
        "growth_class": "medium",
        "wood_density": 0.30,
        "max_dbh_cm": 50,
        "max_height_m": 20,
        "growth_k": 0.09,
        "growth_p": 1.0,
        "root_shoot_ratio": 0.20,
        "carbon_fraction": 0.47,
    },
    "coconut": {
        "label": "Cocos nucifera (Coconut)",
        "growth_class": "medium",
        "wood_density": 0.35,
        "max_dbh_cm": 35,
        "max_height_m": 25,
        "growth_k": 0.08,
        "growth_p": 1.0,
        "root_shoot_ratio": 0.20,
        "carbon_fraction": 0.47,
    },
    # --- Slow-growth hardwoods ---
    "mahogany": {
        "label": "Swietenia / Khaya (Mahogany)",
        "growth_class": "slow",
        "wood_density": 0.50,
        "max_dbh_cm": 70,
        "max_height_m": 35,
        "growth_k": 0.04,
        "growth_p": 1.4,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "iroko": {
        "label": "Milicia excelsa (Iroko)",
        "growth_class": "slow",
        "wood_density": 0.55,
        "max_dbh_cm": 80,
        "max_height_m": 40,
        "growth_k": 0.035,
        "growth_p": 1.4,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "obeche": {
        "label": "Triplochiton scleroxylon (Obeche)",
        "growth_class": "medium",
        "wood_density": 0.32,
        "max_dbh_cm": 70,
        "max_height_m": 40,
        "growth_k": 0.06,
        "growth_p": 1.3,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "shea": {
        "label": "Vitellaria paradoxa (Shea)",
        "growth_class": "slow",
        "wood_density": 0.65,
        "max_dbh_cm": 40,
        "max_height_m": 15,
        "growth_k": 0.03,
        "growth_p": 1.3,
        "root_shoot_ratio": 0.30,
        "carbon_fraction": 0.47,
    },
    "baobab": {
        "label": "Adansonia digitata (Baobab)",
        "growth_class": "slow",
        "wood_density": 0.20,
        "max_dbh_cm": 200,
        "max_height_m": 25,
        "growth_k": 0.02,
        "growth_p": 1.5,
        "root_shoot_ratio": 0.30,
        "carbon_fraction": 0.47,
    },
    # --- Fallback defaults by growth class ---
    "_default_fast": {
        "label": "Fast-growing tropical (default)",
        "growth_class": "fast",
        "wood_density": 0.45,
        "max_dbh_cm": 50,
        "max_height_m": 28,
        "growth_k": 0.12,
        "growth_p": 1.15,
        "root_shoot_ratio": 0.24,
        "carbon_fraction": 0.47,
    },
    "_default_medium": {
        "label": "Medium-growth tropical (default)",
        "growth_class": "medium",
        "wood_density": 0.50,
        "max_dbh_cm": 45,
        "max_height_m": 25,
        "growth_k": 0.08,
        "growth_p": 1.2,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
    "_default_slow": {
        "label": "Slow-growth tropical (default)",
        "growth_class": "slow",
        "wood_density": 0.55,
        "max_dbh_cm": 55,
        "max_height_m": 30,
        "growth_k": 0.04,
        "growth_p": 1.35,
        "root_shoot_ratio": 0.27,
        "carbon_fraction": 0.47,
    },
}

# The overall fallback when species is unknown
DEFAULT_SPECIES_KEY = "_default_medium"


def _normalize_species_key(species: Optional[str]) -> str:
    """Normalize a species name to a lookup key."""
    if not species:
        return DEFAULT_SPECIES_KEY
    key = species.strip().lower().replace(" ", "_").replace("-", "_")
    # Try direct match
    if key in SPECIES_CARBON_DB:
        return key
    # Try partial match (e.g. "eucalyptus grandis" -> "eucalyptus")
    for db_key in SPECIES_CARBON_DB:
        if db_key.startswith("_"):
            continue
        if db_key in key or key in db_key:
            return db_key
    return DEFAULT_SPECIES_KEY


def _get_species_params(species: Optional[str]) -> dict:
    """Get carbon parameters for a species."""
    key = _normalize_species_key(species)
    return SPECIES_CARBON_DB[key]


def project_dbh(params: dict, age_years: float) -> float:
    """Project DBH using Chapman-Richards growth model. Returns cm."""
    if age_years <= 0:
        return 0.0
    dbh_max = params["max_dbh_cm"]
    k = params["growth_k"]
    p = params["growth_p"]
    return dbh_max * (1.0 - math.exp(-k * age_years)) ** p


def project_height(params: dict, dbh_cm: float) -> float:
    """Estimate height from DBH using a simple power relationship. Returns m."""
    if dbh_cm <= 0:
        return 0.0
    max_h = params["max_height_m"]
    max_dbh = params["max_dbh_cm"]
    # H = H_max * (DBH / DBH_max)^0.6  (typical tropical relationship)
    ratio = min(dbh_cm / max_dbh, 1.0)
    return max_h * (ratio ** 0.6)


def calculate_agb_chave(wood_density: float, dbh_cm: float, height_m: float) -> float:
    """
    Aboveground biomass using Chave et al. (2014) pantropical equation.
    AGB (kg) = 0.0673 * (WD * DBH^2 * H)^0.976
    """
    if dbh_cm <= 0 or height_m <= 0:
        return 0.0
    return 0.0673 * (wood_density * dbh_cm ** 2 * height_m) ** 0.976


def estimate_tree_co2_kg(species: Optional[str], age_years: float) -> float:
    """
    Estimate total CO2 stored in a single tree at a given age.
    Returns kg CO2.
    """
    if age_years <= 0:
        return 0.0
    params = _get_species_params(species)
    dbh = project_dbh(params, age_years)
    height = project_height(params, dbh)
    agb = calculate_agb_chave(params["wood_density"], dbh, height)
    bgb = agb * params["root_shoot_ratio"]
    total_biomass = agb + bgb
    carbon_kg = total_biomass * params["carbon_fraction"]
    co2_kg = carbon_kg * (44.0 / 12.0)  # 3.667
    return round(co2_kg, 2)


def estimate_annual_co2_kg(species: Optional[str], age_years: float) -> float:
    """Estimate CO2 sequestered in the most recent year. Returns kg CO2/year."""
    if age_years <= 1:
        return estimate_tree_co2_kg(species, max(age_years, 0.5))
    current = estimate_tree_co2_kg(species, age_years)
    previous = estimate_tree_co2_kg(species, age_years - 1)
    return round(max(current - previous, 0.0), 2)


def estimate_lifetime_co2_kg(species: Optional[str], years: int = 40) -> float:
    """Estimate total CO2 a tree will store over its projected lifetime."""
    return estimate_tree_co2_kg(species, float(years))


def tree_age_years(planting_date: Optional[date], ref_date: Optional[date] = None) -> float:
    """Calculate tree age in fractional years from planting date."""
    if not planting_date:
        return 0.0
    ref = ref_date or date.today()
    delta = ref - planting_date
    return max(delta.days / 365.25, 0.0)


def compute_project_carbon(
    trees: list[dict],
    projection_years: int = 40,
) -> dict:
    """
    Compute carbon summary for a list of tree dicts.
    Each tree dict should have: species, planting_date, status.

    Returns dict with:
      - total_trees, alive_trees
      - current_co2_kg, current_co2_tonnes
      - annual_co2_kg, annual_co2_tonnes
      - projected_lifetime_co2_kg, projected_lifetime_co2_tonnes
      - co2_per_tree_avg_kg
      - top_species: list of {species, count, co2_kg}
    """
    today = date.today()
    alive_statuses = {
        "alive", "healthy", "needs_attention", "pest", "disease",
        "need_watering", "need_protection", "damaged",
        "need_replacement", "needs_replacement",
    }

    total_trees = len(trees)
    alive_trees = 0
    current_co2 = 0.0
    annual_co2 = 0.0
    projected_co2 = 0.0
    species_agg: dict[str, dict] = {}

    for tree in trees:
        status = (tree.get("status") or "alive").lower().strip()
        species = tree.get("species")
        planting_date = tree.get("planting_date")

        # Parse planting_date if string
        if isinstance(planting_date, str):
            try:
                planting_date = date.fromisoformat(planting_date[:10])
            except (ValueError, TypeError):
                planting_date = None

        is_alive = status in alive_statuses
        if is_alive:
            alive_trees += 1

        age = tree_age_years(planting_date, today)

        if is_alive and age > 0:
            tree_co2 = estimate_tree_co2_kg(species, age)
            tree_annual = estimate_annual_co2_kg(species, age)
        elif is_alive:
            tree_co2 = 0.0
            tree_annual = 0.0
        else:
            # Dead/removed trees: count their stored carbon up to now but no future
            tree_co2 = estimate_tree_co2_kg(species, age) if age > 0 else 0.0
            tree_annual = 0.0

        current_co2 += tree_co2
        annual_co2 += tree_annual

        # Projected lifetime only for alive trees
        if is_alive:
            projected_co2 += estimate_lifetime_co2_kg(species, projection_years)

        # Species aggregation
        sp_key = _normalize_species_key(species)
        sp_label = _get_species_params(species).get("label", species or "Unknown")
        if sp_key not in species_agg:
            species_agg[sp_key] = {"species": sp_label, "count": 0, "co2_kg": 0.0}
        species_agg[sp_key]["count"] += 1
        species_agg[sp_key]["co2_kg"] += tree_co2

    top_species = sorted(species_agg.values(), key=lambda x: x["co2_kg"], reverse=True)[:10]
    for sp in top_species:
        sp["co2_kg"] = round(sp["co2_kg"], 1)

    return {
        "total_trees": total_trees,
        "alive_trees": alive_trees,
        "current_co2_kg": round(current_co2, 1),
        "current_co2_tonnes": round(current_co2 / 1000, 2),
        "annual_co2_kg": round(annual_co2, 1),
        "annual_co2_tonnes": round(annual_co2 / 1000, 2),
        "projected_lifetime_co2_kg": round(projected_co2, 1),
        "projected_lifetime_co2_tonnes": round(projected_co2 / 1000, 2),
        "co2_per_tree_avg_kg": round(current_co2 / alive_trees, 1) if alive_trees else 0.0,
        "projection_years": projection_years,
        "methodology": "IPCC Tier 1 + Chave et al. (2014) pantropical allometric equation",
        "top_species": top_species,
    }


def generate_co2_projection_table(
    trees: list[dict],
    years: int = 30,
) -> list[dict]:
    """
    Generate a year-by-year CO2 projection table for reporting.
    Returns list of {year, cumulative_co2_tonnes, annual_co2_tonnes}.
    """
    today = date.today()
    alive_trees_data = []
    alive_statuses = {
        "alive", "healthy", "needs_attention", "pest", "disease",
        "need_watering", "need_protection", "damaged",
    }

    for tree in trees:
        status = (tree.get("status") or "alive").lower().strip()
        if status not in alive_statuses:
            continue
        species = tree.get("species")
        planting_date = tree.get("planting_date")
        if isinstance(planting_date, str):
            try:
                planting_date = date.fromisoformat(planting_date[:10])
            except (ValueError, TypeError):
                planting_date = None
        age = tree_age_years(planting_date, today)
        alive_trees_data.append({"species": species, "current_age": age})

    projection = []
    for yr_offset in range(0, years + 1):
        total_co2 = 0.0
        for tree_data in alive_trees_data:
            future_age = tree_data["current_age"] + yr_offset
            total_co2 += estimate_tree_co2_kg(tree_data["species"], future_age)

        prev_co2 = 0.0
        if yr_offset > 0:
            for tree_data in alive_trees_data:
                prev_age = tree_data["current_age"] + yr_offset - 1
                prev_co2 += estimate_tree_co2_kg(tree_data["species"], prev_age)

        projection.append({
            "year_offset": yr_offset,
            "year": today.year + yr_offset,
            "cumulative_co2_tonnes": round(total_co2 / 1000, 2),
            "annual_co2_tonnes": round((total_co2 - prev_co2) / 1000, 2) if yr_offset > 0 else round(total_co2 / 1000, 2),
        })

    return projection


# Convenience: list all known species for the frontend dropdown
def list_known_species() -> list[dict]:
    """Return list of species available in the carbon database."""
    result = []
    for key, params in SPECIES_CARBON_DB.items():
        if key.startswith("_"):
            continue
        result.append({
            "key": key,
            "label": params["label"],
            "growth_class": params["growth_class"],
        })
    return sorted(result, key=lambda x: x["label"])
