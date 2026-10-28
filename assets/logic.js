'use strict';
// Pure data rules for the dashboard: no DOM access, so they can be tested with Node.
(function (root) {
  const services = {apple:'Apple',gamecenter:'Apple',steam:'Steam',psn:'PSN',xbox:'Xbox',nintendo:'Nintendo',gog:'GOG',epic:'Epic',retro:'RetroAchievements',retroachievements:'RetroAchievements'};
  const serviceName = value => services[value] || value || 'Unknown';
  const collator = new Intl.Collator('en-AU', {sensitivity:'base', numeric:true});
  const artworkPattern = /^output\/art\/[A-Za-z0-9_-]+\.(?:png|jpg|webp)$/;

  // Searching ignores case, accents, curly versus straight quotes and the different dash characters.
  function fold(value) {
    return String(value ?? '').normalize('NFKD').replace(/\p{M}/gu, '')
      .replace(/[‘’‛ʼ′]/g, "'").replace(/[“”‟]/g, '"')
      .replace(/[‐-―−]/g, '-').toLocaleLowerCase('en-AU');
  }
  const validNumber = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const validCount = value => Number.isInteger(value) && value >= 0;
  const validTime = value => typeof value === 'string' && Number.isFinite(Date.parse(value));

  function validate(data) {
    if (data?.schema !== 'selfexophase-telemetry/v1' || !Array.isArray(data.games) || !data.metadata || data.metadata.total_games !== data.games.length) throw new Error('The dataset format is not supported.');
    const ids = new Set();
    for (const game of data.games) {
      if (typeof game.id !== 'string' || ids.has(game.id) || typeof game.title !== 'string' || typeof game.service !== 'string' || !Array.isArray(game.platforms) || game.platforms.some(p => typeof p !== 'string')) throw new Error('A source record could not be read.');
      if (game.hours !== null && !validNumber(game.hours)) throw new Error('Playtime data is invalid.');
      if (game.achievement_completion_percent !== null && (!validNumber(game.achievement_completion_percent) || game.achievement_completion_percent > 100)) throw new Error('Achievement data is invalid.');
      const {earned_awards: earned = null, total_awards: total = null, last_played: played = null, artwork = null} = game;
      if ((earned !== null && !validCount(earned)) || (total !== null && !validCount(total)) || (earned !== null && total !== null && earned > total)) throw new Error('Achievement counts are invalid.');
      if (played !== null && !validTime(played)) throw new Error('A last-played time is invalid.');
      if (artwork !== null && !(typeof artwork === 'string' && artworkPattern.test(artwork))) throw new Error('An artwork path is invalid.');
      ids.add(game.id);
    }
    if (!validNumber(data.metadata.total_known_hours) || !validNumber(data.metadata.unknown_playtime_count)) throw new Error('The activity summary could not be read.');
    if (!validTime(data.metadata.last_updated)) throw new Error('The dataset timestamp is invalid.');
    if (data.metadata.source_retrieved_at !== null && !validTime(data.metadata.source_retrieved_at)) throw new Error('The source timestamp is invalid.');
  }

  // Fill optional fields and precompute the folded search text once per record.
  function prepare(games) {
    return games.map(game => ({
      ...game,
      earned_awards: game.earned_awards ?? null,
      total_awards: game.total_awards ?? null,
      last_played: game.last_played ?? null,
      artwork: game.artwork ?? null,
      search: fold([game.title, serviceName(game.service), ...game.platforms].join(' '))
    }));
  }

  // none: the source lists no achievement set. unknown: counts missing. zero: a set exists, nothing earned.
  // progress: some earned. complete: every achievement in the set earned.
  function achievementState(game) {
    const total = game.total_awards ?? null, earned = game.earned_awards ?? null;
    if (total === 0) return 'none';
    if (!Number.isInteger(total) || !Number.isInteger(earned)) return 'unknown';
    if (earned >= total) return 'complete';
    return earned === 0 ? 'zero' : 'progress';
  }

  // The source percentage can differ from earned/total (some services weight achievements), so it is shown as given,
  // except that real progress which rounds to zero is shown as "<1%" rather than "0%".
  function achievementView(game) {
    const state = achievementState(game);
    if (state === 'none' || state === 'unknown') return {state, counts: null, percent: null, fill: null};
    const percent = game.achievement_completion_percent;
    const ratio = 100 * game.earned_awards / game.total_awards;
    const shown = percent ?? ratio;
    const text = state === 'progress' && shown < 1 ? '<1%' : `${Math.round(shown * 10) / 10}%`;
    return {state, counts: `${game.earned_awards} / ${game.total_awards}`, percent: text, fill: state === 'progress' ? Math.max(shown, 2) : shown};
  }

  const achievementRank = game => ({complete: 0, progress: 0, zero: 0, unknown: 1, none: 2})[achievementState(game)];
  function compareAchievements(a, b) {
    const rank = achievementRank(a) - achievementRank(b);
    if (rank) return rank;
    if (achievementRank(a) === 0) {
      const pa = a.achievement_completion_percent ?? 100 * a.earned_awards / a.total_awards, pb = b.achievement_completion_percent ?? 100 * b.earned_awards / b.total_awards;
      return (pb - pa) || (b.earned_awards / b.total_awards - a.earned_awards / a.total_awards);
    }
    return 0;
  }
  const byKnown = (a, b, direction) => a === null || b === null ? (a === null) - (b === null) : direction * (a - b);
  const time = game => game.last_played === null ? null : Date.parse(game.last_played);
  const comparators = {
    hours: (a, b) => byKnown(a.hours, b.hours, -1),
    title: () => 0,
    achievements: compareAchievements,
    recent: (a, b) => byKnown(time(a), time(b), -1),
    oldest: (a, b) => byKnown(time(a), time(b), 1)
  };

  function filterAndSort(games, {query = '', service = '', sort = 'hours'} = {}) {
    const needle = fold(query).trim();
    const compare = comparators[sort] || comparators.hours;
    return games.filter(g => (!service || g.service === service) && (!needle || g.search.includes(needle)))
      .sort((a, b) => compare(a, b) || collator.compare(a.title, b.title) || (a.id < b.id ? -1 : 1));
  }

  function serviceCounts(games) {
    const counts = new Map();
    for (const game of games) counts.set(game.service, (counts.get(game.service) || 0) + 1);
    return [...counts].sort((a, b) => collator.compare(serviceName(a[0]), serviceName(b[0])));
  }

  // Coverage figures are derived from the records themselves, never hard-coded.
  function summarise(games) {
    const states = games.map(achievementState);
    const withSet = states.filter(s => s === 'complete' || s === 'progress' || s === 'zero').length;
    return {
      records: games.length,
      hoursKnown: games.filter(g => g.hours !== null).length,
      playEvidence: games.filter(g => g.activity_status === 'played').length,
      lastPlayedKnown: games.filter(g => g.last_played !== null).length,
      services: new Set(games.map(g => g.service)).size,
      setsComplete: states.filter(s => s === 'complete').length,
      setsKnown: withSet,
      noSet: states.filter(s => s === 'none').length,
      achievementsUnknown: states.filter(s => s === 'unknown').length
    };
  }

  const api = {serviceName, fold, validate, prepare, achievementState, achievementView, filterAndSort, serviceCounts, summarise, collator};
  root.ExophaseLogic = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(globalThis);
