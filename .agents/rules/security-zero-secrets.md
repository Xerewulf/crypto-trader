---
trigger: always_on
description: Absolute Zero-Secret and Sensitive Data Protection Policy for GitHub and Git commits in Xerewulf/crypto-trader.
---

# Zero-Secret & Sensitive Data Protection Policy

This rule is **permanently active and non-negotiable** across all workflows, agents, tasks, and commits in `Xerewulf/crypto-trader`:

## 1. Absolute Prohibition on Sensitive Data in Git
Under NO circumstance shall sensitive data ever be staged, committed, or pushed to GitHub:
- **Exchange Credentials**: Binance API Keys, API Secrets, Passphrases, HMAC signatures, IP whitelists.
- **Wallet & Cryptographic Keys**: Private keys, mnemonic seed phrases, keystore files (`*.json`, `*.dat`).
- **Communication & Notification Tokens**: Telegram bot tokens, private chat IDs, Discord webhook URLs.
- **Authentication Secrets**: Passwords, bearer tokens, JWT secrets, session keys, SSH/TLS keys (`*.pem`, `*.key`, `id_rsa`).
- **Environment & Credential Files**: `.env`, `.env.*`, `credentials.json`, `secrets.json`, `id_ed25519`.

## 2. Proper Secret Handling Architecture
- **In Local Development**: Credentials live ONLY in ignored files (`trader/credentials.json`, `.env`), which MUST be verified in `.gitignore` before creation.
- **In Git Commits**: ONLY commit example/template files with dummy placeholders:
  - `trader/credentials.example.json` with `"YOUR_BINANCE_API_KEY_HERE"`
  - `.env.example` with `your_binance_api_key_here`
- **In Cloud / Production**: Credentials are NEVER written to disk; they are passed directly as encrypted cloud environment variables (Render, Railway, Fly.io, or Docker runtime environment flags).

## 3. Mandatory Pre-Commit Secret Scan
Before issuing any `git commit`, execute:
```bash
git diff --cached
```
Verify that:
1. No actual credential, high-entropy API key, or webhook URL is in the staged diff.
2. Only template files with obvious uppercase placeholders are included.
3. Every sensitive file pattern is listed in `.gitignore`.
