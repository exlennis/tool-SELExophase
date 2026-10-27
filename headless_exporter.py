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
from datetime import datetime, timezone

import requests

ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 50
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


def compile_datastore(games, player_id, source_mode, source_retrieved_at=None):
    validate_games(games)
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
        rows.append({'id': pid + ':' + gid, 'source_player_id': pid, 'source_game_id': gid,
                     'title': meta['title'], 'service': meta.get('environment_slug') or 'unknown',
                     'platforms': list(dict.fromkeys(p['name'] for p in platforms)),
                     'playtime_minutes': minutes, 'hours': round(minutes / 60, 3) if minutes is not None else None,
                     'earned_awards': earned, 'achievement_completion_percent': percent,
                     'activity_status': 'played' if (minutes or 0) > 0 or (earned or 0) > 0 else 'unknown',
                     'source_url': meta.get('endpoint_awards')})
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
    args = parser.parse_args(argv)
    try:
        player_id = positive_id(args.player_id)
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
        report = compile_datastore(games, player_id, 'offline' if args.input else 'live', retrieved)
        if old is not None:
            previous_ids = {identity(g) for g in validate_games(old.get('games'))}
            if previous_ids - {identity(g) for g in games}:
                raise ValueError('Previously cached games are missing; publication held for review')
        raw = {'schema': 'selfexophase-cache/v1', 'player_id': player_id, 'retrieved_at': retrieved, 'games': games}
        publish(raw, report, args.cache_dir, args.output_dir)
        print(f'Exported {len(games)} games to {args.output_dir / "data.json"}')
        return 0
    except (ValueError, OSError, requests.RequestException) as exc:
        print(f'Export failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
