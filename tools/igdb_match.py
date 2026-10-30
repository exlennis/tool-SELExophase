#!/usr/bin/env python3
"""Match Exophase source records to IGDB entries and maintain data/igdb.json.

Run by hand on a laptop, never in CI. The exporter only reads the file this script writes.
Reviewed entries (confirmed or none) are never overwritten.
"""

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import headless_exporter as exporter  # noqa: E402  (validates every file this script writes)

SCHEMA = exporter.IGDB_SCHEMA
DEFAULT_NOTE = 'Maps Exophase source IDs (player:game) to IGDB entries. Records not listed link to Exophase. Managed by tools/igdb_match.py.'
TOKEN_URL = 'https://id.twitch.tv/oauth2/token'
API_URL = 'https://api.igdb.com/v4/games'
FIELDS = 'name, slug, game_type, first_release_date, total_rating_count, platforms.name'
MIN_INTERVAL = 0.3  # IGDB allows about 4 requests a second
TIMEOUT = 30
SAVE_EVERY = 20
IGDB_URL = re.compile(r'https://(?:www\.)?igdb\.com/games/([a-z0-9][a-z0-9_-]{0,127})/?(?:[?#].*)?')

# Exophase platform name -> IGDB platform names. A platform not listed here gives no evidence either way.
PLATFORMS = {
    'Steam': {'PC (Microsoft Windows)'}, 'GOG': {'PC (Microsoft Windows)'}, 'iOS': {'iOS'}, 'macOS': {'Mac'},
    'PS5': {'PlayStation 5'}, 'PS4': {'PlayStation 4'}, 'PS2': {'PlayStation 2'}, 'PS1': {'PlayStation'}, 'PS VR2': {'PlayStation VR2'},
    'Switch': {'Nintendo Switch'}, 'Switch 2': {'Nintendo Switch 2'}, 'NDS': {'Nintendo DS'}, 'N64': {'Nintendo 64'},
    'NES': {'Nintendo Entertainment System'}, 'SNES': {'Super Nintendo Entertainment System'}, 'GBA': {'Game Boy Advance'},
    'GBC': {'Game Boy Color'}, 'Game Boy': {'Game Boy'}, 'Virtual Boy': {'Virtual Boy'}, 'Atari 2600': {'Atari 2600'},
    'Arcade': {'Arcade'}, 'Genesis': {'Sega Mega Drive/Genesis'}, 'Xbox One': {'Xbox One'}, 'Xbox 360': {'Xbox 360'},
    'Xbox Series X|S': {'Xbox Series X|S'}, 'Xbox': {'Xbox'},
}
# Platforms that IGDB often lists inconsistently across generations or Apple devices. A shared family is weaker
# evidence than an exact platform, but it is not a contradiction.
FAMILIES = {
    'apple': {'iOS', 'Mac', 'tvOS', 'visionOS'},
    'playstation': {'PlayStation 4', 'PlayStation 5', 'PlayStation VR', 'PlayStation VR2'},
    'switch': {'Nintendo Switch', 'Nintendo Switch 2'},
    'xbox': {'Xbox One', 'Xbox Series X|S'},
    'pc': {'PC (Microsoft Windows)', 'Linux', 'DOS'},
}
# IGDB game types that are add-ons to another game. Mods, ports, remakes and the rest are treated equally.
ADDON_TYPES = {1, 2, 6, 7, 14}
MAIN_GAME = 0
EDITION_WORDS = {'edition', 'remastered', 'remaster', 'definitive', 'deluxe', 'complete', 'ultimate', 'gold', 'enhanced', 'goty'}
NOISE_WORDS = {'freeplay', 'hd', 'dx', 'ex', 'classic', 'remastered', 'lite', 'free', 'launch', 'edition'}
MARKETING_WORDS = {'launch', 'update', 'season', 'edition', 'remastered', 'remaster', 'hd', 'anniversary', 'definitive', 'deluxe', 'complete', 'collection', 'beta'}
ROMAN = {'ii': '2', 'iii': '3', 'iv': '4', 'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9', 'x': '10', 'xi': '11', 'xii': '12', 'xiii': '13'}
IMPUTED_PREFIXES = ('super ', 'the ', 'the legend of ', 'legend of ')

# Name kinds, with the score they contribute. Strict kinds identify the same title and can be accepted
# automatically on their own; sole kinds only when they are the single plausible candidate.
NAME_SCORES = {'exact': 100, 'article': 90, 'arcade-plus': 95, 'compact': 88, 'derived': 85, 'edition': 80, 'fuzzy': 70,
               'subtitle': 60, 'prefix-word': 60, 'derived-noise': 60, 'derived-weak': 45, 'partial': 30}
STRICT = ('exact', 'article', 'arcade-plus', 'compact', 'derived')
SOLE = ('subtitle', 'prefix-word', 'fuzzy', 'derived-noise')
POPULARITY_MIN = 10   # ratings the leading candidate needs before popularity can break a tie
POPULARITY_RATIO = 5  # and it must have this many times the runner-up's ratings


# ---- Names and scoring (pure functions) ----

def clean_title(title):
    """Drop Exophase decorations (the ~Hack~ marker, trailing tags, the Apple Arcade "AA" marker) and fix ", The"."""
    text = re.sub(r'~[^~]*~', ' ', title)
    text = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*$', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s*(?<![A-Z])AA$', '', text)
    found = re.fullmatch(r'(.+), (The|A|An)', text)
    return f'{found[2]} {found[1]}' if found else text


def year_hint(title):
    found = re.search(r'\(((?:19|20)\d\d)\)\s*$', title)
    return int(found[1]) if found else None


def normalise(text):
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower().replace('&', ' and ').replace('+', ' plus ')
    words = [ROMAN.get(w, w) for w in re.sub(r'[^a-z0-9]+', ' ', text).split()]
    return ' '.join(words)


def _compact(text):
    return text.replace(' ', '')


def _base(text):
    return ' '.join(w for w in text.split() if w not in EDITION_WORDS)


def title_variants(title):
    """Shorter forms of a title that may be the real game name, as (text, kind) pairs."""
    cleaned = clean_title(title)
    variants = []
    parts = re.split(r'\s+[-–—|]\s+', cleaned, maxsplit=1)
    if len(parts) == 2:
        variants.append((parts[0], 'tagline', parts[1]))
    if ':' in cleaned:
        head, _, tail = cleaned.partition(':')
        variants.append((head.strip(), 'colon', tail.strip()))
    tokens = cleaned.split()
    while len(tokens) >= 2 and normalise(tokens[-1]) in NOISE_WORDS:
        tokens.pop()
        variants.append((' '.join(tokens), 'noise', ''))
        break
    out = []
    for text, label, rest in variants:
        text = text.strip()
        if not text or len(text) < 3:
            continue
        specific = len(text.split()) >= 2 or len(text) >= 8
        if label == 'noise':
            kind = 'derived-noise'
        elif label == 'tagline':
            kind = 'derived' if specific else 'derived-weak'
        else:
            kind = 'derived' if specific and (set(normalise(rest).split()) & MARKETING_WORDS) else 'derived-weak'
        out.append((text, kind))
    return out


def name_match(record_title, candidate_name):
    """Return (score, kind) describing how closely the IGDB name matches the record title."""
    a, b = normalise(clean_title(record_title)), normalise(candidate_name)
    if not a or not b:
        return 0, 'none'
    if a == b:
        return NAME_SCORES['exact'], 'exact'
    strip = lambda s: s[4:] if s.startswith('the ') else s
    if strip(a) == strip(b):
        return NAME_SCORES['article'], 'article'
    if a.endswith(' plus') and a[:-5] == b:  # Apple Arcade catalogue titles end in "+"
        return NAME_SCORES['arcade-plus'], 'arcade-plus'
    if _compact(a) == _compact(b):
        return NAME_SCORES['compact'], 'compact'
    for text, kind in title_variants(record_title):
        v = normalise(text)
        if v and (v == b or _compact(v) == _compact(b)):
            return NAME_SCORES[kind], kind
    if _base(a) and _base(a) == _base(b):
        return NAME_SCORES['edition'], 'edition'
    if any(a == p + b or b == p + a for p in IMPUTED_PREFIXES):
        return NAME_SCORES['prefix-word'], 'prefix-word'
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) < len(long_) and long_[:len(short)] == short:
        if len(short) >= 2 and not long_[len(short)].isdigit() and long_[len(short):] != ['plus']:
            return NAME_SCORES['subtitle'], 'subtitle'
        return NAME_SCORES['partial'], 'partial'
    ca, cb = _compact(a), _compact(b)
    if min(len(ca), len(cb)) >= 8 and re.findall(r'\d+', a) == re.findall(r'\d+', b) and difflib.SequenceMatcher(None, ca, cb).ratio() >= 0.94:
        return NAME_SCORES['fuzzy'], 'fuzzy'
    return 0, 'none'


def platform_signal(record_platforms, candidate_platforms):
    expected = set().union(*(PLATFORMS.get(p, set()) for p in record_platforms)) if record_platforms else set()
    if not expected or not candidate_platforms:
        return 0, 'unknown'
    candidate = set(candidate_platforms)
    if expected & candidate:
        return 40, 'match'
    families = lambda names: {f for f, members in FAMILIES.items() if members & names}
    return (15, 'family') if families(expected) & families(candidate) else (-30, 'mismatch')


def rank_candidates(record, results):
    """Score IGDB search results (already parsed) for one record, best first."""
    ranked = []
    for index, result in enumerate(results):
        score, kind = name_match(record['title'], result['name'])
        if score == 0:
            continue
        platform, signal = platform_signal(record['platforms'], result['platforms'])
        penalty = -40 if result.get('game_type') in ADDON_TYPES else 0
        ranked.append({**result, 'score': score + platform + max(0, 10 - index) + penalty, 'name_kind': kind, 'platform_signal': signal, 'addon': bool(penalty)})
    return sorted(ranked, key=lambda c: -c['score'])


def tie_break(record, contenders, siblings):
    """Choose between candidates that match the title equally well, or return (None, None).

    Order matters: IGDB platform lists are often incomplete, so platform is the last resort, used only when
    popularity cannot separate the candidates and exactly one fits while every other contradicts.
    """
    hint = year_hint(record['title'])
    if hint is not None:
        dated = [c for c in contenders if c['year'] == hint]
        if len(dated) == 1:
            return dated[0], 'tiebreak-year'
    known = siblings.get(normalise(clean_title(record['title'])), set())
    shared = [c for c in contenders if c['slug'] in known]
    if len(shared) == 1:
        return shared[0], 'tiebreak-sibling'
    mains = [c for c in contenders if c.get('game_type') == MAIN_GAME]
    if len(mains) == 1:
        return mains[0], 'tiebreak-type'
    ordered = sorted(contenders, key=lambda c: -(c.get('ratings') or 0))
    top, second = (ordered[0].get('ratings') or 0), (ordered[1].get('ratings') or 0)
    if top >= POPULARITY_MIN and top >= POPULARITY_RATIO * max(second, 1):
        return ordered[0], 'tiebreak-popularity'
    fits = [c for c in contenders if c['platform_signal'] == 'match']
    if len(fits) == 1 and all(c['platform_signal'] == 'mismatch' for c in contenders if c is not fits[0]):
        return fits[0], 'tiebreak-platform'
    return None, None


def decide(record, ranked, siblings=None):
    """Return (candidate, how) for a match to accept automatically, or (None, None) when a person should look."""
    siblings = siblings or {}
    usable = [c for c in ranked if not c['addon']]
    strict = [c for c in usable if c['name_kind'] in STRICT]
    if strict:
        top_score = max(NAME_SCORES[c['name_kind']] for c in strict)
        contenders = [c for c in strict if NAME_SCORES[c['name_kind']] == top_score]
        if len(contenders) == 1:
            chosen, how = contenders[0], contenders[0]['name_kind']
        else:
            chosen, how = tie_break(record, contenders, siblings)
        if chosen and chosen['platform_signal'] != 'mismatch':
            return chosen, how
        if any(c['platform_signal'] != 'mismatch' for c in strict):
            return None, None
    # No same-title entry fits the platform: a named edition that does can stand in for it (a console release).
    editions = [c for c in usable if c['name_kind'] == 'edition' and c['platform_signal'] in ('match', 'family')]
    if len(editions) == 1:
        return editions[0], 'edition-platform'
    sole = [c for c in usable if c['name_kind'] in SOLE and c['platform_signal'] in ('match', 'family')]
    plausible = {c['slug'] for c in ranked if c['score'] >= 60}
    if len(sole) == 1 and plausible == {sole[0]['slug']} and not [c for c in ranked if c['slug'] != sole[0]['slug'] and c['name_kind'] != 'partial']:
        return sole[0], sole[0]['name_kind']
    return None, None


def year_of(timestamp):
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).year if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) else None
    except (OverflowError, OSError, ValueError):
        return None


def parse_result(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), int) or not isinstance(raw.get('name'), str) or not isinstance(raw.get('slug'), str):
        return None
    if not exporter.IGDB_SLUG.fullmatch(raw['slug']):
        return None
    platforms = [p['name'] for p in raw.get('platforms') or [] if isinstance(p, dict) and isinstance(p.get('name'), str)]
    ratings = raw.get('total_rating_count')
    return {'igdb_id': raw['id'], 'slug': raw['slug'], 'name': raw['name'], 'game_type': raw.get('game_type') if isinstance(raw.get('game_type'), int) else None,
            'year': year_of(raw.get('first_release_date')), 'platforms': platforms,
            'ratings': ratings if isinstance(ratings, int) and not isinstance(ratings, bool) else None}


# ---- IGDB access ----

class CredentialsError(Exception):
    pass


def load_credentials(path=None, environ=None):
    """Read the Twitch client ID and secret from an explicit file outside this repository, or the environment.

    Only the id and secret are used; any other keys in the file are ignored. Values are never echoed.
    """
    environ = os.environ if environ is None else environ
    if path:
        resolved = Path(path).expanduser().resolve()
        if resolved == ROOT or ROOT in resolved.parents:
            raise CredentialsError('Refusing a credentials file inside this repository; keep it outside so it cannot be committed')
        try:
            data = json.loads(resolved.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise CredentialsError('The credentials file could not be read as JSON')
        client_id, secret = (data.get('id') or data.get('client_id')), (data.get('secret') or data.get('client_secret'))
    else:
        client_id, secret = environ.get('IGDB_CLIENT_ID'), environ.get('IGDB_CLIENT_SECRET')
    if not isinstance(client_id, str) or not isinstance(secret, str) or not client_id.strip() or not secret.strip():
        raise CredentialsError('A Twitch client ID and secret are required (use --credentials FILE, or IGDB_CLIENT_ID and IGDB_CLIENT_SECRET)')
    return client_id.strip(), secret.strip()


def _quote(term):
    return term.replace('\\', ' ').replace('"', ' ')


class IgdbClient:
    """Rate-limited IGDB client. An optional cache file stores raw responses so results can be replayed offline."""

    def __init__(self, client_id=None, secret=None, session=None, sleep=time.sleep, clock=time.monotonic, cache_path=None, offline=False):
        self.client_id, self.secret = client_id, secret
        self.session, self.sleep, self.clock = session or requests.Session(), sleep, clock
        self.token, self.last = None, None
        self.requests_made = 0
        self.offline, self.cache_path, self.dirty = offline, cache_path, False
        self.cache = {}
        if cache_path and Path(cache_path).exists():
            self.cache = json.loads(Path(cache_path).read_text(encoding='utf-8'))

    def save_cache(self):
        if self.cache_path and self.dirty:
            path = Path(self.cache_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix='.igdb-cache-', dir=path.parent)
            try:
                with os.fdopen(handle, 'w', encoding='utf-8') as file:
                    json.dump(self.cache, file, ensure_ascii=False)
                Path(temporary).replace(path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self.dirty = False

    def _authenticate(self):
        response = self.session.post(TOKEN_URL, data={'client_id': self.client_id, 'client_secret': self.secret, 'grant_type': 'client_credentials'}, timeout=TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(f'Twitch authentication failed (HTTP {response.status_code})')
        self.token = response.json().get('access_token')
        if not self.token:
            raise RuntimeError('Twitch did not return an access token')

    def _pace(self):
        if self.last is not None:
            wait = MIN_INTERVAL - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.last = self.clock()

    def _post(self, body):
        if body in self.cache:
            return self.cache[body]
        if self.offline:
            return []
        reauthenticated = False
        for attempt in range(4):
            if self.token is None:
                self._authenticate()
            self._pace()
            self.requests_made += 1
            response = self.session.post(API_URL, data=body.encode('utf-8'), headers={'Client-ID': self.client_id, 'Authorization': f'Bearer {self.token}'}, timeout=TIMEOUT)
            if response.status_code == 200:
                payload = response.json()
                if self.cache_path is not None:
                    self.cache[body], self.dirty = payload, True
                return payload
            if response.status_code == 401 and not reauthenticated:
                self.token, reauthenticated = None, True
            elif response.status_code == 429 and attempt < 3:
                self.sleep(float(response.headers.get('Retry-After', 5)))
            else:
                raise RuntimeError(f'IGDB request failed (HTTP {response.status_code})')
        raise RuntimeError('IGDB request kept failing')

    @staticmethod
    def _parsed(payload):
        return [r for r in (parse_result(x) for x in payload) if r]

    def search(self, term):
        return self._parsed(self._post(f'fields {FIELDS}; search "{_quote(term)}"; limit 20;'))

    def exact(self, name):
        """Entries whose name equals the given text, ignoring case. Catches names the ranked search buries."""
        return self._parsed(self._post(f'fields {FIELDS}; where name ~ "{_quote(name)}"; limit 20;'))

    def by_slugs(self, slugs):
        found = {}
        for start in range(0, len(slugs), 10):
            chunk = slugs[start:start + 10]
            for result in self._parsed(self._post(f'fields {FIELDS}; where slug = ({",".join(chr(34) + _quote(s) + chr(34) for s in chunk)}); limit 50;')):
                found[result['slug']] = result
        return found


# ---- File state ----

def load_state(path):
    if path.exists():
        exporter.load_igdb(path)  # raises on a malformed file, so nothing is overwritten
        state = json.loads(path.read_text(encoding='utf-8'))
    else:
        state = {'schema': SCHEMA, 'note': DEFAULT_NOTE, 'entries': {}}
    if not isinstance(state.get('pending'), dict):
        state['pending'] = {}
    return state


def save_state(path, state):
    document = {k: v for k, v in state.items() if k not in ('entries', 'pending')}
    document['entries'] = dict(sorted(state['entries'].items()))
    if state['pending']:
        document['pending'] = dict(sorted(state['pending'].items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.igdb-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as file:
            file.write(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        exporter.load_igdb(Path(temporary))  # the exporter must be able to read exactly what is being saved
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def entry_from(candidate, status, today, score=None, via=None):
    entry = {'status': status, 'igdb_id': candidate['igdb_id'], 'slug': candidate['slug'], 'name': candidate['name'], 'platforms': candidate['platforms'], 'matched_at': today}
    if score is not None:
        entry['score'] = score
    if via:
        entry['via'] = via
    return entry


def pending_item(record, ranked):
    return {'title': record['title'], 'service': record['service'], 'platforms': record['platforms'],
            'candidates': [{k: c.get(k) for k in ('igdb_id', 'slug', 'name', 'game_type', 'year', 'platforms', 'score', 'ratings')} for c in ranked[:5]]}


def sibling_slugs(records, entries):
    """Slugs already chosen for each normalised title, so a game on two services is linked to one IGDB entry."""
    siblings = {}
    for record in records:
        entry = entries.get(record['id'])
        if entry and entry['status'] in ('confirmed', 'auto'):
            siblings.setdefault(normalise(clean_title(record['title'])), set()).add(entry['slug'])
    return siblings


# ---- Commands ----

def search_terms(title):
    cleaned = clean_title(title)
    simple = re.sub(r'\s+', ' ', re.sub(r'[^A-Za-z0-9 ]', ' ', unicodedata.normalize('NFKD', cleaned))).strip()
    terms = [cleaned] + ([simple] if simple and simple != cleaned else []) + [text for text, _ in title_variants(title)]
    return list(dict.fromkeys(t for t in terms if t))[:5]


def find_candidates(record, client):
    """Search IGDB, widening only while no candidate identifies the same title."""
    results, seen = [], set()

    def add(found):
        for result in found:
            if result['slug'] not in seen:
                seen.add(result['slug'])
                results.append(result)

    def has_strict():
        return any(c['name_kind'] in STRICT for c in rank_candidates(record, results))

    terms = search_terms(record['title'])
    for term in terms:
        add(client.search(term))
        if has_strict():
            break
    if not has_strict():
        for term in terms[:3]:
            add(client.exact(term))
            if has_strict():
                break
    return rank_candidates(record, results)


def run_match(records, state, client, save, today=None, limit=None, only=None, rematch_auto=False, retry_pending=False, out=print):
    today = today or date.today().isoformat()
    entries, pending = state['entries'], state['pending']
    done = auto = queued = 0
    for record in records:
        rid = record['id']
        if only is not None and rid not in only:
            continue
        existing = entries.get(rid)
        if existing and (existing['status'] in ('confirmed', 'none') or (existing['status'] == 'auto' and not rematch_auto)):
            continue
        if rid in pending and not retry_pending and not existing:
            continue
        if limit is not None and done >= limit:
            break
        ranked = find_candidates(record, client)
        choice, via = decide(record, ranked, sibling_slugs(records, {k: v for k, v in entries.items() if k != rid}))
        pending.pop(rid, None)
        if choice:
            entries[rid] = entry_from(choice, 'auto', today, choice['score'], via)
            auto += 1
            out(f'auto     {record["title"]} ({record["service"]}) -> {choice["name"]} [{choice["slug"]}] via {via}')
        else:
            entries.pop(rid, None)
            pending[rid] = pending_item(record, ranked)
            queued += 1
            out(f'review   {record["title"]} ({record["service"]}): {len(ranked)} candidate(s)')
        done += 1
        if done % SAVE_EVERY == 0:
            save()
            client.save_cache()
    save()
    client.save_cache()
    return {'processed': done, 'auto': auto, 'review': queued}


def replay(records, state, client, out=print):
    """Re-run the matching rules against decisions you made by hand, without changing any file.

    A reviewed decision counts as a hit when the rules would now reach the same slug automatically, a conflict when
    they would reach a different one, and a miss when they would still send it to review.
    """
    entries = state['entries']
    results = {'agree': [], 'conflict': [], 'review': [], 'new-auto': [], 'still-review': []}
    for record in records:
        rid = record['id']
        existing = entries.get(rid)
        reviewed = existing and (existing['status'] == 'none' or (existing['status'] == 'confirmed' and 'via' not in existing))
        if not reviewed and rid not in state['pending']:
            continue
        ranked = find_candidates(record, client)
        choice, via = decide(record, ranked, sibling_slugs(records, {k: v for k, v in entries.items() if k != rid}))
        if reviewed:
            if not choice:
                results['review'].append((record, existing, None, None))
            elif existing['status'] == 'confirmed' and existing['slug'] == choice['slug']:
                results['agree'].append((record, existing, choice, via))
            else:
                results['conflict'].append((record, existing, choice, via))
        else:
            results['new-auto' if choice else 'still-review'].append((record, None, choice, via))
    client.save_cache()
    out(f'Reviewed decisions: {len(results["agree"])} now automatic and identical, {len(results["conflict"])} conflicting, {len(results["review"])} still need review')
    for record, existing, choice, via in results['conflict']:
        out(f'  CONFLICT {record["title"]} ({record["service"]}): you chose {existing.get("slug", "none")}, rules chose {choice["slug"]} via {via}')
    for record, existing, choice, via in results['agree']:
        out(f'  agree    {record["title"]} ({record["service"]}) -> {choice["slug"]} via {via}')
    for record, existing, _, _ in results['review']:
        out(f'  review   {record["title"]} ({record["service"]}): you chose {existing.get("slug", "none")}')
    out(f'Pending records: {len(results["new-auto"])} would now be automatic, {len(results["still-review"])} still need review')
    for record, _, choice, via in results['new-auto']:
        out(f'  would auto {record["title"]} ({record["service"]}) -> {choice["name"]} [{choice["slug"]}] via {via}')
    return results


def verify(state, client, fix=False, out=print):
    """Check that every linked slug still exists on IGDB; with fix, fill in missing IDs, names and platforms."""
    linked = {k: e for k, e in state['entries'].items() if e['status'] in ('confirmed', 'auto')}
    found = client.by_slugs(sorted({e['slug'] for e in linked.values()}))
    missing = filled = renamed = 0
    for rid, entry in sorted(linked.items()):
        result = found.get(entry['slug'])
        if not result:
            missing += 1
            out(f'MISSING  {rid} {entry["slug"]} is not found on IGDB')
            continue
        incomplete = 'igdb_id' not in entry or 'platforms' not in entry
        if result['name'] != entry['name']:
            renamed += 1
            out(f'name     {rid} stored "{entry["name"]}", IGDB says "{result["name"]}"')
        if fix and incomplete:
            entry.update({'igdb_id': result['igdb_id'], 'name': result['name'], 'platforms': result['platforms']})
            filled += 1
    client.save_cache()
    out(f'{len(linked)} linked entries: {missing} missing, {renamed} with a different name, {filled} filled in')
    return {'missing': missing, 'renamed': renamed, 'filled': filled}


def review(state, save, ask=input, out=print, today=None):
    today = today or date.today().isoformat()
    entries, pending = state['entries'], state['pending']
    resolved = 0
    for rid in sorted(list(pending)):
        item = pending[rid]
        out(f'\n{item["title"]}  [{item["service"]}: {", ".join(item["platforms"]) or "no platform"}]  id {rid}')
        for number, c in enumerate(item['candidates'], 1):
            out(f'  {number}. {c["name"]} ({c["year"] or "?"}) {", ".join(c["platforms"]) or "no platforms"}  https://www.igdb.com/games/{c["slug"]}')
        if not item['candidates']:
            out(f'  No candidates. Search by hand: https://www.igdb.com/search?type=1&q={quote_plus(clean_title(item["title"]))}')
        while True:
            answer = ask('Number to accept, u = paste IGDB URL, n = no IGDB entry, s = skip, q = quit: ').strip().lower()
            if answer.isdigit() and 1 <= int(answer) <= len(item['candidates']):
                entries[rid] = entry_from(item['candidates'][int(answer) - 1], 'confirmed', today)
            elif answer == 'u':
                found = IGDB_URL.fullmatch(ask('IGDB URL: ').strip())
                if not found:
                    out('That is not an igdb.com/games/<slug> URL.')
                    continue
                name = ask(f'Name [{item["title"]}]: ').strip() or item['title']
                entries[rid] = {'status': 'confirmed', 'slug': found.group(1), 'name': name, 'matched_at': today}
            elif answer == 'n':
                entries[rid] = {'status': 'none', 'matched_at': today, **({'reason': r} if (r := ask('Reason (optional): ').strip()) else {})}
            elif answer == 's':
                break
            elif answer == 'q':
                save()
                return resolved
            else:
                out('Not recognised.')
                continue
            del pending[rid]
            resolved += 1
            save()
            break
    save()
    return resolved


def confirm_auto(state, save, today=None):
    today = today or date.today().isoformat()
    count = 0
    for entry in state['entries'].values():
        if entry['status'] == 'auto':
            entry['status'] = 'confirmed'
            entry['confirmed_at'] = today
            entry.pop('score', None)
            count += 1
    save()
    return count


def report(state, records, show=None, out=print):
    entries, pending = state['entries'], state['pending']
    known = {r['id']: r for r in records}
    counts = {s: sum(e['status'] == s for e in entries.values()) for s in ('confirmed', 'auto', 'none')}
    unmatched = [r for r in records if r['id'] not in entries and r['id'] not in pending]
    out(f'{len(records)} records: {counts["confirmed"]} confirmed, {counts["auto"]} auto, {counts["none"]} none, {len(pending)} awaiting review, {len(unmatched)} not yet processed')
    stale = [i for i in entries if i not in known]
    if stale:
        out(f'{len(stale)} entries refer to records no longer in the dataset')
    if show:
        for rid, e in sorted(entries.items()):
            if e['status'] == show and rid in known:
                out(f'{show:9}{known[rid]["title"]} ({known[rid]["service"]}) -> {e.get("name", "-")} [{e.get("slug", "-")}] {("via " + e["via"]) if e.get("via") else ""}'.rstrip())
    return counts


def load_records(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    return [{'id': g['id'], 'title': g['title'], 'service': g['service'], 'platforms': g['platforms']} for g in data['games']]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'output' / 'data.json')
    parser.add_argument('--igdb-file', type=Path, default=ROOT / 'data' / 'igdb.json')
    commands = parser.add_subparsers(dest='command', required=True)

    def network(command, required=True):
        command.add_argument('--credentials', help='JSON file outside this repository containing "id" and "secret" (or set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET)')
        command.add_argument('--cache', type=Path, help='File that stores raw IGDB responses so later runs can be replayed offline')

    match = commands.add_parser('match', help='Search IGDB for records without a reviewed entry')
    network(match)
    match.add_argument('--only', help='Comma-separated source IDs to process')
    match.add_argument('--limit', type=int, help='Process at most this many records')
    match.add_argument('--rematch-auto', action='store_true', help='Search again for records with an unreviewed auto entry')
    match.add_argument('--retry-pending', action='store_true', help='Search again for records already awaiting review')
    rep = commands.add_parser('replay', help='Test the matching rules against your reviewed decisions without changing any file')
    network(rep)
    ver = commands.add_parser('verify', help='Check that linked slugs still exist on IGDB')
    network(ver)
    ver.add_argument('--fix', action='store_true', help='Fill in missing IGDB IDs, names and platforms')
    commands.add_parser('review', help='Step through records awaiting review')
    commands.add_parser('confirm-auto', help='Mark every current auto entry as confirmed after you have checked them')
    summary = commands.add_parser('report', help='Summarise progress')
    summary.add_argument('--list', choices=('auto', 'confirmed', 'none'), help='Also list entries with this status')
    args = parser.parse_args(argv)
    try:
        state = load_state(args.igdb_file)
        save = lambda: save_state(args.igdb_file, state)
        records = load_records(args.data)
        if args.command in ('match', 'replay', 'verify'):
            has_credentials = bool(args.credentials or os.environ.get('IGDB_CLIENT_ID'))
            if args.command == 'replay' and args.cache and not has_credentials:
                client = IgdbClient(cache_path=args.cache, offline=True)
            else:
                client = IgdbClient(*load_credentials(args.credentials), cache_path=args.cache)
        if args.command == 'match':
            only = {i.strip() for i in args.only.split(',') if i.strip()} if args.only else None
            if only is not None and any(not exporter.ACCEPT_ID.fullmatch(i) for i in only):
                raise ValueError('--only takes source IDs such as 5315273:699413')
            result = run_match(records, state, client, save, limit=args.limit, only=only, rematch_auto=args.rematch_auto, retry_pending=args.retry_pending)
            print(f'Processed {result["processed"]}: {result["auto"]} auto, {result["review"]} to review ({client.requests_made} requests)')
        elif args.command == 'replay':
            replay(records, state, client)
        elif args.command == 'verify':
            result = verify(state, client, fix=args.fix)
            if args.fix and result['filled']:
                save()
        elif args.command == 'review':
            print(f'Resolved {review(state, save)}; {len(state["pending"])} still awaiting review')
        elif args.command == 'confirm-auto':
            print(f'Confirmed {confirm_auto(state, save)} auto entries')
        else:
            report(state, records, args.list)
        return 0
    except (ValueError, OSError, RuntimeError, CredentialsError, KeyError, requests.RequestException) as exc:
        print(f'igdb_match failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
