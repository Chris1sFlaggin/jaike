"""Config: atomic writes, cached reads, and hot-reload on external edits."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from jake import gateway


class ConfigStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._saved = (gateway.CONFIG_DIR, gateway.CONFIG_PATH)
        gateway.CONFIG_DIR = d
        gateway.CONFIG_PATH = d / "config.json"
        gateway._cfg_cache = None
        gateway._cfg_mtime = None

    def tearDown(self):
        gateway.CONFIG_DIR, gateway.CONFIG_PATH = self._saved
        gateway._cfg_cache = None
        gateway._cfg_mtime = None
        self._tmp.cleanup()

    def test_first_run_seeds_the_file(self):
        cfg = gateway.load_config()
        self.assertTrue(gateway.CONFIG_PATH.exists())
        self.assertIn("backend", cfg)

    def test_save_is_atomic_and_leaves_no_temp(self):
        gateway.save_config({"backend": "ollama", "user_name": "Bob"})
        on_disk = json.loads(gateway.CONFIG_PATH.read_text())
        self.assertEqual(on_disk["backend"], "ollama")
        temps = [p.name for p in gateway.CONFIG_DIR.iterdir()
                 if p.name.startswith(".config-")]
        self.assertEqual(temps, [])

    def test_returned_config_is_an_isolated_copy(self):
        gateway.save_config(dict(gateway.DEFAULT_CONFIG))
        first = gateway.load_config()
        first["backend"] = "tampered"
        self.assertNotEqual(gateway.load_config()["backend"], "tampered")

    def test_corrupt_file_falls_back_to_defaults(self):
        gateway.CONFIG_PATH.write_text("{ not valid json")
        gateway._cfg_cache = None
        gateway._cfg_mtime = None
        cfg = gateway.load_config()
        self.assertEqual(cfg["backend"], gateway.DEFAULT_CONFIG["backend"])

    def test_external_edit_is_picked_up(self):
        gateway.save_config(dict(gateway.DEFAULT_CONFIG, user_name="Finn"))
        self.assertEqual(gateway.load_config()["user_name"], "Finn")
        # someone edits the file behind our back
        path = gateway.CONFIG_PATH
        path.write_text(json.dumps(dict(gateway.DEFAULT_CONFIG,
                                        user_name="Jake")))
        st = path.stat()                       # force a distinct mtime
        os.utime(path, (st.st_atime, st.st_mtime + 5))
        self.assertEqual(gateway.load_config()["user_name"], "Jake")


if __name__ == "__main__":
    unittest.main()
