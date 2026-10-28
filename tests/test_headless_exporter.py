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


PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 32


def art_url(code='abc123', version='v1', service='apple'):
    return f'https://m.exophase.com/{service}/games/t/{code}.png?{version}'


def image_response(body=PNG, status=200):
    return Mock(status_code=status, content=body, headers={'Content-Type': 'image/png'})


class AchievementAndLastPlayedTests(unittest.TestCase):
    def row(self, **fields): return m.compile_datastore([game(**fields)], '5143228', 'offline')['games'][0]
    def test_counts_are_exported_and_a_set_with_zero_earned_stays_distinct_from_no_set(self):
        zero, none = self.row(earned_awards=0, total_awards=48, percent=0), self.row(earned_awards=0, total_awards=0, percent=0)
        self.assertEqual((zero['earned_awards'], zero['total_awards']), (0, 48))
        self.assertEqual((none['earned_awards'], none['total_awards']), (0, 0))
    def test_small_progress_keeps_its_counts_when_the_source_percentage_rounds_to_zero(self):
        row = self.row(earned_awards=1, total_awards=105, percent=0)
        self.assertEqual((row['earned_awards'], row['total_awards'], row['achievement_completion_percent']), (1, 105, 0))
        self.assertEqual(row['activity_status'], 'played')
    def test_missing_total_stays_unknown_and_metadata_total_is_a_fallback(self):
        self.assertIsNone(self.row(earned_awards=3)['total_awards'])
        rows = m.compile_datastore([{**game(), 'meta': {**game()['meta'], 'total_awards': 12}}], '5143228', 'offline')['games']
        self.assertEqual(rows[0]['total_awards'], 12)
    def test_invalid_totals_are_rejected(self):
        for fields in ({'total_awards': -1}, {'total_awards': 1.5}, {'total_awards': True}, {'earned_awards': 5, 'total_awards': 4}):
            with self.assertRaises(ValueError): self.row(**fields)
    def test_last_played_is_normalised_and_unknown_is_preserved(self):
        self.assertEqual(self.row(lastplayed_utc=1793128142)['last_played'], '2026-10-27T19:09:02+00:00')
        self.assertIsNone(self.row(lastplayed_utc=0)['last_played'])
        self.assertIsNone(self.row()['last_played'])
        self.assertEqual(self.row(lastplayed=1793128142)['last_played'], '2026-10-27T19:09:02+00:00')
    def test_implausible_future_time_is_unknown_and_malformed_time_fails(self):
        self.assertIsNone(self.row(lastplayed_utc=4102444800)['last_played'])
        for bad in ('yesterday', -5, True, float('nan'), 1e30):
            with self.assertRaises(ValueError): self.row(lastplayed_utc=bad)


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = Path(self.tmp.name) / 'art'
    def tearDown(self): self.tmp.cleanup()
    def test_only_exophase_https_tile_urls_get_a_local_name(self):
        self.assertEqual(m.artwork_name(art_url()), 'apple-abc123-v1.png')
        for url in ('http://m.exophase.com/apple/games/t/a.png?v', 'https://evil.example/apple/games/t/a.png?v',
                    'https://m.exophase.com/apple/games/s/a.png?v', 'https://m.exophase.com/apple/games/t/..%2Fa.png?v',
                    'https://m.exophase.com/a/b/games/t/a.png', 'https://m.exophase.com:8443/apple/games/t/a.png?v',
                    'https://m.exophase.com/apple/games/t/a.exe?v', None, 7):
            self.assertIsNone(m.artwork_name(url), url)
    def test_images_are_downloaded_once_then_reused_without_network(self):
        games = [game(1, resource_tile=art_url('a')), game(2, resource_tile=art_url('b'))]
        with patch.object(m.requests, 'get', return_value=image_response()) as get:
            found, report = m.sync_artwork(games, self.art, fetch=True)
            self.assertEqual(get.call_count, 2)
        self.assertEqual(set(found), {'99:1', '99:2'})
        with patch.object(m.requests, 'get', side_effect=AssertionError('Cached images must not be downloaded again')):
            again, _ = m.sync_artwork(games, self.art, fetch=True)
        self.assertEqual(found, again)
    def test_failed_and_invalid_downloads_leave_no_file_and_do_not_raise(self):
        cases = [image_response(status=500), image_response(b'<html>blocked</html>'), image_response(b''),
                 image_response(PNG + b'0' * m.ART_MAX_BYTES), image_response(b'RIFF' + b'0' * 20)]
        for index, bad in enumerate(cases):
            target = self.art / f'case{index}'
            with patch.object(m.requests, 'get', return_value=bad):
                found, report = m.sync_artwork([game(1, resource_tile=art_url(f'c{index}'))], target, fetch=True)
            self.assertEqual(found, {})
            self.assertEqual(len(report['failures']), 1)
            self.assertEqual([p for p in target.iterdir()] if target.exists() else [], [])
        with patch.object(m.requests, 'get', side_effect=m.requests.Timeout('slow')):
            self.assertEqual(m.sync_artwork([game(1, resource_tile=art_url())], self.art, fetch=True)[0], {})
    def test_older_cached_version_is_used_when_the_new_one_fails(self):
        self.art.mkdir(); (self.art / 'apple-abc123-v1.png').write_bytes(PNG)
        with patch.object(m.requests, 'get', return_value=image_response(status=503)):
            found, _ = m.sync_artwork([game(1, resource_tile=art_url(version='v2'))], self.art, fetch=True)
        self.assertEqual(found, {'99:1': 'apple-abc123-v1.png'})
    def test_downloads_stop_after_repeated_failures(self):
        games = [game(i, resource_tile=art_url(f'g{i}')) for i in range(1, 20)]
        with patch.object(m.requests, 'get', return_value=image_response(status=500)) as get:
            _, report = m.sync_artwork(games, self.art, fetch=True)
        self.assertEqual(get.call_count, m.ART_MAX_CONSECUTIVE_FAILURES)
        self.assertTrue(report['stopped'])
    def test_offline_mode_never_uses_the_network(self):
        with patch.object(m.requests, 'get', side_effect=AssertionError('no network')):
            self.assertEqual(m.sync_artwork([game(1, resource_tile=art_url())], self.art, fetch=False)[0], {})
        self.assertFalse(self.art.exists())
    def test_prune_removes_only_unreferenced_thumbnails(self):
        self.art.mkdir()
        for name in ('apple-keep-v1.png', 'apple-old-v1.png', 'notes.txt'): (self.art / name).write_bytes(PNG)
        removed = m.prune_artwork(self.art, {'games': [{'artwork': 'output/art/apple-keep-v1.png'}, {'artwork': None}]})
        self.assertEqual(removed, 1)
        self.assertEqual(sorted(p.name for p in self.art.iterdir()), ['apple-keep-v1.png', 'notes.txt'])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cache, self.output = root / 'cache', root / 'output'
        self.argv = ['--player-id', '5143228', '--cache-dir', str(self.cache), '--output-dir', str(self.output)]
        self.cache.mkdir(); self.output.mkdir()
        self.old = [game(1, resource_tile=art_url('a')), game(2, resource_tile=art_url('b'))]
        (self.cache / 'telemetry_cache.json').write_text(json.dumps({'schema': 'selfexophase-cache/v1', 'player_id': '5143228', 'games': self.old}))
        (self.output / 'data.json').write_text('previous report')
    def tearDown(self): self.tmp.cleanup()
    def run_main(self, fetched, *extra, env=None):
        with patch.object(m, 'fetch_telemetry_safely', return_value=fetched), patch.object(m.requests, 'get', return_value=image_response()), patch.dict(m.os.environ, env or {}, clear=False):
            return m.main(self.argv + list(extra))
    def files(self): return [(self.cache / 'telemetry_cache.json').read_bytes(), (self.output / 'data.json').read_bytes()]
    def test_publication_survives_an_artwork_failure(self):
        with patch.object(m, 'fetch_telemetry_safely', return_value=self.old), patch.object(m.requests, 'get', return_value=image_response(status=500)):
            self.assertEqual(m.main(self.argv), 0)
        rows = json.loads((self.output / 'data.json').read_text())['games']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['artwork'] is None for r in rows))
    def test_artwork_is_published_beside_the_report_and_stale_images_pruned(self):
        (self.output / 'art').mkdir(); (self.output / 'art' / 'apple-gone-v1.png').write_bytes(PNG)
        self.assertEqual(self.run_main(self.old), 0)
        rows = json.loads((self.output / 'data.json').read_text())['games']
        self.assertEqual({r['artwork'] for r in rows}, {'output/art/apple-a-v1.png', 'output/art/apple-b-v1.png'})
        self.assertEqual(sorted(p.name for p in (self.output / 'art').iterdir()), ['apple-a-v1.png', 'apple-b-v1.png'])
    def test_unreviewed_removals_stay_held_and_name_the_records(self):
        before = self.files()
        with patch('sys.stderr') as err:
            self.assertEqual(self.run_main(self.old[:1]), 1)
        self.assertEqual(self.files(), before)
        self.assertIn('99:2', ''.join(call.args[0] for call in err.write.call_args_list))
    def test_reviewed_removals_are_accepted_only_when_they_match_exactly(self):
        before = self.files()
        for accept in ('99:3', '99:2,99:3', '', 'not-an-id'):
            self.assertEqual(self.run_main(self.old[:1], '--accept-removals', accept), 1, accept)
            self.assertEqual(self.files(), before)
        self.assertEqual(self.run_main(self.old[:1], '--accept-removals', '99:2'), 0)
        self.assertEqual([g['id'] for g in json.loads((self.output / 'data.json').read_text())['games']], ['99:1'])
    def test_partial_acceptance_is_still_held(self):
        before = self.files()
        self.assertEqual(self.run_main([], '--accept-removals', '99:2'), 1)
        self.assertEqual(self.run_main([game(3)], '--accept-removals', '99:2'), 1)
        self.assertEqual(self.files(), before)
    def test_environment_input_is_the_workflow_route_and_scheduled_runs_accept_nothing(self):
        self.assertEqual(self.run_main(self.old[:1], env={'EXOPHASE_ACCEPT_REMOVALS': ''}), 1)
        self.assertEqual(self.run_main(self.old[:1], env={'EXOPHASE_ACCEPT_REMOVALS': '99:2'}), 0)
    def test_acceptance_without_a_previous_cache_is_rejected(self):
        (self.cache / 'telemetry_cache.json').unlink()
        self.assertEqual(self.run_main(self.old, '--accept-removals', '99:2'), 1)


if __name__ == '__main__': unittest.main(verbosity=2)
