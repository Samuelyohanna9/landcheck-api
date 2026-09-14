# Estate layout planning basis

LandCheck Estates creates a **concept layout for review**, not a statutory subdivision approval.
The final layout must be checked and signed off by the responsible town planner, surveyor,
engineer and planning authority for the project location.

## Planning basis

- Nigerian planning law assigns physical-development planning and local plan/layout approval to
  the relevant planning bodies. There is no single nationwide plot-size or road-width value that
  is safe to apply to every state, local authority or estate scheme.
- The Lagos State planning regulation is a useful example of the information a professional
  submission should expose: property boundary and dimensions, site orientation, roads and
  drains, landscaping/open areas, and developed/undeveloped percentages.
- UN-Habitat's international guidance supports connected movement networks, accessible public
  space, integrated infrastructure and climate resilience. These principles guide the generated
  concept, but they do not replace local standards.

## Configurable assumptions

The automatic-layout form exposes these values instead of hiding them in code:

| Input | Purpose |
| --- | --- |
| Target plot size | Guides the number and dimensions of candidate residential plots. |
| Road width | Creates connected internal access corridors between plot rows and columns. |
| Edge reserve | Keeps an outer reserve around the estate boundary. |
| Drainage reserve | Records an outer drainage reserve as a visible map layer. |
| Open-space percentage | Reserves central shared/open-space cells and reports the achieved percentage. |
| Plot label prefix | Produces stable plot numbers such as `P-001`. |
| Maximum plots | Prevents an accidental oversized proposal. |

The values are deliberately editable. A jurisdiction-specific planning profile can be added later
once the developer has confirmed the applicable state or area-council standards. The generator
reports the planning CRS, plot count, plot area, road count and open-space percentage so a reviewer
can compare the result with the project brief before approval.

## Checks before approval

The backend rejects invalid or empty boundaries, keeps generated plots as valid non-overlapping
polygons, checks duplicate plot numbers, and stores road/open-space/drainage candidates separately.
Approval is an explicit action. It creates available Estate plots and visible spatial layers but
does not publish the Estate map; the existing Estate map-approval step remains separate.

## References

- [Nigerian Urban and Regional Planning Act, UNEP Law and Environment Assistance Platform](https://leap.unep.org/en/countries/ng/national-legislation/nigerian-urban-and-regional-planning-act-1992)
- [Lagos State Urban and Regional Planning and Development Regulation 2019](https://www.epp.lagosstate.gov.ng/regulations/REVISED_LASPPPA_REGULATION_2019_1.pdf)
- [UN-Habitat International Guidelines on Urban and Territorial Planning](https://unhabitat.org/international-guidelines-on-urban-and-territorial-planning)
