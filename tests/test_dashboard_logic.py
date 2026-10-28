import shutil
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DashboardLogicTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed to run the dashboard logic tests')
    def test_dashboard_logic_under_node(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/dashboard_logic.test.js')], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__': unittest.main()
