"""APINotifyService entry point.

Assembles all modules: load config, create queue, create router,
start Worker, start HTTP server. Run with `python -m app.main`.
"""

import logging
import os
import sys

from .config import load_app_config, load_vendors
from .deliver import DeliveryWorker, NotificationRouter
from .server import create_server
from .task import TaskQueue

# Logging to console with timestamp, level, and module name
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _find_config_dir() -> str:
    """Locate the config/ directory.

    Search order:
    1. config/ under the current working directory
    2. config/ next to the package root
    """
    candidates = [
        os.path.join(os.getcwd(), "config"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    # Fall back to cwd/config/ (default config will be used)
    return candidates[0]


def main() -> None:
    """Main: wire up modules and start the service."""
    # Locate config directory
    config_dir = _find_config_dir()
    logger.info("Using config directory: %s", config_dir)

    # Load configuration
    app_config = load_app_config(config_dir)
    vendors = load_vendors(config_dir)
    logger.info("Loaded %d vendor configurations", len(vendors))

    # Wire up modules
    queue = TaskQueue()
    router = NotificationRouter(vendors)
    worker = DeliveryWorker(
        queue=queue,
        router=router,
        max_retries=app_config["max_retries"],
        retry_base_interval_sec=app_config["retry_base_interval_sec"],
        poll_interval_sec=app_config["worker_poll_interval_sec"],
    )

    # Start background Worker
    worker.start()

    # Start HTTP server
    host = app_config["host"]
    port = app_config["port"]
    server = create_server(queue, host, port)
    logger.info("Server listening on http://%s:%d", host, port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        worker.stop()
        server.shutdown()
        logger.info("Bye.")


if __name__ == "__main__":
    main()
