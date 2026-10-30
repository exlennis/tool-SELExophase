'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const L = require('../assets/logic.js');

const record = (id, fields = {}) => ({id: String(id), title: `Game ${id}`, service: 'steam', platforms: ['Steam'], hours: null, playtime_minutes: null,
  earned_awards: 0, total_awards: 0, achievement_completion_percent: 0, last_played: null, artwork: null, activity_status: 'unknown', source_url: null, ...fields});
const dataset = games => ({schema: 'selfexophase-telemetry/v1', games, metadata: {total_games: games.length, total_known_hours: 0, unknown_playtime_count: 0, last_updated: '2026-10-28T00:00:00+00:00', source_retrieved_at: null}});
const prepared = games => L.prepare(games);

test('search folds accents, case, curly quotes and dashes in both directions', () => {
  const games = prepared([record(1, {title: 'Pokémon Violet'}), record(2, {title: 'Zelda: Link’s Awakening'}), record(3, {title: 'Alto\'s Odyssey — Remastered'}), record(4, {title: 'Pokemon Emerald Version'})]);
  const ids = query => L.filterAndSort(games, {query, sort: 'title'}).map(g => g.id);
  assert.deepEqual(ids('pokemon'), ['4', '1']);
  assert.deepEqual(ids('POKÉMON'), ['4', '1']);
  assert.deepEqual(ids("link's"), ['2']);
  assert.deepEqual(ids('link’s'), ['2']);
  assert.deepEqual(ids('alto’s odyssey - remastered'), ['3']);
  assert.deepEqual(ids('nothing like this'), []);
});

test('search also matches the service and release platforms', () => {
  const games = prepared([record(1, {service: 'nintendo', platforms: ['Switch 2']}), record(2, {service: 'apple', platforms: ['iOS']})]);
  assert.deepEqual(L.filterAndSort(games, {query: 'switch'}).map(g => g.id), ['1']);
  assert.deepEqual(L.filterAndSort(games, {query: 'apple'}).map(g => g.id), ['2']);
});

test('achievement states keep no set, unknown, zero, progress and complete apart', () => {
  const state = (earned, total) => L.achievementState({earned_awards: earned, total_awards: total});
  assert.equal(state(0, 0), 'none');
  assert.equal(state(null, 0), 'none');
  assert.equal(state(null, null), 'unknown');
  assert.equal(state(3, null), 'unknown');
  assert.equal(state(0, 48), 'zero');
  assert.equal(state(9, 48), 'progress');
  assert.equal(state(48, 48), 'complete');
});

test('progress that rounds to zero is not shown as 0 percent, and counts are always shown', () => {
  const small = L.achievementView({earned_awards: 1, total_awards: 105, achievement_completion_percent: 0});
  assert.equal(small.state, 'progress');
  assert.equal(small.counts, '1 / 105');
  assert.equal(small.percent, '<1%');
  assert.ok(small.fill >= 2);
  const zero = L.achievementView({earned_awards: 0, total_awards: 48, achievement_completion_percent: 0});
  assert.deepEqual([zero.state, zero.counts, zero.percent, zero.fill], ['zero', '0 / 48', '0%', 0]);
  assert.equal(L.achievementView({earned_awards: 0, total_awards: 0, achievement_completion_percent: 0}).counts, null);
});

test('the source percentage is shown as given when it differs from the count ratio', () => {
  const view = L.achievementView({earned_awards: 6, total_awards: 79, achievement_completion_percent: 6});
  assert.equal(view.percent, '6%');
});

test('achievement sort puts real progress first, then unknown, then records without a set', () => {
  const games = prepared([
    record(1, {earned_awards: 0, total_awards: 0}),
    record(2, {earned_awards: null, total_awards: null, achievement_completion_percent: null}),
    record(3, {earned_awards: 0, total_awards: 40, achievement_completion_percent: 0}),
    record(4, {earned_awards: 20, total_awards: 40, achievement_completion_percent: 50}),
    record(5, {earned_awards: 40, total_awards: 40, achievement_completion_percent: 100}),
    record(6, {earned_awards: 1, total_awards: 105, achievement_completion_percent: 0})]);
  assert.deepEqual(L.filterAndSort(games, {sort: 'achievements'}).map(g => g.id), ['5', '4', '6', '3', '2', '1']);
});

test('unknown playtime and last played sort after known values in either direction', () => {
  const games = prepared([record(1), record(2, {hours: 5, last_played: '2020-01-01T00:00:00+00:00'}), record(3, {hours: 0.5, last_played: '2026-01-01T00:00:00+00:00'}), record(4, {hours: 0, last_played: '2019-01-01T00:00:00+00:00'})]);
  const order = sort => L.filterAndSort(games, {sort}).map(g => g.id);
  assert.deepEqual(order('hours'), ['2', '3', '4', '1']);
  assert.deepEqual(order('recent'), ['3', '2', '4', '1']);
  assert.deepEqual(order('oldest'), ['4', '2', '3', '1']);
});

test('title sort is natural and ignores case and accents', () => {
  const games = prepared([record(1, {title: 'Game 10'}), record(2, {title: 'Game 2'}), record(3, {title: 'Émile'}), record(4, {title: 'emma'})]);
  assert.deepEqual(L.filterAndSort(games, {sort: 'title'}).map(g => g.title), ['Émile', 'emma', 'Game 2', 'Game 10']);
});

test('service filter uses the underlying service value, and counts come from the data', () => {
  const games = prepared([record(1, {service: 'apple'}), record(2, {service: 'apple'}), record(3, {service: 'psn'}), record(4, {service: 'retro'})]);
  assert.deepEqual(L.filterAndSort(games, {service: 'apple'}).map(g => g.id), ['1', '2']);
  assert.deepEqual(L.serviceCounts(games).map(([s, n]) => [s, n]), [['apple', 2], ['psn', 1], ['retro', 1]]);
  assert.ok(!L.serviceCounts(games).some(([s]) => s === 'epic'));
});

test('summary denominators are derived from the records', () => {
  const games = prepared([
    record(1, {hours: 2, activity_status: 'played', earned_awards: 5, total_awards: 5, achievement_completion_percent: 100, last_played: '2026-01-01T00:00:00+00:00'}),
    record(2, {earned_awards: 0, total_awards: 10}), record(3), record(4, {earned_awards: null, total_awards: null})]);
  assert.deepEqual(L.summarise(games), {records: 4, hoursKnown: 1, playEvidence: 1, lastPlayedKnown: 1, services: 1, setsComplete: 1, setsKnown: 2, noSet: 1, achievementsUnknown: 1});
});

test('validation accepts older data without the new fields and rejects bad new fields', () => {
  const old = record(1); delete old.total_awards; delete old.last_played; delete old.artwork;
  assert.doesNotThrow(() => L.validate(dataset([old])));
  assert.doesNotThrow(() => L.validate(dataset([record(1, {artwork: 'output/art/apple-abc-v1.png', last_played: '2026-01-01T00:00:00+00:00', total_awards: 3, earned_awards: 3})])));
  for (const bad of [{artwork: 'https://evil.example/x.png'}, {artwork: '../x.png'}, {artwork: 'output/art/../../x.png'}, {artwork: 'output/art/a.svg'}, {last_played: 'later'}, {total_awards: -1}, {total_awards: 1.5}, {earned_awards: 5, total_awards: 4}]) {
    assert.throws(() => L.validate(dataset([record(1, bad)])), /invalid|could not/i, JSON.stringify(bad));
  }
});

test('title links go to IGDB when matched and fall back to Exophase otherwise', () => {
  const exophase = 'https://www.exophase.com/game/black-myth-wukong-psn/trophies/#5315273';
  assert.deepEqual(L.titleLink({igdb_url: 'https://www.igdb.com/games/hades--1', source_url: exophase}), {url: 'https://www.igdb.com/games/hades--1', label: 'IGDB'});
  assert.deepEqual(L.titleLink({igdb_url: null, source_url: exophase}), {url: exophase, label: 'Exophase'});
  assert.deepEqual(L.titleLink({source_url: exophase}), {url: exophase, label: 'Exophase'});
  assert.equal(L.titleLink({igdb_url: null, source_url: null}), null);
  assert.equal(L.titleLink({igdb_url: null, source_url: 'http://www.exophase.com/x'}), null);
  assert.equal(L.titleLink({igdb_url: null, source_url: 'https://evil.example/exophase.com'}), null);
  assert.equal(L.titleLink({igdb_url: 'https://evil.example/games/hades', source_url: null}), null);
  assert.equal(L.titleLink({igdb_url: 'https://www.igdb.com.evil.example/games/hades', source_url: null}), null);
});

test('validation accepts IGDB links only on the IGDB games path', () => {
  assert.doesNotThrow(() => L.validate(dataset([record(1, {igdb_url: 'https://www.igdb.com/games/hades--1'}), record(2, {igdb_url: null})])));
  for (const bad of ['http://www.igdb.com/games/hades', 'https://www.igdb.com/search?q=x', 'https://www.igdb.com/games/Hades', 'https://evil.example/games/hades', 'javascript:alert(1)', 7]) {
    assert.throws(() => L.validate(dataset([record(1, {igdb_url: bad})])), /IGDB link/i, String(bad));
  }
});
