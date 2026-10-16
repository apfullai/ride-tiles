# Bounded top-50 U.S. city tile queue

Prepared 2026-10-14. This is an expansion plan and a deterministic input queue, not a claim that fifty complete cities have been mapped or published.

## Scope and source of the ranking

“Top 50” means rows 1–50 in the U.S. Census Bureau's [Vintage 2025 incorporated-place ranking](https://www.census.gov/data/datasets/time-series/demo/popest/2020s-total-cities-and-towns.html), by estimated resident population on July 1, 2025, released May 2026. It does not mean metropolitan statistical areas, urban areas, counties, fastest-growing places, or an editorial selection of cycling cities. The official balance-area names for Indianapolis, Nashville-Davidson and Louisville/Jefferson County are preserved in the metadata. Census boundaries for these estimates are January 1, 2025; they are not the planning rectangles below.

Primary workbook: [SUB-IP-EST2025-ANNRNK.xlsx](https://www2.census.gov/programs-surveys/popest/tables/2020-2025/cities/totals/SUB-IP-EST2025-ANNRNK.xlsx). A 149 KB local copy is pinned by SHA-256 in `config/rollout-city-metadata.json`, with an extracted CSV in `research/top50-census-2025.csv`. No population or rank is inferred from third-party lists. Vintage 2025 is deliberately fixed until an explicit queue revision; the queue must not reorder automatically when a new Census vintage appears.

## Runtime contract and priority

`config/rollout-regions.json` is a JSON array of exactly 50 objects using the existing builder's six fields: `id`, `name`, `bounds`, `minzoom`, `maxzoom`, `sourceStates`. All use z8–14, valid west/south/east/north bounds with area under 6 square degrees, and one to three official Geofabrik state slugs, except an explicit four-source allowance for existing `chicagoland` only. New boxes are hand-authored small city-center windows, at most 0.4° in either dimension; they are not verified municipal polygons. These windows trade completeness for bounded, affordable rollout. Water, overlapping suburbs and blank areas can be inside the rectangles. OSM completeness is not guaranteed.

The first two array entries preserve existing refresh targets: `nyc-pilot` and `chicagoland`. The other 48 entries are ordered by population rank. The first new-city candidate is **Los Angeles**. A candidate is not a promise to build it in the next run: live download byte limits, processing time, aggregate output size and validation decide eligibility. If a candidate is deferred, record its reason and use the next eligible entry without declaring the deferred city covered. Do not feed the entire array to the legacy builder's `main`, which builds every row and separately imports Chicago; the incremental orchestrator must select bounded work and preserve prior published regions.

An appropriate scheduler performs one due existing-region refresh and at most one eligible new-city build per cycle, within its cumulative run/publication budgets. Queue order establishes expansion priority, not permission to launch fifty builds concurrently. Repeated state downloads for several cities are costly; share a validated source cache when supported, but do not silently switch providers or exceed source freshness requirements.

## Existing coverage and frozen releases

- **New York:** `nyc-pilot` keeps `[-74.05, 40.65, -73.85, 40.90]` and adds `new-jersey` alongside `new-york` for the next refresh. It is still a small partial NYC window, not all five boroughs. The previous published pilot used New York alone; its coverage must not be retroactively described as including New Jersey. The added source fills the selected rectangle’s New Jersey side only after a successful new publication.
- **Chicago:** `chicagoland` keeps `[-89, 41.05, -86.65, 43.25]` and adds Michigan to the original Illinois/Wisconsin/Indiana sources for the next GitHub refresh. This existing region alone is allowed four sources; new cities retain the three-source ceiling. Its 5.17 deg² window is the sole broad existing-region exception. GitHub may publish a new mutable manifest pointer for this region while retaining prior immutable versions. The Cloudflare snapshot and any separate exact-metro frozen release remain byte-frozen. This queue neither includes a separate exact-metro refresh target nor authorizes changing Cloudflare. The old frozen snapshot lacks Michigan as an input even though its rectangle includes a Michigan corner. The new GitHub refresh adds that input within the unchanged bounds; old immutable or Cloudflare bytes are not retroactively fixed. Neither edition promises complete OSM coverage or an official Chicago metropolitan area.
- **El Paso and Detroit:** The selected rectangles cross international boundaries. Sources are explicitly U.S. states only (Texas/New Mexico and Michigan respectively); Mexican/Canadian coverage is not promised and no foreign source is silently fetched.
- Other cross-state windows list adjacent states explicitly: Philadelphia PA/NJ, Charlotte NC/SC, Washington DC/MD/VA, Louisville KY/IN, Portland OR/WA, Memphis TN/AR/MS, Kansas City MO/KS and Omaha NE/IA.
- Neighboring pilot windows can overlap (for example Denver/Aurora and Phoenix/Mesa). Region presence is not a count of unique square kilometers; preserve the client's existing overlap/layer semantics.

## Source size checks and smaller official alternatives

Only the [official Geofabrik U.S. catalog](https://download.geofabrik.de/north-america/us.html) is selected. HEAD requests on 2026-10-14, recorded in `research/source-head-checks.json`, returned these compressed PBF sizes without downloading PBF bodies:

| Source | Bytes | Under 1,500,000,000-byte per-source cap? |
| --- | ---: | --- |
| California | 1,332,392,446 | Yes, currently |
| Texas | 724,738,425 | Yes, currently |
| Northern California | 653,945,373 | Yes, currently |
| Southern California | 671,483,495 | Yes, currently |

California and Texas are **not currently blocked by that byte cap**. A small output rectangle does not reduce the initial whole-state input download. Recheck exact byte lengths at execution; missing or oversized source metadata should defer the city under the orchestrator's safety rules rather than raising caps. Time limits and cumulative download budgets can still defer either state.

[California's official download page](https://download.geofabrik.de/north-america/us/california.html) provides `california/norcal` and `california/socal` subregions. These are smaller same-provider alternatives, but the existing flat-slug `sourceStates` validator and URL construction do not support them. Keep this baseline state-only. A later explicit path allowlist, per-city containment check against the official `.poly` boundary, provenance update and filename/path-safety tests would be required before enabling them. Southern California is a plausible next optimization for Los Angeles/Long Beach/San Diego; northern or southern selection for other cities must be boundary-checked. [Texas's official page](https://download.geofabrik.de/north-america/us/texas.html) lists no subregions. If Texas exceeds a safety cap later, defer it unless a separately approved implementation changes the plan; do not fabricate an official city extract or substitute a new data provider.

## Validation and limitations

Run `python -m unittest discover -s tests -v` from this directory. Tests verify all fifty ranks/populations against the pinned workbook using only the standard library; queue ordering and uniqueness; the builder schema, state slugs, bbox and zoom caps; unchanged existing extents and the explicit NYC/Chicago source-completeness additions; no complete-city coverage claim; and timestamped source-size evidence. The baseline queue passed the prior builder validator. This revised config requires the agreed builder change permitting exactly four sources for `chicagoland`; all other regions retain the three-source ceiling. Recheck the integrated validator before publication.

This work downloaded only the small Census workbook and made four source HEAD requests. It did not generate or publish any new tiles. The first actual build still needs geometry/content validation, strict total publication budgets, an ODbL source extract/provenance artifact, and client verification before being marked available. The existing builder's maxima (1.5 GB source, 750 MB publication, 100,000 tiles) remain safety ceilings, not target sizes.

## Explicit city list

The table is in population-rank order. The JSON puts the two existing regions first, then preserves this order among new candidates. “Existing” means a previously bounded product, not full-city completion; every other row is merely planned until its published manifest is verified.

| Rank | Census place | July 1, 2025 population | Region ID | Bounds [W,S,E,N] | Sources | Coverage |
| ---: | --- | ---: | --- | --- | --- | --- |
| 1 | New York city, New York | 8,584,629 | `nyc-pilot` | [-74.05, 40.65, -73.85, 40.9] | new-york, new-jersey | Existing bounded pilot |
| 2 | Los Angeles city, California | 3,869,089 | `los-angeles-center` | [-118.4, 33.95, -118.1, 34.2] | california | Planned city-center pilot |
| 3 | Chicago city, Illinois | 2,731,585 | `chicagoland` | [-89, 41.05, -86.65, 43.25] | illinois, wisconsin, indiana, michigan | Existing broad window; refresh adds Michigan |
| 4 | Houston city, Texas | 2,397,315 | `houston-center` | [-95.55, 29.6, -95.2, 29.9] | texas | Planned city-center pilot |
| 5 | Phoenix city, Arizona | 1,665,481 | `phoenix-center` | [-112.23, 33.3, -111.93, 33.6] | arizona | Planned city-center pilot |
| 6 | Philadelphia city, Pennsylvania | 1,574,281 | `philadelphia-center` | [-75.3, 39.85, -75.05, 40.1] | pennsylvania, new-jersey | Planned city-center pilot |
| 7 | San Antonio city, Texas | 1,548,422 | `san-antonio-center` | [-98.65, 29.28, -98.35, 29.58] | texas | Planned city-center pilot |
| 8 | San Diego city, California | 1,406,106 | `san-diego-center` | [-117.25, 32.65, -117.0, 32.9] | california | Planned city-center pilot |
| 9 | Dallas city, Texas | 1,329,491 | `dallas-center` | [-96.95, 32.65, -96.65, 32.95] | texas | Planned city-center pilot |
| 10 | Fort Worth city, Texas | 1,028,117 | `fort-worth-center` | [-97.48, 32.6, -97.18, 32.9] | texas | Planned city-center pilot |
| 11 | Jacksonville city, Florida | 1,017,689 | `jacksonville-center` | [-81.8, 30.18, -81.5, 30.48] | florida | Planned city-center pilot |
| 12 | Austin city, Texas | 1,002,632 | `austin-center` | [-97.9, 30.12, -97.6, 30.42] | texas | Planned city-center pilot |
| 13 | San Jose city, California | 989,814 | `san-jose-center` | [-122.05, 37.2, -121.75, 37.45] | california | Planned city-center pilot |
| 14 | Charlotte city, North Carolina | 964,784 | `charlotte-center` | [-80.98, 35.08, -80.68, 35.38] | north-carolina, south-carolina | Planned city-center pilot |
| 15 | Columbus city, Ohio | 938,396 | `columbus-center` | [-83.15, 39.82, -82.85, 40.12] | ohio | Planned city-center pilot |
| 16 | Indianapolis city (balance), Indiana | 901,116 | `indianapolis-center` | [-86.31, 39.62, -86.01, 39.92] | indiana | Planned city-center pilot |
| 17 | San Francisco city, California | 826,079 | `san-francisco-center` | [-122.53, 37.68, -122.32, 37.85] | california | Planned city-center pilot |
| 18 | Seattle city, Washington | 784,777 | `seattle-center` | [-122.43, 47.48, -122.22, 47.76] | washington | Planned city-center pilot |
| 19 | Denver city, Colorado | 740,613 | `denver-center` | [-105.14, 39.59, -104.84, 39.89] | colorado | Planned city-center pilot |
| 20 | Nashville-Davidson metropolitan government (balance), Tennessee | 721,074 | `nashville-center` | [-86.93, 36.01, -86.63, 36.31] | tennessee | Planned city-center pilot |
| 21 | Oklahoma City city, Oklahoma | 719,849 | `oklahoma-city-center` | [-97.67, 35.32, -97.37, 35.62] | oklahoma | Planned city-center pilot |
| 22 | Washington city, District of Columbia | 693,645 | `washington-center` | [-77.15, 38.75, -76.85, 39.05] | district-of-columbia, maryland, virginia | Planned city-center pilot |
| 23 | El Paso city, Texas | 683,012 | `el-paso-center` | [-106.55, 31.7, -106.3, 31.95] | texas, new-mexico | Planned city-center pilot |
| 24 | Las Vegas city, Nevada | 679,817 | `las-vegas-center` | [-115.29, 36.02, -114.99, 36.32] | nevada | Planned city-center pilot |
| 25 | Boston city, Massachusetts | 672,973 | `boston-center` | [-71.18, 42.24, -70.94, 42.46] | massachusetts | Planned city-center pilot |
| 26 | Detroit city, Michigan | 649,095 | `detroit-center` | [-83.2, 42.23, -82.9, 42.49] | michigan | Planned city-center pilot |
| 27 | Louisville/Jefferson County metro government (balance), Kentucky | 641,962 | `louisville-center` | [-85.91, 38.1, -85.61, 38.4] | kentucky, indiana | Planned city-center pilot |
| 28 | Portland city, Oregon | 635,109 | `portland-center` | [-122.82, 45.37, -122.52, 45.67] | oregon, washington | Planned city-center pilot |
| 29 | Memphis city, Tennessee | 609,647 | `memphis-center` | [-90.2, 35.0, -89.9, 35.3] | tennessee, arkansas, mississippi | Planned city-center pilot |
| 30 | Baltimore city, Maryland | 569,997 | `baltimore-center` | [-76.76, 39.14, -76.46, 39.44] | maryland | Planned city-center pilot |
| 31 | Milwaukee city, Wisconsin | 562,407 | `milwaukee-center` | [-88.03, 42.9, -87.78, 43.18] | wisconsin | Planned city-center pilot |
| 32 | Albuquerque city, New Mexico | 556,588 | `albuquerque-center` | [-106.8, 34.94, -106.5, 35.24] | new-mexico | Planned city-center pilot |
| 33 | Fresno city, California | 555,549 | `fresno-center` | [-119.94, 36.59, -119.64, 36.89] | california | Planned city-center pilot |
| 34 | Tucson city, Arizona | 548,371 | `tucson-center` | [-111.12, 32.07, -110.82, 32.37] | arizona | Planned city-center pilot |
| 35 | Sacramento city, California | 536,449 | `sacramento-center` | [-121.64, 38.43, -121.34, 38.73] | california | Planned city-center pilot |
| 36 | Atlanta city, Georgia | 529,110 | `atlanta-center` | [-84.54, 33.6, -84.24, 33.9] | georgia | Planned city-center pilot |
| 37 | Kansas City city, Missouri | 521,220 | `kansas-city-center` | [-94.73, 38.95, -94.43, 39.25] | missouri, kansas | Planned city-center pilot |
| 38 | Mesa city, Arizona | 513,656 | `mesa-center` | [-111.98, 33.27, -111.68, 33.57] | arizona | Planned city-center pilot |
| 39 | Raleigh city, North Carolina | 506,306 | `raleigh-center` | [-78.79, 35.63, -78.49, 35.93] | north-carolina | Planned city-center pilot |
| 40 | Colorado Springs city, Colorado | 494,743 | `colorado-springs-center` | [-104.97, 38.68, -104.67, 38.98] | colorado | Planned city-center pilot |
| 41 | Miami city, Florida | 489,812 | `miami-center` | [-80.35, 25.62, -80.1, 25.92] | florida | Planned city-center pilot |
| 42 | Omaha city, Nebraska | 488,797 | `omaha-center` | [-96.1, 41.11, -95.8, 41.41] | nebraska, iowa | Planned city-center pilot |
| 43 | Virginia Beach city, Virginia | 453,737 | `virginia-beach-center` | [-76.12, 36.7, -75.92, 36.95] | virginia | Planned city-center pilot |
| 44 | Long Beach city, California | 450,469 | `long-beach-center` | [-118.33, 33.66, -118.08, 33.91] | california | Planned city-center pilot |
| 45 | Oakland city, California | 440,838 | `oakland-center` | [-122.39, 37.68, -122.14, 37.93] | california | Planned city-center pilot |
| 46 | Minneapolis city, Minnesota | 430,324 | `minneapolis-center` | [-93.42, 44.83, -93.12, 45.13] | minnesota | Planned city-center pilot |
| 47 | Bakersfield city, California | 422,165 | `bakersfield-center` | [-119.17, 35.22, -118.87, 35.52] | california | Planned city-center pilot |
| 48 | Tulsa city, Oklahoma | 416,209 | `tulsa-center` | [-96.14, 36.0, -95.84, 36.3] | oklahoma | Planned city-center pilot |
| 49 | Tampa city, Florida | 413,554 | `tampa-center` | [-82.61, 27.8, -82.31, 28.1] | florida | Planned city-center pilot |
| 50 | Aurora city, Colorado | 410,053 | `aurora-center` | [-104.95, 39.6, -104.65, 39.9] | colorado | Planned city-center pilot |
