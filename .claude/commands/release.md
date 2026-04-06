---
description: Bump version, tag, and push a new release
---

Release a new version of Emerge.

The user invokes `/emerge:release <version>` (e.g. `/emerge:release 0.2.6`).
If no version is given, **default to a patch bump**: read the current version from `.claude-plugin/plugin.json`, increment the patch number by 1, and proceed without asking.

Steps:

1. **Determine version**: if provided, validate it matches `MAJOR.MINOR.PATCH` (no leading `v`). If not provided, read the current version with:
   ```bash
   python3 -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])"
   ```
   then auto-compute `MAJOR.MINOR.(PATCH+1)`.

2. **Commit any uncommitted changes** before bumping:
   - Run `git status` to check for dirty state.
   - If there are uncommitted changes, stage all tracked files and commit:
     ```bash
     git add -u
     git commit -m "chore: pre-release cleanup"
     ```
   - Skip if working tree is already clean.

3. **Bump all version files** using the version-bump script:
   ```bash
   bash scripts/bump-version.sh {NEW}
   ```
   This updates `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` (both version fields) atomically.

4. **Update README.md** — replace the version badge:
   ```
   ![Version](https://img.shields.io/badge/version-v{OLD}-blue)
   ```
   with:
   ```
   ![Version](https://img.shields.io/badge/version-v{NEW}-blue)
   ```

5. **Commit** the version bump:
   ```bash
   git add .claude-plugin/plugin.json .claude-plugin/marketplace.json README.md
   git commit -m "chore: bump version to {NEW}"
   ```

6. **Tag** the release:
   ```bash
   git tag v{NEW}
   ```

7. **Push** (branch + tag):
   ```bash
   git push origin main
   git push origin v{NEW}
   ```

8. Report success: confirm the push succeeded and show the new version. Remind the user that Claude Code will detect the update via marketplace refresh.
