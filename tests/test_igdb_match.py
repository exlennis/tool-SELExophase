import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('igdb_match', ROOT / 'tools' / 'igdb_match.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def result(igdb_id, name, slug, platforms=(), game_type=0, year=2020, ratings=None):
    return {'igdb_id': igdb_id, 'slug': slug, 'name': name, 'game_type': game_type, 'year': year, 'platforms': list(platforms), 'ratings': ratings}


def record(rid='1:1', title='Hades', service='nintendo', platforms=('Switch',)):
    return {'id': rid, 'title': title, 'service': service, 'platforms': list(platforms)}


HADES_2020 = result(113112, 'Hades', 'hades--1', ['Nintendo Switch', 'PC (Microsoft Windows)', 'Mac'], ratings=1794)
HADES_1995 = result(80529, 'Hades', 'hades', ['PC (Microsoft Windows)'], year=1995, ratings=0)


class FakeClient:
    requests_made = 0
    def __init__(self, table): self.table, self.terms, self.exact_terms = table, [], []
    def search(self, term):
        self.terms.append(term)
        return self.table.get(term.lower(), [])
    def exact(self, name):
        self.exact_terms.append(name)
        return self.table.get('exact:' + name.lower(), [])
    def save_cache(self): pass
    def by_slugs(self, slugs): return {x: self.table['slug:' + x] for x in slugs if 'slug:' + x in self.table}


class NamesAndScoring(unittest.TestCase):
    def test_normalisation_folds_accents_symbols_and_roman_numerals(self):
        self.assertEqual(m.normalise('Pokémon Violet'), m.normalise('Pokemon violet'))
        self.assertEqual(m.normalise('Hades II'), m.normalise('Hades 2'))
        self.assertEqual(m.normalise('Link’s Awakening'), m.normalise("Link's Awakening"))
        self.assertEqual(m.normalise('Tom & Jerry'), m.normalise('Tom and Jerry'))
    def test_exophase_decorations_are_removed_before_searching(self):
        self.assertEqual(m.clean_title('~Hack~ Pokémon Unbound'), 'Pokémon Unbound')
        self.assertEqual(m.clean_title('Some Game (Remastered)'), 'Some Game')
        self.assertEqual(m.clean_title('Hades'), 'Hades')
    def test_name_kinds_are_ranked(self):
        kinds = {t: m.name_match('The Witcher 3', t)[1] for t in ('The Witcher 3', 'Witcher 3', 'The Witcher 3 Deluxe Edition', 'The Witcher 3: Blood and Wine', 'Doom')}
        self.assertEqual(kinds, {'The Witcher 3': 'exact', 'Witcher 3': 'article', 'The Witcher 3 Deluxe Edition': 'edition', 'The Witcher 3: Blood and Wine': 'subtitle', 'Doom': 'none'})
        self.assertEqual(m.name_match('Mario 64', 'Super Mario 64')[1], 'prefix-word')
    def test_platform_signal_uses_only_known_platforms(self):
        self.assertEqual(m.platform_signal(['Steam'], ['PC (Microsoft Windows)'])[1], 'match')
        self.assertEqual(m.platform_signal(['PS5'], ['Nintendo Switch'])[1], 'mismatch')
        self.assertEqual(m.platform_signal(['Made-up Console'], ['Nintendo Switch'])[1], 'unknown')
        self.assertEqual(m.platform_signal(['PS5'], [])[1], 'unknown')
    def test_results_are_parsed_defensively(self):
        raw = {'id': 5, 'name': 'X', 'slug': 'x', 'game_type': 5, 'first_release_date': 1600000000, 'platforms': [{'id': 1, 'name': 'Mac'}, {'bad': 1}]}
        self.assertEqual(m.parse_result(raw), {'igdb_id': 5, 'slug': 'x', 'name': 'X', 'game_type': 5, 'year': 2020, 'platforms': ['Mac'], 'ratings': None})
        for bad in (None, {}, {'id': '5', 'name': 'X', 'slug': 'x'}, {'id': 5, 'name': 'X', 'slug': 'Bad Slug'}, {'id': 5, 'slug': 'x'}):
            self.assertIsNone(m.parse_result(bad))


class Decisions(unittest.TestCase):
    def pick(self, rec, results, siblings=None):
        chosen, how = m.decide(rec, m.rank_candidates(rec, results), siblings)
        return (chosen['slug'], how) if chosen else (None, None)
    def choice(self, rec, results, siblings=None): return self.pick(rec, results, siblings)[0]
    def test_popularity_separates_the_two_games_called_hades(self):
        self.assertEqual(self.pick(record(service='steam', platforms=['Steam']), [HADES_1995, HADES_2020]), ('hades--1', 'tiebreak-popularity'))
    def test_a_tie_with_no_usable_signal_goes_to_review(self):
        a, b = result(1, 'Tetris', 'tetris', ['iOS'], ratings=40), result(2, 'Tetris', 'tetris--20', ['iOS'], ratings=30)
        self.assertIsNone(self.choice(record('1:1', 'Tetris', 'apple', ['iOS']), [a, b]))
        a['ratings'] = b['ratings'] = None
        self.assertIsNone(self.choice(record('1:1', 'Tetris', 'apple', ['iOS']), [a, b]))
    def test_platform_decides_only_when_one_entry_fits_and_every_other_contradicts(self):
        remake = result(1, 'Link’s Awakening', 'la-2019', ['Nintendo Switch'], year=2019, ratings=900)
        original = result(2, 'Link’s Awakening', 'la-1993', ['Game Boy'], year=1993, ratings=400)
        self.assertEqual(self.pick(record('1:1', 'Link’s Awakening', 'nintendo', ['Switch']), [original, remake]), ('la-2019', 'tiebreak-platform'))
        unknown = result(3, 'Link’s Awakening', 'la-other', [], year=2000)
        unrated = result(4, 'Link’s Awakening', 'la-unrated', ['Nintendo Switch'], year=2019)
        self.assertIsNone(self.choice(record('1:1', 'Link’s Awakening', 'nintendo', ['Switch']), [unknown, unrated]))
    def test_popularity_decides_between_entries_that_both_fit_the_platform(self):
        popular = result(1, 'Unpacking', 'unpacking', ['PC (Microsoft Windows)', 'iOS'], year=2021, ratings=247)
        prototype = result(2, 'Unpacking', 'unpacking--1', ['PC (Microsoft Windows)', 'Mac'], year=2018, ratings=None)
        self.assertEqual(self.pick(record('1:1', 'Unpacking+', 'apple', ['macOS', 'iOS']), [prototype, popular]), ('unpacking', 'tiebreak-popularity'))
    def test_a_popular_entry_that_contradicts_the_platform_is_left_for_review_when_another_fits(self):
        popular = result(1, 'Injustice', 'console', ['PlayStation 4'], year=2013, ratings=358)
        mobile = result(2, 'Injustice', 'mobile', ['iOS'], year=2013, ratings=16)
        self.assertIsNone(self.choice(record('1:1', 'Injustice', 'apple', ['iOS']), [popular, mobile]))
    def test_a_title_already_linked_for_another_service_is_reused(self):
        a, b = result(1, 'Tetris', 'tetris', ['iOS'], ratings=40), result(2, 'Tetris', 'tetris--20', ['iOS'], ratings=30)
        siblings = {'tetris': {'tetris--20'}}
        self.assertEqual(self.pick(record('1:1', 'Tetris', 'apple', ['iOS']), [a, b], siblings), ('tetris--20', 'tiebreak-sibling'))
    def test_a_main_game_beats_a_bundle_of_the_same_name(self):
        bundle, main = result(1, 'PowerWash Simulator', 'ps-2025', ['iOS'], game_type=3, ratings=5), result(2, 'PowerWash Simulator', 'ps-2022', ['iOS'], game_type=0, ratings=5)
        self.assertEqual(self.pick(record('1:1', 'PowerWash Simulator', 'apple', ['iOS']), [bundle, main]), ('ps-2022', 'tiebreak-type'))
    def test_a_year_tag_in_the_title_picks_the_matching_year(self):
        new, old = result(1, 'Doom', 'doom--2', ['PC (Microsoft Windows)'], year=2016), result(2, 'Doom', 'doom--3', ['PC (Microsoft Windows)'], year=2019)
        self.assertEqual(self.pick(record('1:1', 'DOOM (2016)', 'gog', ['GOG']), [old, new]), ('doom--2', 'tiebreak-year'))
    def test_platform_families_are_not_contradictions_but_other_platforms_are(self):
        self.assertEqual(self.choice(record('1:1', 'Grindstone', 'apple', ['macOS']), [result(1, 'Grindstone', 'grindstone', ['iOS'])]), 'grindstone')
        self.assertEqual(self.choice(record('1:1', 'Cyberpunk 2077', 'psn', ['PS5']), [result(2, 'Cyberpunk 2077', 'cp', ['PlayStation 4'])]), 'cp')
        self.assertEqual(self.choice(record('1:1', 'Cyberpunk 2077', 'steam', ['Steam']), [result(2, 'Cyberpunk 2077', 'cp', ['PlayStation 4'])]), None)
        self.assertEqual(self.choice(record(platforms=['PS5']), [result(1, 'Hades', 'hades--1', ['Nintendo Switch'])]), None)
    def test_a_unique_exact_match_is_accepted(self):
        self.assertEqual(self.choice(record('9:9', 'Black Myth: Wukong', 'psn', ['PS5']), [result(1, 'Black Myth: Wukong', 'black-myth-wukong', ['PlayStation 5'])]), 'black-myth-wukong')
    def test_mods_are_treated_like_any_other_game(self):
        mod = result(7, 'Pokémon Unbound', 'pokemon-unbound', ['Game Boy Advance'], game_type=5)
        self.assertEqual(self.choice(record('5:5', '~Hack~ Pokémon Unbound', 'retro', ['GBA']), [mod]), 'pokemon-unbound')
    def test_add_ons_do_not_shadow_the_main_game_and_are_not_auto_accepted(self):
        dlc = result(2, 'Hades', 'hades-dlc', ['Nintendo Switch'], game_type=1)
        self.assertEqual(self.choice(record(), [dlc, HADES_2020]), 'hades--1')
        self.assertIsNone(self.choice(record(), [dlc]))
    def test_an_edition_of_the_same_game_does_not_block_the_exact_match(self):
        base = result(1, 'Hogwarts Legacy', 'hogwarts-legacy', ['PC (Microsoft Windows)'])
        deluxe = result(2, 'Hogwarts Legacy: Deluxe Edition', 'hogwarts-legacy-deluxe-edition', ['PC (Microsoft Windows)'])
        self.assertEqual(self.choice(record('1:1', 'Hogwarts Legacy', 'steam', ['Steam']), [base, deluxe]), 'hogwarts-legacy')
    def test_an_edition_that_fits_the_platform_stands_in_when_the_plain_entry_does_not(self):
        plain = result(1, 'Planet Coaster', 'planet-coaster', ['PC (Microsoft Windows)'], year=2016)
        console = result(2, 'Planet Coaster: Deluxe Edition', 'planet-coaster-deluxe-edition', ['PlayStation 4'], year=2020)
        self.assertEqual(self.pick(record('1:1', 'Planet Coaster', 'psn', ['PS5']), [plain, console]), ('planet-coaster-deluxe-edition', 'edition-platform'))
        self.assertIsNone(self.choice(record('1:1', 'Planet Coaster', 'steam', ['Steam']), [console]))
    def test_a_trailing_plus_is_not_silently_dropped(self):
        self.assertIsNone(self.choice(record('1:1', 'Chess', 'apple', ['iOS']), [result(1, 'Chess++', 'chess-plus-plus--1', ['iOS'])]))
        self.assertEqual(self.choice(record('1:1', 'Terraria+', 'apple', ['iOS']), [result(2, 'Terraria', 'terraria', ['iOS'])]), 'terraria')
        self.assertEqual(self.choice(record('1:1', 'Terraria+', 'apple', ['iOS']), [result(2, 'Terraria', 'terraria', ['iOS']), result(3, 'Terraria+', 'terraria-plus', ['iOS'])]), 'terraria-plus')
        self.assertIsNone(self.choice(record('1:1', 'Fruit Ninja Classic', 'apple', ['iOS']), [result(4, 'Fruit Ninja Classic+', 'fruit-ninja-classic-plus', ['iOS'])]))
    def test_a_sole_subtitle_candidate_is_accepted_but_not_when_it_is_ambiguous_or_a_one_word_title(self):
        rec = record('1:1', 'Oceanhorn 2', 'apple', ['iOS'])
        self.assertEqual(self.pick(rec, [result(1, 'Oceanhorn 2: Knights of the Lost Realm', 'oceanhorn-2', ['iOS'])]), ('oceanhorn-2', 'subtitle'))
        two = [result(1, 'Plants vs. Zombies 2: Reflourished', 'a', ['iOS']), result(2, "Plants vs. Zombies 2: It's About Time", 'b', ['iOS'])]
        self.assertIsNone(self.choice(record('1:1', 'Plants vs. Zombies 2', 'apple', ['iOS']), two))
        self.assertIsNone(self.choice(record('1:1', 'Doom', 'steam', ['Steam']), [result(4, 'Doom Eternal', 'doom-eternal', ['PC (Microsoft Windows)'])]))
        self.assertIsNone(self.choice(record('1:1', 'Doom 2', 'steam', ['Steam']), [result(4, 'Doom 2 Eternal', 'x', ['PC (Microsoft Windows)']), result(5, 'Doom 2 Classic', 'y', ['PC (Microsoft Windows)'])]))
    def test_glued_words_and_punctuation_match_on_a_compact_form(self):
        self.assertEqual(self.pick(record('1:1', 'Pac-Man', 'apple', ['iOS']), [result(1, 'PacMan', 'pacman', ['iOS'])]), ('pacman', 'compact'))
        self.assertEqual(self.pick(record('1:1', 'RollerCoaster Tycoon 4Mobile', 'apple', ['iOS']), [result(1, 'RollerCoaster Tycoon 4 Mobile', 'rct4', ['iOS'])]), ('rct4', 'compact'))
    def test_taglines_and_marketing_suffixes_are_stripped_for_specific_titles_only(self):
        self.assertEqual(self.pick(record('1:1', 'Logos Quiz - Guess the logos!', 'apple', ['iOS']), [result(1, 'Logos Quiz', 'logos-quiz', ['iOS'])]), ('logos-quiz', 'derived'))
        self.assertEqual(self.pick(record('1:1', 'Genshin Impact: Natlan Launch', 'apple', ['iOS']), [result(1, 'Genshin Impact', 'genshin-impact', ['iOS'])]), ('genshin-impact', 'derived'))
        self.assertIsNone(self.choice(record('1:1', 'Doom: Eternal Reborn', 'apple', ['iOS']), [result(1, 'Doom', 'doom', ['iOS'])]))
        self.assertIsNone(self.choice(record('1:1', 'Chess - Play & Learn+', 'apple', ['iOS']), [result(1, 'Chess', 'chess', ['iOS'])]))
        self.assertEqual(self.pick(record('1:1', 'Threes! Freeplay', 'apple', ['iOS']), [result(1, 'Threes!', 'threes', ['iOS'])]), ('threes', 'derived-noise'))
    def test_cleanup_handles_apple_arcade_markers_inverted_articles_and_year_tags(self):
        self.assertEqual(m.clean_title('The PathlessAA'), 'The Pathless')
        self.assertEqual(m.clean_title('Assemble with Care AA'), 'Assemble with Care')
        self.assertEqual(m.clean_title('GRAAA'), 'GRAAA')
        self.assertEqual(m.clean_title('Ultimate Doom, The'), 'The Ultimate Doom')
        self.assertEqual(m.clean_title('DOOM (2016)'), 'DOOM')
        self.assertEqual(m.year_hint('DOOM (2016)'), 2016)
        self.assertIsNone(m.year_hint('Hades'))
    def test_known_short_forms_and_close_spellings(self):
        self.assertEqual(self.pick(record('1:1', 'Sword and Fairy', 'steam', ['Steam']), [result(1, 'The Legend of Sword and Fairy', 'tlosf', ['PC (Microsoft Windows)'])]), ('tlosf', 'prefix-word'))
        self.assertEqual(self.pick(record('1:1', 'Looney Tunes Dash!', 'apple', ['iOS']), [result(1, 'Looney Tune Dash', 'ltd', ['iOS'])]), ('ltd', 'fuzzy'))
        self.assertIsNone(self.choice(record('1:1', 'Tetris Worlds 2', 'apple', ['iOS']), [result(1, 'Tetris Worlds 3', 'x', ['iOS'])]))
    def test_a_lone_edition_entry_is_accepted_only_when_the_platform_fits(self):
        edition = result(3, 'Cult of the Lamb: Ultimate Edition', 'x', ['PC (Microsoft Windows)'])
        self.assertEqual(self.pick(record('1:1', 'Cult of the Lamb', 'steam', ['Steam']), [edition]), ('x', 'edition-platform'))
    def test_edition_and_partial_matches_are_never_automatic(self):
        self.assertIsNone(self.choice(record('1:1', 'Cult of the Lamb', 'steam', ['Steam']), [result(3, 'Cult of the Lamb: Ultimate Edition', 'x', ['PlayStation 4'])]))


class RunMatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'data' / 'igdb.json'
        self.state = m.load_state(self.path)
        self.save = lambda: m.save_state(self.path, self.state)
        self.lines = []
    def tearDown(self): self.tmp.cleanup()
    def run_match(self, records, table, **kw):
        client = FakeClient(table)
        out = m.run_match(records, self.state, client, self.save, today='2026-10-29', out=self.lines.append, **kw)
        return out, client
    def test_auto_and_review_outcomes_are_recorded_and_the_file_is_readable_by_the_exporter(self):
        tetris = [result(1, 'Tetris', 'tetris', ['iOS'], ratings=40), result(2, 'Tetris', 'tetris--20', ['iOS'], ratings=30)]
        records = [record('1:1', 'Hades', 'nintendo', ['Switch']), record('2:2', 'Tetris', 'apple', ['iOS']), record('3:3', 'Unknown Thing')]
        out, _ = self.run_match(records, {'hades': [HADES_1995, HADES_2020], 'tetris': tetris})
        self.assertEqual(out, {'processed': 3, 'auto': 1, 'review': 2})
        self.assertEqual(self.state['entries']['1:1']['status'], 'auto')
        self.assertEqual(self.state['entries']['1:1']['via'], 'tiebreak-popularity')
        self.assertEqual(set(self.state['pending']), {'2:2', '3:3'})
        self.assertEqual(self.state['pending']['3:3']['candidates'], [])
        self.assertEqual(m.exporter.load_igdb(self.path), {'1:1': 'https://www.igdb.com/games/hades--1'})
    def test_rerunning_makes_no_requests_for_records_already_decided(self):
        records = [record('1:1'), record('3:3', 'Unknown Thing')]
        self.run_match(records, {'hades': [HADES_2020]})
        _, client = self.run_match(records, {'hades': [HADES_2020]})
        self.assertEqual(client.terms, [])
    def test_reviewed_entries_are_never_overwritten_even_by_a_rematch(self):
        self.state['entries']['1:1'] = {'status': 'confirmed', 'slug': 'my-choice', 'name': 'Mine'}
        self.state['entries']['2:2'] = {'status': 'none'}
        self.run_match([record('1:1'), record('2:2')], {'hades': [HADES_2020]}, rematch_auto=True, retry_pending=True)
        self.assertEqual(self.state['entries']['1:1']['slug'], 'my-choice')
        self.assertEqual(self.state['entries']['2:2'], {'status': 'none'})
    def test_rematch_replaces_only_unreviewed_auto_entries(self):
        self.state['entries']['1:1'] = {'status': 'auto', 'slug': 'old', 'name': 'Old'}
        self.run_match([record('1:1')], {'hades': [HADES_2020]}, rematch_auto=True)
        self.assertEqual(self.state['entries']['1:1']['slug'], 'hades--1')
    def test_only_and_limit_restrict_what_is_searched(self):
        records = [record('1:1', 'A'), record('2:2', 'B'), record('3:3', 'C')]
        _, client = self.run_match(records, {}, only={'2:2'})
        self.assertEqual(client.terms, ['B'])
        self.state['pending'].clear()
        _, client = self.run_match(records, {}, limit=1)
        self.assertEqual(len(client.terms), 1)
    def test_punctuation_free_search_is_tried_when_the_first_search_finds_nothing(self):
        out, client = self.run_match([record('1:1', "Alto's Odyssey: Remastered Gold", 'apple', ['iOS'])], {})
        self.assertEqual(client.terms, ["Alto's Odyssey: Remastered Gold", 'Alto s Odyssey Remastered Gold', "Alto's Odyssey"])
        self.assertEqual(client.exact_terms, ["Alto's Odyssey: Remastered Gold", 'Alto s Odyssey Remastered Gold', "Alto's Odyssey"])
    def test_a_corrupt_existing_file_stops_everything_without_overwriting(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'schema': m.SCHEMA, 'entries': {'1:1': {'status': 'confirmed', 'slug': 'Bad Slug', 'name': 'X'}}}))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): m.load_state(self.path)
        self.assertEqual(self.path.read_bytes(), before)
    def test_saving_preserves_unrelated_top_level_keys_and_orders_entries(self):
        self.state['note'] = 'keep me'
        self.state['entries'] = {'9:9': {'status': 'none'}, '1:1': {'status': 'none'}}
        self.save()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['note'], 'keep me')
        self.assertEqual(list(saved['entries']), ['1:1', '9:9'])
        self.assertNotIn('pending', saved)


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'igdb.json'
        self.state = m.load_state(self.path)
        for rid in ('1:1', '2:2', '3:3', '4:4'):
            self.state['pending'][rid] = m.pending_item(record(rid, f'Game {rid}'), [{**HADES_2020, 'score': 150, 'name_kind': 'exact', 'platform_signal': 'match', 'addon': False}])
    def tearDown(self): self.tmp.cleanup()
    def answers(self, *replies):
        queue = iter(replies)
        return lambda prompt: next(queue)
    def run_review(self, *replies):
        return m.review(self.state, lambda: m.save_state(self.path, self.state), ask=self.answers(*replies), out=lambda *_: None, today='2026-10-29')
    def test_accepting_a_candidate_a_pasted_url_none_and_skip(self):
        resolved = self.run_review('1', 'u', 'https://www.igdb.com/games/pokemon-unbound/', '', 'n', 'only a mod', 's')
        self.assertEqual(resolved, 3)
        entries = self.state['entries']
        self.assertEqual((entries['1:1']['status'], entries['1:1']['slug']), ('confirmed', 'hades--1'))
        self.assertEqual(entries['2:2'], {'status': 'confirmed', 'slug': 'pokemon-unbound', 'name': 'Game 2:2', 'matched_at': '2026-10-29'})
        self.assertEqual(entries['3:3']['status'], 'none')
        self.assertEqual(entries['3:3']['reason'], 'only a mod')
        self.assertEqual(set(self.state['pending']), {'4:4'})
    def test_bad_input_is_re_asked_and_quit_keeps_progress_on_disk(self):
        resolved = self.run_review('banana', '9', 'u', 'https://evil.example/games/x', 'u', 'https://igdb.com/games/x-y', 'My X', 'q')
        self.assertEqual(resolved, 1)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['entries']['1:1']['slug'], 'x-y')
        self.assertEqual(set(saved['pending']), {'2:2', '3:3', '4:4'})
        self.assertEqual(list(m.exporter.load_igdb(self.path)), ['1:1'])
    def test_confirm_auto_promotes_only_auto_entries(self):
        self.state['entries'] = {'1:1': {'status': 'auto', 'slug': 'a', 'name': 'A', 'score': 140}, '2:2': {'status': 'none'}, '3:3': {'status': 'confirmed', 'slug': 'c', 'name': 'C'}}
        self.assertEqual(m.confirm_auto(self.state, lambda: m.save_state(self.path, self.state), today='2026-10-29'), 1)
        self.assertEqual(self.state['entries']['1:1'], {'status': 'confirmed', 'slug': 'a', 'name': 'A', 'confirmed_at': '2026-10-29'})
        self.assertEqual(self.state['entries']['2:2'], {'status': 'none'})


class CredentialsAndClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def file(self, content, folder=None):
        path = (folder or self.folder) / 'creds.json'
        path.write_text(json.dumps(content))
        return path
    def test_only_id_and_secret_are_read_and_other_keys_are_ignored(self):
        path = self.file({'id': 'abc', 'secret': 'xyz', 'csrf': 'COOKIE', '_session': 'COOKIE2'})
        self.assertEqual(m.load_credentials(path, {}), ('abc', 'xyz'))
    def test_environment_is_the_fallback(self):
        self.assertEqual(m.load_credentials(None, {'IGDB_CLIENT_ID': 'a', 'IGDB_CLIENT_SECRET': 'b'}), ('a', 'b'))
    def test_missing_or_empty_values_fail_without_echoing_secrets(self):
        for content in ({'id': 'abc'}, {'id': '', 'secret': 'topsecret'}, {'secret': 'topsecret'}):
            with self.assertRaises(m.CredentialsError) as raised: m.load_credentials(self.file(content), {})
            self.assertNotIn('topsecret', str(raised.exception))
        with self.assertRaises(m.CredentialsError): m.load_credentials(None, {})
        (self.folder / 'bad.json').write_text('{not json')
        with self.assertRaises(m.CredentialsError): m.load_credentials(self.folder / 'bad.json', {})
    def test_a_credentials_file_inside_the_repository_is_refused(self):
        inside = ROOT / 'tests' / '.creds-test.json'
        inside.write_text(json.dumps({'id': 'a', 'secret': 'b'}))
        try:
            with self.assertRaises(m.CredentialsError): m.load_credentials(inside, {})
        finally:
            inside.unlink(missing_ok=True)
    def response(self, status, payload=None, headers=None):
        return Mock(status_code=status, json=Mock(return_value=payload), headers=headers or {})
    def client(self, *replies):
        session = Mock()
        session.post.side_effect = list(replies)
        sleeps = []
        return m.IgdbClient('cid', 'sec', session=session, sleep=sleeps.append, clock=lambda: 100.0), session, sleeps
    def test_secrets_go_in_the_request_body_never_the_url_and_quotes_are_escaped(self):
        token = self.response(200, {'access_token': 'tok'})
        found = self.response(200, [{'id': 1, 'name': 'A', 'slug': 'a'}])
        client, session, _ = self.client(token, found)
        self.assertEqual(len(client.search('Say "hi"; fields *')), 1)
        auth, search = session.post.call_args_list
        self.assertEqual(auth.args[0], m.TOKEN_URL)
        self.assertEqual(auth.kwargs['data']['client_secret'], 'sec')
        self.assertNotIn('sec', auth.args[0])
        self.assertEqual(search.kwargs['headers'], {'Client-ID': 'cid', 'Authorization': 'Bearer tok'})
        body = search.kwargs['data'].decode()
        self.assertIn('search "Say  hi ; fields *";', body)
        self.assertEqual(body.count('"'), 2)
    def test_rate_limits_wait_and_expired_tokens_reauthenticate_once(self):
        ok = self.response(200, [])
        client, session, sleeps = self.client(self.response(200, {'access_token': 't1'}), self.response(429, headers={'Retry-After': '7'}), ok)
        client.search('x')
        self.assertIn(7.0, sleeps)
        client, session, _ = self.client(self.response(200, {'access_token': 't1'}), self.response(401), self.response(200, {'access_token': 't2'}), ok)
        client.search('x')
        self.assertEqual(session.post.call_args_list[-1].kwargs['headers']['Authorization'], 'Bearer t2')
        client, _, _ = self.client(self.response(200, {'access_token': 't'}), self.response(401), self.response(200, {'access_token': 't'}), self.response(401))
        with self.assertRaises(RuntimeError): client.search('x')
    def test_authentication_and_server_failures_raise(self):
        client, _, _ = self.client(self.response(400))
        with self.assertRaises(RuntimeError): client.search('x')
        client, _, _ = self.client(self.response(200, {'access_token': 't'}), self.response(500))
        with self.assertRaises(RuntimeError): client.search('x')


class CommandLine(unittest.TestCase):
    def test_missing_credentials_fail_cleanly_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data.json'
            data.write_text(json.dumps({'games': [{'id': '1:1', 'title': 'Hades', 'service': 'steam', 'platforms': ['Steam']}]}))
            igdb = Path(folder) / 'igdb.json'
            self.assertEqual(m.main(['--data', str(data), '--igdb-file', str(igdb), 'match']), 1)
            self.assertFalse(igdb.exists())
            self.assertEqual(m.main(['--data', str(data), '--igdb-file', str(igdb), 'match', '--only', 'nonsense']), 1)
            self.assertEqual(m.main(['--data', str(data), '--igdb-file', str(igdb), 'report']), 0)
    def test_report_counts_statuses_and_stale_entries(self):
        state = {'entries': {'1:1': {'status': 'auto', 'slug': 'a', 'name': 'A'}, '9:9': {'status': 'none'}}, 'pending': {'2:2': {}}}
        lines = []
        counts = m.report(state, [record('1:1'), record('2:2'), record('3:3')], show='auto', out=lines.append)
        self.assertEqual(counts, {'confirmed': 0, 'auto': 1, 'none': 1})
        self.assertIn('1 awaiting review, 1 not yet processed', lines[0])
        self.assertTrue(any('no longer in the dataset' in l for l in lines))


class CacheAndReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def ok(self, payload): return Mock(status_code=200, json=Mock(return_value=payload), headers={})
    def client(self, replies, **kw):
        session = Mock()
        session.post.side_effect = [self.ok({'access_token': 't'})] + list(replies)
        return m.IgdbClient('cid', 'sec', session=session, sleep=lambda s: None, clock=lambda: 100.0, **kw), session
    def test_responses_are_cached_to_disk_and_replayed_without_network_or_credentials(self):
        path = self.folder / 'cache.json'
        raw = [{'id': 1, 'name': 'Hades', 'slug': 'hades--1', 'total_rating_count': 1794}]
        client, session = self.client([self.ok(raw)], cache_path=path)
        self.assertEqual(client.search('Hades')[0]['ratings'], 1794)
        client.search('Hades')
        self.assertEqual(session.post.call_count, 2)  # token plus one search
        client.save_cache()
        offline = m.IgdbClient(cache_path=path, offline=True)
        self.assertEqual(offline.search('Hades')[0]['slug'], 'hades--1')
        self.assertEqual(offline.search('Never searched'), [])
    def test_exact_name_and_slug_lookups_use_the_expected_queries(self):
        client, session = self.client([self.ok([]), self.ok([{'id': 1, 'name': 'A', 'slug': 'a'}]), self.ok([])])
        client.exact('Pokémon Masters EX')
        self.assertIn('where name ~ "Pokémon Masters EX";', session.post.call_args.kwargs['data'].decode())
        found = client.by_slugs([f's{i}' for i in range(12)])
        self.assertEqual(session.post.call_count, 4)  # token, exact, two slug chunks
        self.assertEqual(list(found), ['a'])
    def test_replay_counts_agreement_conflicts_and_misses_without_changing_anything(self):
        records = [record('1:1', 'Hades', 'steam', ['Steam']), record('2:2', 'Tetris', 'apple', ['iOS']), record('3:3', 'Zelda Thing', 'steam', ['Steam']), record('4:4', 'Pending One', 'apple', ['iOS'])]
        state = {'entries': {'1:1': {'status': 'confirmed', 'slug': 'hades--1', 'name': 'Hades'}, '2:2': {'status': 'confirmed', 'slug': 'tetris', 'name': 'Tetris'},
                             '3:3': {'status': 'none'}}, 'pending': {'4:4': {}}}
        before = json.dumps(state, sort_keys=True)
        wrong = [result(1, 'Tetris', 'tetris--20', ['iOS'], ratings=500), result(2, 'Tetris', 'tetris', ['iOS'], ratings=3)]
        client = FakeClient({'hades': [HADES_1995, HADES_2020], 'tetris': wrong, 'pending one': [result(5, 'Pending One', 'pending-one', ['iOS'])]})
        lines = []
        outcome = m.replay(records, state, client, out=lines.append)
        self.assertEqual([r[0]['id'] for r in outcome['agree']], ['1:1'])
        self.assertEqual([r[0]['id'] for r in outcome['conflict']], ['2:2'])
        self.assertEqual([r[0]['id'] for r in outcome['review']], ['3:3'])
        self.assertEqual([r[0]['id'] for r in outcome['new-auto']], ['4:4'])
        self.assertTrue(any('CONFLICT' in l and 'tetris--20' in l for l in lines))
        self.assertEqual(json.dumps(state, sort_keys=True), before)
    def test_verify_reports_missing_slugs_and_fills_only_incomplete_entries(self):
        state = {'entries': {'1:1': {'status': 'confirmed', 'slug': 'gone', 'name': 'Gone'},
                             '2:2': {'status': 'confirmed', 'slug': 'pasted', 'name': 'My typed name'},
                             '3:3': {'status': 'auto', 'slug': 'full', 'name': 'Full', 'igdb_id': 3, 'platforms': ['Mac']},
                             '4:4': {'status': 'none'}}, 'pending': {}}
        client = FakeClient({'slug:pasted': result(22, 'Real Name', 'pasted', ['iOS']), 'slug:full': result(3, 'Full', 'full', ['Mac'])})
        lines = []
        report_only = m.verify(state, client, fix=False, out=lines.append)
        self.assertEqual(report_only, {'missing': 1, 'renamed': 1, 'filled': 0})
        self.assertEqual(state['entries']['2:2']['name'], 'My typed name')
        fixed = m.verify(state, client, fix=True, out=lines.append)
        self.assertEqual(fixed['filled'], 1)
        self.assertEqual(state['entries']['2:2'], {'status': 'confirmed', 'slug': 'pasted', 'name': 'Real Name', 'igdb_id': 22, 'platforms': ['iOS']})
        self.assertEqual(state['entries']['3:3']['igdb_id'], 3)
        self.assertTrue(any('MISSING' in l and 'gone' in l for l in lines))
    def test_review_offers_a_search_link_when_there_are_no_candidates(self):
        state = {'entries': {}, 'pending': {'1:1': m.pending_item(record('1:1', 'Thronefall - A Little Kingdom', 'apple', ['iOS']), [])}}
        lines = []
        m.review(state, lambda: None, ask=lambda prompt: 'q', out=lines.append)
        self.assertTrue(any('igdb.com/search?type=1&q=Thronefall' in l for l in lines))


if __name__ == '__main__': unittest.main()
