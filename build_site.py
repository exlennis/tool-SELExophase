#!/usr/bin/env python3
"""Build a static Pages bundle containing only the dashboard and its public dataset."""

import argparse
import json
from pathlib import Path
import re
import shutil
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

ROOT = Path(__file__).resolve().parent
SITE_FILES = ('index.html', 'assets/dashboard.css', 'assets/logic.js', 'assets/dashboard.js', 'output/data.json')
ART_PATH = re.compile(r'output/art/[A-Za-z0-9_-]+\.(?:png|jpg|webp)')
ART_MAX_BYTES = 256 * 1024


def build_site(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination == root or destination in root.parents:
        raise ValueError('The site destination must not replace the project or a parent directory')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Use an empty site destination; existing files will not be removed')
    data = json.loads((root / 'output/data.json').read_text(encoding='utf-8'))
    if data.get('schema') != 'selfexophase-telemetry/v1' or not isinstance(data.get('games'), list) or data.get('metadata', {}).get('total_games') != len(data['games']):
        raise ValueError('Invalid telemetry dataset; site build held')
    for name in SITE_FILES:
        if not (root / name).is_file() or (root / name).is_symlink():
            raise ValueError('Missing or unexpected site asset: ' + name)
    # Only thumbnails the dataset references are published. A missing or unusable image is skipped (the page
    # shows a placeholder) rather than holding back otherwise valid activity data.
    artwork = sorted({g.get('artwork') for g in data['games'] if isinstance(g.get('artwork'), str) and ART_PATH.fullmatch(g['artwork'])})
    artwork = [name for name in artwork if (root / name).is_file() and not (root / name).is_symlink() and 0 < (root / name).stat().st_size <= ART_MAX_BYTES]
    destination.mkdir(parents=True, exist_ok=True)
    for name in (*SITE_FILES, *artwork):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    (destination / '.nojekyll').touch()
    return destination


def serve_site(directory, port):
    class Server(ThreadingHTTPServer):
        def server_bind(self):
            # Avoid reverse-DNS delays when binding localhost on this Mac.
            TCPServer.server_bind(self)
            self.server_name = 'localhost'
            self.server_port = self.server_address[1]
    server = Server(('127.0.0.1', port), partial(SimpleHTTPRequestHandler, directory=str(directory)))
    print(f'Dashboard preview: http://127.0.0.1:{port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--serve', action='store_true', help='Preview only the public bundle on localhost')
    parser.add_argument('--port', type=int, default=8786)
    args = parser.parse_args()
    destination = build_site(ROOT, args.destination)
    print('Static dashboard prepared at ' + str(destination))
    if args.serve:
        serve_site(destination, args.port)
