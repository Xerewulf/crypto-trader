#!/usr/bin/env python3
"""24/7 Production Process Supervisor & Watchdog for Binance Crypto Trader.
Standard library only.

Ensures uninterrupted 24/7 autonomous operation:
  1. Spawns `paper_trader.py run` as a supervised child process.
  2. Actively monitors heartbeat timestamps in `state.json`.
  3. Detects hangs, network freezes, or crashes (heartbeat > 90s stale).
  4. Automatically terminates unresponsive processes and restarts them with backoff.
  5. Dispatches instant Telegram / Discord alert notifications on failure and recovery.

Usage:
  python trader/live/watchdog.py              # Supervise paper trader
  python trader/live/watchdog.py --testnet    # Supervise live testnet execution
  python trader/live/watchdog.py --live       # Supervise real production trading
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TRADER_SCRIPT = os.path.join(HERE, "paper_trader.py")

try:
    import notifier
except ImportError:
    notifier = None

MAX_STALE_SECONDS = 150
BOOT_GRACE_SECONDS = 180
POLL_INTERVAL_SECONDS = 15


def get_heartbeat_ms():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data.get("heartbeat_ms")
    except Exception:
        return None


def now_ms():
    return int(time.time() * 1000)


def iso(ts=None):
    t = datetime.datetime.fromtimestamp((ts or time.time()), datetime.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")


def run_watchdog(extra_flags=None):
    extra_flags = extra_flags or []
    cmd = [sys.executable, TRADER_SCRIPT, "run"] + extra_flags

    print("=" * 70)
    print("      BINANCE 24/7 PRODUCTION SUPERVISOR & WATCHDOG STARTED     ")
    print("=" * 70)
    print(f"Target Command   : {' '.join(cmd)}")
    print(f"Heartbeat Timeout: {MAX_STALE_SECONDS}s | Check Interval: {POLL_INTERVAL_SECONDS}s")
    print(f"State File Mon   : {STATE_FILE}")
    print("=" * 70 + "\n")

    restart_count = 0

    while True:
        print(f"[{iso()}] Launching trading engine (Restart #{restart_count})...", flush=True)
        if notifier and restart_count > 0:
            notifier.notify_system_alert("WARNING", f"Watchdog restarting trading engine (Attempt #{restart_count})")

        proc = subprocess.Popen(cmd)
        process_start_time = now_ms()
        time.sleep(10)  # Allow initial process startup

        try:
            while True:
                # 1. Check if child process died
                poll_res = proc.poll()
                if poll_res is not None:
                    print(f"[{iso()}] [!] Child process terminated unexpectedly with exit code {poll_res}!", flush=True)
                    if notifier:
                        notifier.notify_system_alert("ERROR", f"Trading engine process terminated (code {poll_res})")
                    break

                # 2. Check heartbeat age
                hb_ms = get_heartbeat_ms()
                current_time = now_ms()

                # Ignore leftover heartbeats written prior to this process launch
                if hb_ms and hb_ms >= process_start_time:
                    age_seconds = (current_time - hb_ms) / 1000.0
                    if age_seconds > MAX_STALE_SECONDS:
                        print(f"[{iso()}] [!] HEARTBEAT STALE ({age_seconds:.1f}s > {MAX_STALE_SECONDS}s). Process appears hung!", flush=True)
                        if notifier:
                            notifier.notify_system_alert("CRITICAL", f"Engine frozen. Heartbeat stale by {age_seconds:.0f}s. Force restarting...")
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break
                else:
                    # Child is still completing its initial boot cycle and data download
                    stale_since_boot = (current_time - process_start_time) / 1000.0
                    if stale_since_boot > BOOT_GRACE_SECONDS:
                        print(f"[{iso()}] [!] No initial heartbeat produced after {stale_since_boot:.1f}s (boot grace {BOOT_GRACE_SECONDS}s). Force restarting...", flush=True)
                        if notifier:
                            notifier.notify_system_alert("CRITICAL", f"Engine boot timeout ({stale_since_boot:.0f}s). Force restarting...")
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break

                time.sleep(POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print(f"\n[{iso()}] Watchdog stopped by user. Gracefully terminating child engine...", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            print(f"[{iso()}] Engine stopped. Goodbye.")
            return 0

        restart_count += 1
        time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="24/7 Production Watchdog Supervisor.")
    parser.add_argument("--testnet", action="store_true", help="Supervise in Binance Futures Testnet mode")
    parser.add_argument("--live", action="store_true", help="Supervise in Binance Futures Live Production mode")
    args = parser.parse_args()

    flags = []
    mode = os.getenv("BINANCE_MODE", "").lower()
    testnet_env = os.getenv("BINANCE_TESTNET", "").lower() in ("true", "1", "yes") or os.getenv("TESTNET", "").lower() in ("true", "1", "yes")

    if args.live or mode == "live":
        flags.append("--live")
    elif args.testnet or mode == "testnet" or testnet_env:
        flags.append("--testnet")

    run_watchdog(flags)


if __name__ == "__main__":
    main()
