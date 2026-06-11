"""Config loading edge case tests."""

import json
import os
import tempfile
import unittest

from app.config import load_app_config, load_vendors


class TestLoadAppConfig(unittest.TestCase):
    """Edge cases for load_app_config."""

    def test_missing_config_dir_returns_defaults(self):
        """Non-existent config directory returns default config."""
        config = load_app_config("/nonexistent/path")
        self.assertEqual(config["host"], "0.0.0.0")
        self.assertEqual(config["port"], 8000)
        self.assertEqual(config["max_retries"], 3)

    def test_empty_app_json_returns_defaults(self):
        """Empty app.json returns default config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "app.json"), "w") as f:
                json.dump({}, f)
            config = load_app_config(tmpdir)
            self.assertEqual(config["port"], 8000)

    def test_partial_app_json_overrides_specific_keys(self):
        """Partial app.json overrides only the specified keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "app.json"), "w") as f:
                json.dump({"port": 9000}, f)
            config = load_app_config(tmpdir)
            self.assertEqual(config["port"], 9000)
            self.assertEqual(config["host"], "0.0.0.0")  # unchanged

    def test_corrupted_app_json_raises(self):
        """Corrupted app.json should raise JSONDecodeError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "app.json"), "w") as f:
                f.write("not json")
            with self.assertRaises(json.JSONDecodeError):
                load_app_config(tmpdir)


class TestLoadVendors(unittest.TestCase):
    """Edge cases for load_vendors."""

    def test_missing_vendors_json_returns_empty(self):
        """Missing vendors.json returns empty dict."""
        config = load_vendors("/nonexistent/path")
        self.assertEqual(config, {})

    def test_empty_vendors_json_returns_empty(self):
        """Empty vendors.json (empty object) returns empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "vendors.json"), "w") as f:
                json.dump({}, f)
            vendors = load_vendors(tmpdir)
            self.assertEqual(vendors, {})

    def test_corrupted_vendors_json_raises(self):
        """Corrupted vendors.json should raise JSONDecodeError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "vendors.json"), "w") as f:
                f.write("{bad json}")
            with self.assertRaises(json.JSONDecodeError):
                load_vendors(tmpdir)


if __name__ == "__main__":
    unittest.main()
