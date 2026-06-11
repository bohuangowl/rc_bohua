"""Configuration loading module.

Load service settings and vendor configurations from the config/ directory.
"""

import logging
import json
import os
from typing import Any, Dict

# Default service config; values in config/app.json with matching keys override these defaults
DEFAULT_APP_CONFIG: Dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8000,
    "max_retries": 3,
    "retry_base_interval_sec": 10,
    "worker_poll_interval_sec": 5,
}


def load_app_config(config_dir: str) -> Dict[str, Any]:
    """Load service config with defaults for any missing fields."""
    path = os.path.join(config_dir, "app.json")
    config = dict(DEFAULT_APP_CONFIG)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            file_config = json.load(f)
            config.update(file_config)
    return config


def load_vendors(config_dir: str) -> Dict[str, Any]:
    """Load vendor configurations, returns a dict of vendor_name -> vendor_config."""
    path = os.path.join(config_dir, "vendors.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        vendors = json.load(f)
    for name, cfg in vendors.items():
        if not isinstance(cfg, dict) or "url" not in cfg:
            logger.warning("Vendor '%s' is missing 'url' field, delivery will fail", name)
    return vendors
logger = logging.getLogger(__name__)
