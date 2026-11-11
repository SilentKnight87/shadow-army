"""Exercise the pane's rendered copy without a browser or model connection."""
from pathlib import Path
import subprocess
import unittest


class PanePolishTests(unittest.TestCase):
    def test_pane_polish(self):
        result = subprocess.run(['node', str(Path(__file__).with_suffix('.mjs'))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pane_looks_up_elements_through_refs_only(self):
        source = (Path(__file__).parent / 'shadow-army/desktop/plugin.js').read_text()
        self.assertNotIn('document.querySelector', source)
        self.assertNotIn('document.getElementById', source)

