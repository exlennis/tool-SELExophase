import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_site', ROOT / 'build_site.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SiteTests(unittest.TestCase):
    def test_bundle_excludes_raw_cache_and_local_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root, out = Path(folder) / 'source', Path(folder) / 'public'
            for name in m.SITE_FILES:
                file = root / name; file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(json.dumps({'schema':'selfexophase-telemetry/v1','metadata':{'total_games':0},'games':[]}) if name.endswith('.json') else 'asset')
            (root / 'cache').mkdir()
            (root / 'cache/telemetry_cache.json').write_text('private raw payload')
            (root / '.env').write_text('test secret')
            m.build_site(root, out)
            files = {str(p.relative_to(out)) for p in out.rglob('*') if p.is_file()}
            self.assertEqual(files, set(m.SITE_FILES) | {'.nojekyll'})
    def test_builder_refuses_nonempty_or_project_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'keep.txt').write_text('retain')
            for destination in (root, root / 'existing'):
                destination.mkdir(exist_ok=True)
                (destination / 'keep.txt').write_text('retain')
                with self.assertRaises(ValueError): m.build_site(root, destination)
                self.assertEqual((destination / 'keep.txt').read_text(), 'retain')
    def test_gitignore_retains_only_intended_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git','init','--quiet',str(root)],check=True)
            (root / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
            hidden = ['.venv/bin/python','cache/awards_1_2.json','cache/20261017/all_games.json','cache/.telemetry-temp/scratch.json','output/report.csv','output/20261017/report.xlsx','.env','assets/__pycache__/x.pyc']
            tracked = ['cache/telemetry_cache.json','output/data.json','headless_exporter.py','index.html','.github/workflows/sync.yml']
            for name in hidden + tracked:
                result = subprocess.run(['git','-C',str(root),'check-ignore','--quiet',name])
                self.assertEqual(result.returncode, 0 if name in hidden else 1, name)


if __name__ == '__main__': unittest.main()
