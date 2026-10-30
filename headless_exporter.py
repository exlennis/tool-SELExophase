#!/usr/bin/env python3
"""Export public Exophase activity to JSON without starting the terminal UI."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 50
ART_HOST = 'm.exophase.com'
ART_WEB_PREFIX = 'output/art/'
ART_MAX_BYTES = 256 * 1024
ART_TIMEOUT = 15
ART_MAX_CONSECUTIVE_FAILURES = 8
ART_NAME = re.compile(r'[a-z0-9_-]{1,32}-[A-Za-z0-9_-]{1,64}-[A-Za-z0-9]{1,64}\.(?:png|jpg|webp)')
ART_MAGIC = {'png': (b'\x89PNG\r\n\x1a\n',), 'jpg': (b'\xff\xd8\xff',), 'webp': (b'RIFF',)}
ACCEPT_ID = re.compile(r'[0-9]+:[0-9]+')
IGDB_SCHEMA = 'selfexophase-igdb/v1'
IGDB_STATUSES = ('confirmed', 'auto', 'none')
IGDB_SLUG = re.compile(r'[a-z0-9][a-z0-9_-]{0,127}')
HEADERS = {'Accept': 'application/json, text/plain, */*', 'Referer': 'https://www.exophase.com/',
           'Accept-Language': 'en-US,en;q=0.9',
           'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) or int(value) <= 0:
        raise ValueError('Player and game identifiers must be positive integers')
    return str(int(value))


def identity(game):
    if not isinstance(game, dict):
        raise ValueError('Invalid game object')
    return positive_id(game.get('master_playerid')), positive_id(game.get('master_id'))


def validate_games(games):
    if not isinstance(games, list) or not games:
        raise ValueError('Empty or invalid game list; previous files retained for review')
    seen = set()
    for game in games:
        key = identity(game)
        if key in seen:
            raise ValueError('Duplicate game identifiers; incomplete snapshot rejected')
        seen.add(key)
        meta = game.get('meta')
        if not isinstance(meta, dict) or not isinstance(meta.get('title'), str) or not meta['title'].strip():
            raise ValueError('Missing game title or metadata')
    return games


def fetch_telemetry_safely(player_id):
    games = []
    seen = set()
    for page in range(1, 1001):
        response = requests.get(f'https://api.exophase.com/public/player/{player_id}/games',
                                params={'page': page, 'environment': '', 'sort': 1, 'showHidden': 0},
                                headers=HEADERS, timeout=30)
        if response.status_code != 200:
            raise ValueError(f'Exophase returned HTTP {response.status_code}; previous files retained')
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise ValueError('Exophase did not confirm a successful page; previous files retained')
        batch = payload.get('games')
        if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
            raise ValueError('Exophase pagination structure changed')
        for game in batch:
            key = identity(game)
            if key in seen:
                raise ValueError('Repeated Exophase page or duplicate game')
            seen.add(key)
        games.extend(batch)
        if len(batch) < PAGE_SIZE:
            return validate_games(games)
    raise ValueError('Exophase pagination did not terminate')


def number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'Invalid {field}')
    return value


def playtime_minutes(game):
    text = game.get('playtime')
    units = game.get('playtimeUnits')
    if units is not None:
        if not isinstance(units, dict):
            raise ValueError('Invalid playtime units')
        hours = number(units.get('hours') or 0, 'playtime hours')
        minutes = number(units.get('minutes') or 0, 'playtime minutes')
        if hours or minutes:
            return hours * 60 + minutes
    if text is None or text == '':
        return None
    if not isinstance(text, str):
        raise ValueError('Playtime requires labelled hours or minutes')
    match = re.fullmatch(r'\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*', text, re.I)
    if not match or not any(match.groups()):
        raise ValueError('Unrecognised playtime format')
    return int(match[1] or 0) * 60 + int(match[2] or 0)


def count(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'Invalid {field}')
    return value


def last_played(game, now=None):
    """Return the source's last-played time as UTC ISO text, or None when absent or implausible."""
    value = game.get('lastplayed_utc', game.get('lastplayed'))
    if value is None or value == 0:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid last played time')
    now = now or datetime.now(timezone.utc)
    try:
        moment = datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError('Invalid last played time')
    if moment > now + timedelta(days=1):
        return None
    return moment.isoformat(timespec='seconds')


def artwork_name(url):
    """Derive a stable local file name from an Exophase tile URL, or None if it is not a recognised image."""
    try:
        parts = urlsplit(url)
    except (ValueError, AttributeError):
        return None
    if parts.scheme != 'https' or parts.hostname != ART_HOST or parts.port is not None:
        return None
    segments = parts.path.strip('/').split('/')
    if len(segments) != 4 or segments[1] != 'games' or segments[2] != 't':
        return None
    stem, dot, ext = segments[3].rpartition('.')
    ext = {'png': 'png', 'jpg': 'jpg', 'jpeg': 'jpg', 'webp': 'webp'}.get(ext.lower()) if dot else None
    version = re.sub(r'[^A-Za-z0-9]', '', parts.query)[:24] or 'v0'
    name = f'{segments[0]}-{stem}-{version}.{ext}'
    return name if ext and ART_NAME.fullmatch(name) else None


def download_artwork(url, target):
    response = requests.get(url, headers=HEADERS, timeout=ART_TIMEOUT)
    if response.status_code != 200:
        raise ValueError(f'HTTP {response.status_code}')
    body = response.content
    kind = target.suffix[1:]
    if not isinstance(body, bytes) or not 0 < len(body) <= ART_MAX_BYTES:
        raise ValueError('Image is empty or too large')
    if not body.startswith(ART_MAGIC[kind]) or (kind == 'webp' and body[8:12] != b'WEBP'):
        raise ValueError('Response is not the expected image type')
    temporary = target.with_name('.' + target.name + '.part')
    try:
        temporary.write_bytes(body)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def sync_artwork(games, art_dir, fetch):
    """Map each source record to a locally cached thumbnail, downloading only images not yet cached.

    A failed or unavailable image never raises: the record keeps an older cached version or no artwork.
    """
    found, skipped, failures, consecutive = {}, 0, [], 0
    for game in games:
        url = (game.get('resource_tile') or game.get('resource_small'))
        name = artwork_name(url) if isinstance(url, str) else None
        row_id = '%s:%s' % identity(game)
        if name is None:
            skipped += 1
            continue
        target = art_dir / name
        if target.is_file() and target.stat().st_size > 0:
            found[row_id] = name
            continue
        if fetch and consecutive < ART_MAX_CONSECUTIVE_FAILURES:
            try:
                art_dir.mkdir(parents=True, exist_ok=True)
                download_artwork(url, target)
                found[row_id], consecutive = name, 0
                continue
            except (ValueError, OSError, requests.RequestException) as exc:
                consecutive += 1
                failures.append(f'{row_id}: {exc}')
        stem = name.rpartition('-')[0] + '-'
        older = sorted(art_dir.glob(glob_escape(stem) + '*')) if art_dir.is_dir() else []
        older = [p for p in older if ART_NAME.fullmatch(p.name) and p.stat().st_size > 0]
        if older:
            found[row_id] = older[-1].name
    return found, {'skipped': skipped, 'failures': failures, 'stopped': consecutive >= ART_MAX_CONSECUTIVE_FAILURES}


def glob_escape(text):
    return re.sub(r'([\[\]*?])', r'[\1]', text)


def prune_artwork(art_dir, report):
    """Remove cached thumbnails no longer referenced by the published report."""
    if not art_dir.is_dir():
        return 0
    keep = {Path(g['artwork']).name for g in report['games'] if g.get('artwork')}
    removed = 0
    for path in art_dir.iterdir():
        if path.is_file() and not path.is_symlink() and path.name not in keep and (ART_NAME.fullmatch(path.name) or path.name.endswith('.part')):
            path.unlink()
            removed += 1
    return removed


def parse_accepted_removals(text):
    ids = set()
    for token in re.split(r'[\s,;]+', (text or '').strip()):
        if token:
            if not ACCEPT_ID.fullmatch(token):
                raise ValueError(f'Invalid removal identifier {token!r}; use player:game, for example 5315278:714604')
            ids.add(token)
    return ids


def check_removals(old_games, games, accepted):
    """Hold publication when cached records vanish unless exactly those records were reviewed and accepted."""
    now = {'%s:%s' % identity(g) for g in games}
    titles = {'%s:%s' % identity(g): g['meta']['title'] for g in old_games}
    missing = set(titles) - now
    unexpected = accepted - missing
    if unexpected:
        raise ValueError('Accepted removals are not missing from the source: ' + ', '.join(sorted(unexpected)) + '; publication held')
    unreviewed = sorted(missing - accepted)
    if unreviewed:
        listing = '; '.join(f'{i} ({titles[i]})' for i in unreviewed)
        raise ValueError(f'Previously cached games are missing; publication held for review: {listing}. After review, accept exactly these with --accept-removals "{",".join(unreviewed)}"')
    return sorted(missing)


def load_igdb(path):
    """Read the hand-managed IGDB mapping and return {source id: IGDB URL} for linked records.

    A missing file simply means no IGDB links. A present but invalid file raises, so a bad edit holds
    publication and keeps the last good output instead of silently dropping links.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'IGDB mapping file could not be read: {exc}')
    if not isinstance(data, dict) or data.get('schema') != IGDB_SCHEMA or not isinstance(data.get('entries'), dict):
        raise ValueError('IGDB mapping file has an unsupported format')
    links = {}
    for key, entry in data['entries'].items():
        if not ACCEPT_ID.fullmatch(key) or not isinstance(entry, dict) or entry.get('status') not in IGDB_STATUSES:
            raise ValueError(f'IGDB mapping entry {key!r} is invalid')
        if entry['status'] == 'none':
            continue
        slug, name = entry.get('slug'), entry.get('name')
        if not isinstance(slug, str) or not IGDB_SLUG.fullmatch(slug) or not isinstance(name, str) or not name.strip():
            raise ValueError(f'IGDB mapping entry {key!r} needs a valid slug and name')
        if entry.get('igdb_id') is not None and (isinstance(entry['igdb_id'], bool) or not isinstance(entry['igdb_id'], int) or entry['igdb_id'] <= 0):
            raise ValueError(f'IGDB mapping entry {key!r} has an invalid igdb_id')
        if entry.get('platforms') is not None and (not isinstance(entry['platforms'], list) or any(not isinstance(p, str) for p in entry['platforms'])):
            raise ValueError(f'IGDB mapping entry {key!r} has invalid platforms')
        links[key] = 'https://www.igdb.com/games/' + slug
    return links


def compile_datastore(games, player_id, source_mode, source_retrieved_at=None, artwork=None, igdb=None):
    validate_games(games)
    artwork = artwork or {}
    igdb = igdb or {}
    rows = []
    for game in games:
        pid, gid = identity(game)
        meta = game['meta']
        platforms = meta.get('platforms') or []
        if not isinstance(platforms, list) or any(not isinstance(p, dict) or not isinstance(p.get('name'), str) for p in platforms):
            raise ValueError('Invalid release platforms')
        if meta.get('environment_slug') is not None and not isinstance(meta['environment_slug'], str):
            raise ValueError('Invalid source service')
        minutes = playtime_minutes(game)
        earned = game.get('earned_awards')
        if earned is None:
            medals = [game.get('earned_' + name) for name in ('bronze', 'silver', 'gold', 'platinum')]
            earned = sum(number(v, 'earned awards') for v in medals if v is not None) if any(v is not None for v in medals) else None
        if earned is not None:
            earned = number(earned, 'earned awards')
        percent = game.get('percent')
        if percent is not None:
            percent = number(percent, 'achievement completion percentage')
            if percent > 100:
                raise ValueError('Achievement completion percentage exceeds 100')
        total = game.get('total_awards', meta.get('total_awards'))
        if total is not None:
            total = count(total, 'achievement total')
            if earned is not None and earned > total:
                raise ValueError('Earned achievements exceed the achievement total')
        rows.append({'id': pid + ':' + gid, 'source_player_id': pid, 'source_game_id': gid,
                     'title': meta['title'], 'service': meta.get('environment_slug') or 'unknown',
                     'platforms': list(dict.fromkeys(p['name'] for p in platforms)),
                     'playtime_minutes': minutes, 'hours': round(minutes / 60, 3) if minutes is not None else None,
                     'earned_awards': earned, 'total_awards': total, 'achievement_completion_percent': percent,
                     'last_played': last_played(game),
                     'activity_status': 'played' if (minutes or 0) > 0 or (earned or 0) > 0 else 'unknown',
                     'source_url': meta.get('endpoint_awards'),
                     'igdb_url': igdb.get(pid + ':' + gid),
                     'artwork': ART_WEB_PREFIX + artwork[pid + ':' + gid] if (pid + ':' + gid) in artwork else None})
    rows.sort(key=lambda g: (-(g['playtime_minutes'] or 0), g['title'].casefold(), g['id']))
    fingerprint = hashlib.sha256(json.dumps(games, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {'schema': 'selfexophase-telemetry/v1', 'metadata': {
        'player_id': player_id, 'last_updated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source_retrieved_at': source_retrieved_at, 'source_mode': source_mode,
        'snapshot_sha256': fingerprint, 'total_games': len(rows),
        'total_known_hours': round(sum(g['playtime_minutes'] or 0 for g in rows) / 60, 3),
        'unknown_playtime_count': sum(g['playtime_minutes'] is None for g in rows),
        'played_evidence_count': sum(g['activity_status'] == 'played' for g in rows)}, 'games': rows}


def publish(raw, report, cache_dir, output_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir.resolve() == output_dir.resolve() or cache_dir.resolve() in output_dir.resolve().parents or output_dir.resolve() in cache_dir.resolve().parents:
        raise ValueError('Cache and output must be separate directories')
    if cache_dir.stat().st_dev != output_dir.stat().st_dev:
        raise ValueError('Cache and output must share a filesystem for atomic publication')
    cache_file, output_file = cache_dir / 'telemetry_cache.json', output_dir / 'data.json'
    with tempfile.TemporaryDirectory(prefix='.telemetry-', dir=cache_dir) as folder:
        folder = Path(folder)
        staged_cache, staged_report, backup = folder / 'cache.json', folder / 'report.json', folder / 'before.json'
        for file, data in ((staged_cache, raw), (staged_report, report)):
            file.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        had_cache = cache_file.exists()
        if had_cache:
            backup.write_bytes(cache_file.read_bytes())
        staged_cache.replace(cache_file)
        try:
            staged_report.replace(output_file)
        except BaseException:
            if had_cache:
                backup.replace(cache_file)
            else:
                cache_file.unlink(missing_ok=True)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--player-id', default=os.environ.get('EXOPHASE_PLAYER_ID', '5143228'))
    parser.add_argument('--cache-dir', type=Path, default=Path(os.environ.get('EXOPHASE_CACHE_DIR', ROOT / 'cache')))
    parser.add_argument('--output-dir', type=Path, default=Path(os.environ.get('EXOPHASE_OUTPUT_DIR', ROOT / 'output')))
    parser.add_argument('--input', type=Path, help='Compile a saved all_games JSON array without a network request')
    parser.add_argument('--igdb-file', type=Path, default=ROOT / 'data' / 'igdb.json', help='Hand-managed mapping of source IDs to IGDB entries')
    parser.add_argument('--art-dir', type=Path, help='Local thumbnail cache served with the site (default: OUTPUT_DIR/art)')
    parser.add_argument('--download-artwork', action='store_true', help='Download missing thumbnails even with --input (live runs always do)')
    parser.add_argument('--accept-removals', default=os.environ.get('EXOPHASE_ACCEPT_REMOVALS', ''),
                        help='Comma-separated player:game IDs whose disappearance from the source has been reviewed and accepted')
    args = parser.parse_args(argv)
    try:
        player_id = positive_id(args.player_id)
        accepted = parse_accepted_removals(args.accept_removals)
        igdb_links = load_igdb(args.igdb_file)
        previous = args.cache_dir / 'telemetry_cache.json'
        old = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else None
        if old is not None and (not isinstance(old, dict) or old.get('schema') != 'selfexophase-cache/v1' or old.get('player_id') != player_id):
            raise ValueError('Existing telemetry cache belongs to another profile or has an invalid schema')
        retrieved = None
        if args.input:
            games = validate_games(json.loads(args.input.read_text(encoding='utf-8')))
        else:
            games = fetch_telemetry_safely(player_id)
            retrieved = datetime.now(timezone.utc).isoformat(timespec='seconds')
        removed = check_removals(validate_games(old.get('games')), games, accepted) if old is not None else []
        if old is None and accepted:
            raise ValueError('No previous cache exists, so there are no removals to accept')
        art_dir = args.art_dir or args.output_dir / 'art'
        artwork, art_report = sync_artwork(games, art_dir, fetch=not args.input or args.download_artwork)
        report = compile_datastore(games, player_id, 'offline' if args.input else 'live', retrieved, artwork, igdb_links)
        raw = {'schema': 'selfexophase-cache/v1', 'player_id': player_id, 'retrieved_at': retrieved, 'games': games}
        publish(raw, report, args.cache_dir, args.output_dir)
        removed_art = prune_artwork(art_dir, report)
        for failure in art_report['failures']:
            print(f'Artwork unavailable ({failure}); activity data published without it', file=sys.stderr)
        if art_report['stopped']:
            print('Artwork downloads stopped after repeated failures; remaining images use cached or no artwork', file=sys.stderr)
        if removed:
            print(f'Accepted {len(removed)} reviewed source removals: ' + ', '.join(removed))
        print(f'Exported {len(games)} games to {args.output_dir / "data.json"} ({len(artwork)} with artwork, {removed_art} stale images removed)')
        return 0
    except (ValueError, OSError, requests.RequestException) as exc:
        print(f'Export failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
