"""Offline structural and provenance checks for the bounded city queue."""
import ast
import hashlib
import json
import re
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGIONS = json.loads((ROOT / 'config/rollout-regions.json').read_text())
METADATA = json.loads((ROOT / 'config/rollout-city-metadata.json').read_text())
BY_ID = {r['id']: r for r in REGIONS}
XLSX = ROOT / 'research/SUB-IP-EST2025-ANNRNK.xlsx'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def workbook_ranking():
    """Read the pinned source without adding an Excel dependency to CI."""
    with zipfile.ZipFile(XLSX) as book:
        strings = [''.join(n.itertext()) for n in ET.fromstring(book.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        rows = ET.fromstring(book.read('xl/worksheets/sheet1.xml')).findall('s:sheetData/s:row', NS)
    result = []
    for row in rows:
        cells = {}
        for cell in row.findall('s:c', NS):
            value = cell.find('s:v', NS)
            if value is None:
                continue
            value = strings[int(value.text)] if cell.get('t') == 's' else value.text
            cells[re.sub(r'\d+', '', cell.get('r'))] = value
        if cells.get('A', '').isdigit() and 1 <= int(cells['A']) <= 50:
            result.append((int(cells['A']), cells['B'], int(cells['I'])))
    return result


class RolloutQueueTests(unittest.TestCase):
    def test_exactly_fifty_unique_cities_and_regions(self):
        self.assertEqual(len(REGIONS), 50)
        self.assertEqual(len(BY_ID), 50)
        self.assertEqual([c['populationRank'] for c in METADATA['cities']], list(range(1, 51)))
        self.assertEqual({c['regionId'] for c in METADATA['cities']}, set(BY_ID))

    def test_builder_contract_and_bounded_zooms(self):
        allowed_states = set('new-york california illinois wisconsin indiana texas arizona pennsylvania new-jersey florida north-carolina south-carolina ohio washington colorado tennessee oklahoma district-of-columbia maryland virginia new-mexico nevada massachusetts michigan kentucky oregon arkansas mississippi georgia missouri kansas nebraska iowa minnesota'.split())
        for r in REGIONS:
            with self.subTest(region=r['id']):
                self.assertEqual(set(r), {'id','name','bounds','minzoom','maxzoom','sourceStates'})
                self.assertRegex(r['id'], r'^[a-z0-9-]{1,48}$')
                w,s,e,n = r['bounds']
                self.assertTrue(-180 <= w < e <= 180 and -85 <= s < n <= 85)
                self.assertTrue(0 < (e-w)*(n-s) < 6)
                self.assertEqual((r['minzoom'], r['maxzoom']), (8,14))
                self.assertTrue(1 <= len(r['sourceStates']) <= (4 if r['id'] == 'chicagoland' else 3))
                self.assertEqual(len(r['sourceStates']), len(set(r['sourceStates'])))
                self.assertTrue(set(r['sourceStates']) <= allowed_states)
                if r['id'] not in {'nyc-pilot','chicagoland'}:
                    self.assertLessEqual(e-w, .4000001)
                    self.assertLessEqual(n-s, .4000001)
                    self.assertIn('pilot', r['name'])

    def test_existing_bounds_and_explicit_source_completeness_fix(self):
        self.assertEqual(BY_ID['nyc-pilot']['bounds'], [-74.05,40.65,-73.85,40.9])
        self.assertEqual(BY_ID['nyc-pilot']['sourceStates'], ['new-york','new-jersey'])
        self.assertEqual(BY_ID['chicagoland']['bounds'], [-89,41.05,-86.65,43.25])
        self.assertEqual(BY_ID['chicagoland']['sourceStates'], ['illinois','wisconsin','indiana','michigan'])
        self.assertTrue(all(not c['fullCityCoverageClaim'] for c in METADATA['cities']))
        self.assertNotIn('exact-metro', BY_ID)
        self.assertNotIn('chicagoland-metro', BY_ID)

    def test_fixed_population_priority_after_existing_regions(self):
        self.assertEqual([r['id'] for r in REGIONS[:3]], ['nyc-pilot','chicagoland','los-angeles-center'])
        existing = {'nyc-pilot','chicagoland'}
        expansion = [c['regionId'] for c in METADATA['cities'] if c['regionId'] not in existing]
        self.assertEqual([r['id'] for r in REGIONS[2:]], expansion)
        self.assertEqual(METADATA['queuePolicy']['firstNewCityCandidate'], 'los-angeles-center')
        self.assertFalse(METADATA['queuePolicy']['buildAllAtOnce'])
        self.assertFalse(METADATA['queuePolicy']['newProviderAllowed'])

    def test_ranking_exactly_matches_pinned_primary_source(self):
        self.assertEqual(hashlib.sha256(XLSX.read_bytes()).hexdigest(), METADATA['ranking']['sourceSha256'])
        self.assertEqual(workbook_ranking(), [(c['populationRank'],c['censusArea'],c['population2025']) for c in METADATA['cities']])
        self.assertEqual(METADATA['ranking']['populationDate'], '2025-07-01')
        self.assertEqual(METADATA['ranking']['releaseMonth'], '2026-05')

    def test_source_budget_evidence_is_timestamped_and_not_permission_to_raise_cap(self):
        evidence = json.loads((ROOT / 'research/source-head-checks.json').read_text())
        self.assertEqual(evidence['checkedAt'], '2026-10-14')
        for result in evidence['results']:
            self.assertEqual(result['status'], 200)
            self.assertLess(int(result['contentLength']), 1_500_000_000)
            self.assertTrue(result['url'].startswith('https://download.geofabrik.de/'))


if __name__ == '__main__':
    unittest.main()
