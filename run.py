"""
run.py - Continuous scanner loop for Railway / VPS deployment.

Runs scanner.py in a loop instead of relying on GitHub Actions cron.
Default: scan every 2 minutes (configurable via SCAN_INTERVAL_SECONDS env var).
"""

import os
import sys
import time
import logging
import subprocess
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("runner")

# How often to scan (seconds). Default 120 = 2 minutes.
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))


def run_scan():
    """Run scanner.py as a standalone script so __name__ == '__main__' works."""
    result = subprocess.run(
        [sys.executable, "scanner.py"],
        timeout=300,  # 5 minute timeout per scan
    )
    if result.returncode != 0:
        raise RuntimeError(f"Scanner exited with code {result.returncode}")


if __name__ == "__main__":
    log.info("=== Memecoinsnipa Runner Started ===")
    log.info(f"Scan interval: {SCAN_INTERVAL}s ({SCAN_INTERVAL / 60:.1f} min)")
    log.info(f"Time: {datetime.now(timezone.utc).isoformat()}")

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10

    while True:
        try:
            log.info(f"--- Starting scan at {datetime.now(timezone.utc).isoformat()} ---")
            run_scan()
            consecutive_errors = 0
            log.info(f"--- Scan complete. Next scan in {SCAN_INTERVAL}s ---")

        except EnvironmentError as e:
            log.error(f"FATAL config error: {e}")
            sys.exit(1)

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Scan failed ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("Too many consecutive errors. Exiting.")
                sys.exit(1)

            backoff = min(SCAN_INTERVAL * consecutive_errors, 600)
            log.info(f"Backing off {backoff}s before retry...")
            time.sleep(backoff)
            continue

        time.sleep(SCAN_INTERVAL)