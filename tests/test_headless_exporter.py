import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('headless_exporter', Path(__file__).resolve().parents[1] / 'headless_exporter.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def game(gid=1, **fields):
    return {'master_playerid': 99, 'master_id': gid, 'meta': {'title': f'Game {gid}',
            'environment_slug': 'xbox', 'platforms': [{'name': 'iOS'}]}, **fields}


def response(payload, status=200):
    return Mock(status_code=status, json=Mock(return_value=payload))


class HeadlessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache, self.output = self.root / 'cache', self.root / 'output'
        self.argv = ['--player-id', '5143228', '--cache-dir', str(self.cache), '--output-dir', str(self.output)]
    def tearDown(self): self.tmp.cleanup()
    def report(self, rows): return m.compile_datastore(rows, '5143228', 'offline')
    def seed(self):
        self.cache.mkdir(); self.output.mkdir()
        old = {'schema': 'selfexophase-cache/v1', 'player_id': '5143228', 'games': [game()]}
        (self.cache / 'telemetry_cache.json').write_text(json.dumps(old))
        (self.output / 'data.json').write_text('previous report')
        return [(p, p.read_bytes()) for p in (self.cache / 'telemetry_cache.json', self.output / 'data.json')]
    def assert_preserved(self, files):
        for path, content in files: self.assertEqual(path.read_bytes(), content)
    def test_paginates_complete_source(self):
        with patch.object(m.requests, 'get', side_effect=[response({'success': True, 'games': [game(i) for i in range(1,51)]}), response({'success': True, 'games': [game(51)]})]) as get:
            self.assertEqual(len(m.fetch_telemetry_safely('5143228')), 51)
            self.assertEqual(get.call_args.kwargs['params']['page'], 2)
    def test_repeated_page_is_rejected(self):
        with patch.object(m.requests, 'get', return_value=response({'success': True, 'games': [game(i) for i in range(1,51)]})):
            with self.assertRaises(ValueError): m.fetch_telemetry_safely('5143228')
    def test_http_error_returns_failure_and_retains_files(self):
        before = self.seed()
        with patch.object(m.requests, 'get', return_value=response({}, 403)):
            self.assertEqual(m.main(self.argv), 1)
        self.assert_preserved(before)
    def test_partial_download_retains_files(self):
        before = self.seed()
        with patch.object(m.requests, 'get', side_effect=[response({'success': True, 'games': [game(i) for i in range(1,51)]}), m.requests.Timeout('timeout')]):
            self.assertEqual(m.main(self.argv), 1)
        self.assert_preserved(before)
    def test_false_success_empty_and_malformed_pages_are_held(self):
        for payload in ({'success': False}, {'success': True, 'games': []}, {'games': [game()]}, {'success': True, 'games': 'wrong'}):
            with patch.object(m.requests, 'get', return_value=response(payload)):
                with self.assertRaises(ValueError): m.fetch_telemetry_safely('5143228')
    def test_actual_metadata_and_service_platform_distinction(self):
        row = self.report([game(playtime='2h 3m', percent=100, earned_awards=10)])['games'][0]
        self.assertEqual(row['title'], 'Game 1')
        self.assertEqual(row['service'], 'xbox')
        self.assertEqual(row['platforms'], ['iOS'])
        self.assertEqual(row['hours'], 2.05)
        self.assertEqual(row['achievement_completion_percent'], 100)
        self.assertEqual(row['activity_status'], 'played')
        self.assertNotIn('ownership', row)
        self.assertNotIn('completed', row.values())
    def test_unknown_playtime_is_not_zero_or_unplayed(self):
        row = self.report([game(playtime='', playtimeUnits={'hours': 0, 'minutes': 0})])['games'][0]
        self.assertIsNone(row['hours'])
        self.assertEqual(row['activity_status'], 'unknown')
    def test_units_and_medals_supply_positive_activity_evidence(self):
        rows = self.report([game(playtime='', playtimeUnits={'hours': 1, 'minutes': 5}), game(2, earned_bronze=1)])['games']
        self.assertEqual(rows[0]['playtime_minutes'], 65)
        self.assertEqual(rows[1]['activity_status'], 'played')
    def test_invalid_values_and_duplicate_ids_fail(self):
        for rows in ([game(),game()], [game(playtime='not time')], [game(percent=float('nan'))], [game(percent=101)], [game(master_id=-1)]):
            with self.assertRaises(ValueError): self.report(rows)
    def test_transform_failure_retains_cache_and_report(self):
        before = self.seed()
        with patch.object(m, 'fetch_telemetry_safely', return_value=[game(playtime='bad')]):
            self.assertEqual(m.main(self.argv), 1)
        self.assert_preserved(before)
    def test_source_removals_are_held_for_review(self):
        before = self.seed()
        with patch.object(m, 'fetch_telemetry_safely', return_value=[game(2)]):
            self.assertEqual(m.main(self.argv), 1)
        self.assert_preserved(before)
    def test_output_replace_failure_restores_cache(self):
        before = self.seed()
        original = Path.replace
        def replace(path, target):
            if Path(target) == self.output / 'data.json': raise OSError('Simulated save failure')
            return original(path, target)
        with patch.object(Path, 'replace', replace):
            with self.assertRaises(OSError): m.publish({'new': True}, {'new': True}, self.cache, self.output)
        self.assert_preserved(before)
        self.assertEqual(list(self.cache.glob('.telemetry-*')), [])
    def test_offline_import_succeeds_without_requests_or_textual(self):
        source = self.root / 'all_games.json'; source.write_text(json.dumps([game()]))
        with patch.object(m.requests, 'get', side_effect=AssertionError('Network must not be used')):
            self.assertEqual(m.main(self.argv + ['--input', str(source)]), 0)
        report = json.loads((self.output / 'data.json').read_text())
        self.assertEqual(report['metadata']['total_games'], 1)
        self.assertEqual(report['metadata']['source_mode'], 'offline')
        self.assertIsNone(report['metadata']['source_retrieved_at'])
        self.assertEqual(list(self.output.iterdir()), [self.output / 'data.json'])
    def test_cache_profile_mismatch_is_rejected(self):
        before = self.seed()
        self.assertEqual(m.main([*self.argv, '--player-id', '2']), 1)
        self.assert_preserved(before)
    def test_invalid_profile_is_rejected_before_http(self):
        with patch.object(m.requests, 'get') as get:
            self.assertEqual(m.main([*self.argv, '--player-id', '../other']), 1)
            get.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
