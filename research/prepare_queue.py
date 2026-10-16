"""Deterministically produce the reviewable queue from a pinned Census workbook."""
import hashlib,json
from pathlib import Path
from openpyxl import load_workbook
ROOT=Path(__file__).resolve().parents[1]
# Bounds are intentionally hand-authored planning windows, not legal city limits.
# Every new window is at most 0.4 by 0.4 degrees, except existing Chicagoland.
DATA=[
('nyc-pilot','New York City pilot',[-74.05,40.65,-73.85,40.90],['new-york','new-jersey']),
('los-angeles-center','Los Angeles city-center pilot',[-118.40,33.95,-118.10,34.20],['california']),
('chicagoland','Chicagoland',[-89,41.05,-86.65,43.25],['illinois','wisconsin','indiana','michigan']),
('houston-center','Houston city-center pilot',[-95.55,29.60,-95.20,29.90],['texas']),
('phoenix-center','Phoenix city-center pilot',[-112.23,33.30,-111.93,33.60],['arizona']),
('philadelphia-center','Philadelphia city-center pilot',[-75.30,39.85,-75.05,40.10],['pennsylvania','new-jersey']),
('san-antonio-center','San Antonio city-center pilot',[-98.65,29.28,-98.35,29.58],['texas']),
('san-diego-center','San Diego city-center pilot',[-117.25,32.65,-117.00,32.90],['california']),
('dallas-center','Dallas city-center pilot',[-96.95,32.65,-96.65,32.95],['texas']),
('fort-worth-center','Fort Worth city-center pilot',[-97.48,32.60,-97.18,32.90],['texas']),
('jacksonville-center','Jacksonville city-center pilot',[-81.80,30.18,-81.50,30.48],['florida']),
('austin-center','Austin city-center pilot',[-97.90,30.12,-97.60,30.42],['texas']),
('san-jose-center','San Jose city-center pilot',[-122.05,37.20,-121.75,37.45],['california']),
('charlotte-center','Charlotte city-center pilot',[-80.98,35.08,-80.68,35.38],['north-carolina','south-carolina']),
('columbus-center','Columbus city-center pilot',[-83.15,39.82,-82.85,40.12],['ohio']),
('indianapolis-center','Indianapolis city-center pilot',[-86.31,39.62,-86.01,39.92],['indiana']),
('san-francisco-center','San Francisco city-center pilot',[-122.53,37.68,-122.32,37.85],['california']),
('seattle-center','Seattle city-center pilot',[-122.43,47.48,-122.22,47.76],['washington']),
('denver-center','Denver city-center pilot',[-105.14,39.59,-104.84,39.89],['colorado']),
('nashville-center','Nashville city-center pilot',[-86.93,36.01,-86.63,36.31],['tennessee']),
('oklahoma-city-center','Oklahoma City city-center pilot',[-97.67,35.32,-97.37,35.62],['oklahoma']),
('washington-center','Washington DC city-center pilot',[-77.15,38.75,-76.85,39.05],['district-of-columbia','maryland','virginia']),
('el-paso-center','El Paso US city-center pilot',[-106.55,31.70,-106.30,31.95],['texas','new-mexico']),
('las-vegas-center','Las Vegas city-center pilot',[-115.29,36.02,-114.99,36.32],['nevada']),
('boston-center','Boston city-center pilot',[-71.18,42.24,-70.94,42.46],['massachusetts']),
('detroit-center','Detroit US city-center pilot',[-83.20,42.23,-82.90,42.49],['michigan']),
('louisville-center','Louisville city-center pilot',[-85.91,38.10,-85.61,38.40],['kentucky','indiana']),
('portland-center','Portland city-center pilot',[-122.82,45.37,-122.52,45.67],['oregon','washington']),
('memphis-center','Memphis city-center pilot',[-90.20,35.00,-89.90,35.30],['tennessee','arkansas','mississippi']),
('baltimore-center','Baltimore city-center pilot',[-76.76,39.14,-76.46,39.44],['maryland']),
('milwaukee-center','Milwaukee city-center pilot',[-88.03,42.90,-87.78,43.18],['wisconsin']),
('albuquerque-center','Albuquerque city-center pilot',[-106.80,34.94,-106.50,35.24],['new-mexico']),
('fresno-center','Fresno city-center pilot',[-119.94,36.59,-119.64,36.89],['california']),
('tucson-center','Tucson city-center pilot',[-111.12,32.07,-110.82,32.37],['arizona']),
('sacramento-center','Sacramento city-center pilot',[-121.64,38.43,-121.34,38.73],['california']),
('atlanta-center','Atlanta city-center pilot',[-84.54,33.60,-84.24,33.90],['georgia']),
('kansas-city-center','Kansas City city-center pilot',[-94.73,38.95,-94.43,39.25],['missouri','kansas']),
('mesa-center','Mesa city-center pilot',[-111.98,33.27,-111.68,33.57],['arizona']),
('raleigh-center','Raleigh city-center pilot',[-78.79,35.63,-78.49,35.93],['north-carolina']),
('colorado-springs-center','Colorado Springs city-center pilot',[-104.97,38.68,-104.67,38.98],['colorado']),
('miami-center','Miami city-center pilot',[-80.35,25.62,-80.10,25.92],['florida']),
('omaha-center','Omaha city-center pilot',[-96.10,41.11,-95.80,41.41],['nebraska','iowa']),
('virginia-beach-center','Virginia Beach city-center pilot',[-76.12,36.70,-75.92,36.95],['virginia']),
('long-beach-center','Long Beach city-center pilot',[-118.33,33.66,-118.08,33.91],['california']),
('oakland-center','Oakland city-center pilot',[-122.39,37.68,-122.14,37.93],['california']),
('minneapolis-center','Minneapolis city-center pilot',[-93.42,44.83,-93.12,45.13],['minnesota']),
('bakersfield-center','Bakersfield city-center pilot',[-119.17,35.22,-118.87,35.52],['california']),
('tulsa-center','Tulsa city-center pilot',[-96.14,36.00,-95.84,36.30],['oklahoma']),
('tampa-center','Tampa city-center pilot',[-82.61,27.80,-82.31,28.10],['florida']),
('aurora-center','Aurora city-center pilot',[-104.95,39.60,-104.65,39.90],['colorado']),
]
xlsx=ROOT/'research/SUB-IP-EST2025-ANNRNK.xlsx'
rows=[r for r in load_workbook(xlsx,data_only=True).active.values if isinstance(r[0],int) and 1<=r[0]<=50]
assert len(rows)==len(DATA)==50
ranked=[]; configs=[]
for (rank,area,*pops),(rid,name,bounds,states) in zip(rows,DATA):
 configs.append(dict(id=rid,name=name,bounds=bounds,minzoom=8,maxzoom=14,sourceStates=states))
 ranked.append(dict(populationRank=rank,censusArea=area,population2025=pops[-1],regionId=rid,coverage='existing-bounded-nyc-pilot' if rid=='nyc-pilot' else 'existing-broad-chicagoland-window-with-expanded-refresh-sources' if rid=='chicagoland' else 'planned-bounded-city-center-pilot',fullCityCoverageClaim=False))
# Existing mutable regions precede the population-priority expansion queue.
configs=[configs[0],configs[2],configs[1],*configs[3:]]
meta={'schemaVersion':1,'preparedAt':'2026-10-14','ranking':{'publisher':'U.S. Census Bureau','table':'SUB-IP-EST2025-ANNRNK','populationDate':'2025-07-01','releaseMonth':'2026-05','geography':'Top 50 entries in the official incorporated-place ranking, including listed balance areas; not metropolitan areas.','landingUrl':'https://www.census.gov/data/datasets/time-series/demo/popest/2020s-total-cities-and-towns.html','sourceUrl':'https://www2.census.gov/programs-surveys/popest/tables/2020-2025/cities/totals/SUB-IP-EST2025-ANNRNK.xlsx','sourceSha256':hashlib.sha256(xlsx.read_bytes()).hexdigest()},'queuePolicy':{'existingFirst':['nyc-pilot','chicagoland'],'expansion':'Remaining cities in ascending populationRank; budget or runtime eligibility may defer an entry.','firstNewCityCandidate':'los-angeles-center','buildAllAtOnce':False,'sourceProvider':'Geofabrik','newProviderAllowed':False},'cities':ranked}
meta['coverageRefreshChanges'] = {'nyc-pilot': {'unchangedBounds': True, 'previousSourceStates': ['new-york'], 'nextSourceStates': ['new-york', 'new-jersey'], 'effectiveAfter': 'successful new GitHub publication'}, 'chicagoland': {'unchangedBounds': True, 'previousSourceStates': ['illinois', 'wisconsin', 'indiana'], 'nextSourceStates': ['illinois', 'wisconsin', 'indiana', 'michigan'], 'sourceCountException': 4, 'effectiveAfter': 'successful new GitHub publication', 'frozenCloudflareAndOldVersionsUnchanged': True}}
for path,obj in [('config/rollout-regions.json',configs),('config/rollout-city-metadata.json',meta)]:
 (ROOT/path).write_text(json.dumps(obj,indent=2,ensure_ascii=False)+'\n')
print(f'Wrote {len(configs)} regions; pinned workbook SHA256 {meta["ranking"]["sourceSha256"]}')
