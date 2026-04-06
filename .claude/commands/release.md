---
description: Bump version, tag, and push a new release
---

Release a new version of Emerge.

The user invokes `/emerge:release <version>` (e.g. `/emerge:release 0.2.3`).
If no version is given, **default to a patch bump**: read the current version from the badge in `README.md`, increment the patch number by 1, and proceed without asking.

Steps:

1. **Determine version**: if provided, validate it matches `MAJOR.MINOR.PATCH` (no leading `v`). If not provided, read the current version from the badge (`https://img.shields.io/badge/version-v{CURRENT}-blue`) and auto-compute `MAJOR.MINOR.(PATCH+1)`.

2. **Commit any uncommitted changes** (staged or unstaged) before bumping the version:
   - Run `git status` to check for dirty state.
   - If there are uncommitted changes, stage all tracked files and commit them:
     ```
     git add -u
     git commit -m "chore: pre-release cleanup"
     ```
   - Skip this step if the working tree is already clean.

3. **Update README.md** — replace the version badge line:
   ```
   ![Version](https://img.shields.io/badge/version-v{OLD}-blue)
   ```
   with:
   ```
   ![Version](https://img.shields.io/badge/version-v{NEW}-blue)
   ```

4. **Commit** the version bump:
   ```
   git add README.md
   git commit -m "chore: bump version to {NEW}"
   ```

5. **Push**:
   ```
   git push origin main
   ```

6. Report success: confirm the push succeeded and show the new version.
