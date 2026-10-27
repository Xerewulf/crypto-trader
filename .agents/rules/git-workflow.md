---
trigger: always_on
description: Git branching and Pull Request / Merge Request operating workflow for Xerewulf/crypto-trader.
---

# Git Branching & Merge Request Operating Workflow

For any new code, feature, bugfix, or refactoring in `Xerewulf/crypto-trader`:

1. **Never Commit Directly to `main`**:
   - `main` is protected as the production baseline.
   - Always ensure you are on a dedicated feature or fix branch before writing commits.

2. **Branch Naming Standard**:
   - `feat/<feature-name>`: New capabilities (e.g. `feat/funding-arbitrage-execution`)
   - `fix/<bug-name>`: Bug fixes (e.g. `fix/depth-imbalance-calculation`)
   - `chore/<task-name>`: Tooling, rules, dependencies, workflow updates
   - `refactor/<target>`: Structural code improvements

3. **Pre-Commit Verification**:
   - Run unit tests (`cd tests; python -m unittest discover -s . -p "test_*.py"`) and verify that all test suites pass with zero regressions.
   - Verify that no secrets, credentials, or transient logs are being committed.

4. **Commit & Push Flow**:
   - Commit with clean, semantic commit messages (e.g., `feat(module): description`).
   - Push the branch to `origin`:
     ```bash
     git push -u origin <branch-name>
     ```

5. **Pull / Merge Request Link**:
   - Always output the direct GitHub Pull Request URL so the user can review and merge into `main` with one click:
     `https://github.com/Xerewulf/crypto-trader/pull/new/<branch-name>`
