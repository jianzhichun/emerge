---
description: Bump version, tag, and push a new release
---

Release a new version of Emerge.

The user invokes `/emerge:release <version>` (e.g. `/emerge:release 0.2.6`).
If no version is given, **default to a patch bump**: read the current version from `.claude-plugin/plugin.json`, increment the patch number by 1, and proceed without asking.

Documentation policy:

- `docs/doc-consistency-checklist.md` is the canonical pre-release doc gate.
- `/emerge:release` MUST run and satisfy that checklist before tagging/pushing.

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
   - Note: `git add -u` stages tracked-file changes only. Untracked files are left untouched unless explicitly added.
   - Skip if working tree is already clean.

3. **Build cockpit frontend** (hard gate, fail release if build fails):
   ```bash
   cd scripts/admin/cockpit
   npm ci
   npm run build
   cd ../../..
   test -f scripts/admin/cockpit/dist/index.html
   ```
   The release is invalid without `scripts/admin/cockpit/dist/index.html`.

4. **Bump all version files** using the version-bump script:
   ```bash
   bash scripts/bump-version.sh {NEW}
   ```
   This updates `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` (both version fields) atomically.

5. **Run documentation consistency gate** using `docs/doc-consistency-checklist.md`:
   - Run the automated checker first (hard gate):
     ```bash
     python3 scripts/check_doc_consistency.py
     ```
   - Verify architecture/data-flow docs are current (`README.md` canonical diagrams, `CLAUDE.md` invariants aligned).
   - Verify command/skill docs are current (`commands/*.md`, `skills/*/SKILL.md`) and do not describe deprecated connector stub loading behavior.
   - Verify MCP surface docs match `scripts/emerge_daemon.py`.
   - Verify hook semantics docs match `hooks/*.py` + `hooks/hooks.json`.
   - Verify test baseline numbers in `README.md` (badge + quick verification baseline) are current.
   - Run a targeted stale-token scan and fix any hits that are genuinely stale:
     ```bash
     rg "377|2025-03-26|_write_connector_rules|InstructionsLoaded" README.md CLAUDE.md commands skills
     ```
     (Keep legitimate historical/compatibility mentions; only fix stale/incorrect claims.)

6. **Update README.md** — replace the version badge:
   ```
   ![Version](https://img.shields.io/badge/version-v{OLD}-blue)
   ```
   with:
   ```
   ![Version](https://img.shields.io/badge/version-v{NEW}-blue)
   ```

7. **Commit** the version bump and built cockpit assets:
   ```bash
   git add .claude-plugin/plugin.json .claude-plugin/marketplace.json README.md scripts/admin/cockpit/dist
   git commit -m "chore: bump version to {NEW}"
   ```

8. **Tag** the release:
   ```bash
   git tag v{NEW}
   ```

9. **Push** (branch + tag):
   ```bash
   git push origin main
   git push origin v{NEW}
   ```

10. Report success: confirm the push succeeded and show the new version. Remind the user that Claude Code will detect the update via marketplace refresh.
