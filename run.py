"""
run.py - Continuous scanner loop for Railway / VPS deployment.

Runs scanner.py in a loop instead of relying on GitHub Actions cron.
Default: scan every 2 minutes (configurable via SCAN_INTERVAL_SECONDS env var).
Automatically commits and pushes playbook.json to GitHub after each scan.
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

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))
GIT_TOKEN = os.environ.get("GIT_TOKEN", "")


def setup_git():
    if not GIT_TOKEN:
        log.warning("GIT_TOKEN not set - playbook.json will NOT be saved to GitHub")
        return False
    try:
        subprocess.run(["git", "config", "user.email", "bot@memecoinsnipa.com"], check=True)
        subprocess.run(["git", "config", "user.name", "Memecoinsnipa Bot"], check=True)
        subprocess.run(
            ["git", "remote", "set-url", "origin",
             f"https://x-access-token:{GIT_TOKEN}@github.com/britzmaximus-ship-it/Memecoinsnipa.git"],
            check=True,
        )
        log.info("Git configured for playbook persistence")
        return True
    except Exception as e:
        log.warning(f"Git setup failed: {e}")
        return False


def save_playbook():
    if not GIT_TOKEN:
        return
    try:
        result = subprocess.run(["git", "diff", "--name-only", "playbook.json"], capture_output=True, text=True)
        if "playbook.json" not in result.stdout:
            return
        subprocess.run(["git", "add", "playbook.json"], check=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run(["git", "commit", "-m", f"Update playbook (Railway scan {timestamp})"], check=True)
        subprocess.run(["git", "push"], check=True, timeout=30)
        log.info("Playbook saved to GitHub")
    except Exception as e:
        log.warning(f"Playbook save failed: {e}")


def run_scan():
    result = subprocess.run([sys.executable, "scanner.py"], timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Scanner exited with code {result.returncode}")


if __name__ == "__main__":
    log.info("=== Memecoinsnipa Runner Started ===")
    log.info(f"Scan interval: {SCAN_INTERVAL}s ({SCAN_INTERVAL / 60:.1f} min)")
    log.info(f"Time: {datetime.now(timezone.utc).isoformat()}")

    git_enabled = setup_git()
    log.info(f"Playbook auto-save: {'ENABLED' if git_enabled else 'DISABLED'}")

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10

    while True:
        try:
            log.info(f"--- Starting scan at {datetime.now(timezone.utc).isoformat()} ---")
            run_scan()
            save_playbook()
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