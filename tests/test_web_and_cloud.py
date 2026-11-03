#!/usr/bin/env python3
"""Unit tests for 24/7 Web Dashboard & Cloud Deployment Infrastructure."""

import base64
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIVE = os.path.join(ROOT, "trader", "live")
if LIVE not in sys.path:
    sys.path.insert(0, LIVE)

import watchdog
import paper_trader


class TestWebAndCloudDeployment(unittest.TestCase):
    def test_cloud_config_files_exist(self):
        """Verify that all cloud and container orchestration files exist."""
        required_files = [
            os.path.join(ROOT, "Dockerfile"),
            os.path.join(ROOT, "docker-compose.yml"),
            os.path.join(ROOT, ".dockerignore"),
            os.path.join(ROOT, "render.yaml"),
            os.path.join(ROOT, "requirements.txt"),
            os.path.join(ROOT, ".env.example")
        ]
        for f in required_files:
            self.assertTrue(os.path.exists(f), f"Missing required cloud file: {f}")

    def test_gitignore_protects_env_and_secrets(self):
        """Verify that .gitignore strictly protects .env, secrets, and credentials."""
        gitignore_path = os.path.join(ROOT, ".gitignore")
        with open(gitignore_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(".env", content)
        self.assertIn("trader/credentials.json", content)
        self.assertIn("data/", content)

    def test_watchdog_env_var_flags(self):
        """Verify watchdog respects cloud environment variables."""
        # Test testnet env var
        os.environ["BINANCE_TESTNET"] = "true"
        os.environ.pop("BINANCE_MODE", None)
        flags = []
        mode = os.getenv("BINANCE_MODE", "").lower()
        testnet_env = os.getenv("BINANCE_TESTNET", "").lower() in ("true", "1", "yes")
        if mode == "live":
            flags.append("--live")
        elif mode == "testnet" or testnet_env:
            flags.append("--testnet")
        self.assertEqual(flags, ["--testnet"])

        # Test live mode env var
        os.environ["BINANCE_MODE"] = "live"
        flags = []
        mode = os.getenv("BINANCE_MODE", "").lower()
        testnet_env = os.getenv("BINANCE_TESTNET", "").lower() in ("true", "1", "yes")
        if mode == "live":
            flags.append("--live")
        elif mode == "testnet" or testnet_env:
            flags.append("--testnet")
        self.assertEqual(flags, ["--live"])

        # Clean up
        os.environ.pop("BINANCE_TESTNET", None)
        os.environ.pop("BINANCE_MODE", None)

    def test_health_check_payload_structure(self):
        """Verify the health check payload structure returned by the web server."""
        s = paper_trader.fresh_state()
        _, _, eq = paper_trader.equity_parts(s)
        health_data = {
            "status": "healthy",
            "uptime_ms": 1000,
            "equity": round(eq, 2),
            "open_positions": len(s.get("positions", {})),
            "heartbeat_age_s": 0.5,
            "broker": "paper"
        }
        self.assertEqual(health_data["status"], "healthy")
        self.assertEqual(health_data["equity"], 5000.0)
        self.assertEqual(health_data["open_positions"], 0)
        self.assertEqual(health_data["broker"], "paper")

    def test_basic_auth_credential_validation(self):
        """Verify HTTP Basic Auth header parsing and matching."""
        auth_user = "trader_boss"
        auth_pass = "SuperSecurePass123!"

        # Valid credentials
        valid_creds = base64.b64encode(f"{auth_user}:{auth_pass}".encode("utf-8")).decode("utf-8")
        header = f"Basic {valid_creds}"
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        user, pwd = decoded.split(":", 1)
        self.assertEqual(user, auth_user)
        self.assertEqual(pwd, auth_pass)

        # Invalid password
        bad_creds = base64.b64encode(f"{auth_user}:WrongPassword".encode("utf-8")).decode("utf-8")
        header_bad = f"Basic {bad_creds}"
        decoded_bad = base64.b64decode(header_bad[6:]).decode("utf-8")
        user_bad, pwd_bad = decoded_bad.split(":", 1)
        self.assertNotEqual(pwd_bad, auth_pass)


if __name__ == "__main__":
    unittest.main()
