# Botrix Fork Notes

Self-hosting Dograh with our own customizations while continuing to pull updates from the maintainers.

## Remotes & branches

- `origin` = our fork (`Jatin-35/dograh`)
- `upstream` = maintainers' repo (`dograh-hq/dograh`)
- `main` = clean mirror of `upstream/main`. Fast-forward only. **Never commit customizations here.**
- `botrix-main` = our actual working/deploy branch. All customizations live here.

## Sync routine (pulling maintainer updates)

```
git fetch upstream
git checkout botrix-main
git merge upstream/main
# resolve conflicts if any (see below)
git push origin botrix-main
```

## Conflict-avoidance strategy

- Prefer adding customizations as **new files** (new components/pages/modules) instead of editing existing upstream files directly — new files never conflict on merge.
- When an existing file must be touched, keep the change to a minimal "hook point" (e.g. one import/registration line) so any future conflict there is small and fast to resolve.

## If a merge conflict is too complex

1. **Sync more often** — smaller diffs between syncs mean smaller conflicts. This is the main preventive measure.
2. **Isolate and reapply** — if one file conflicts badly, abort the merge (`git merge --abort`), take upstream's version of just that file, then manually reapply our specific customization on top.
3. **Worst case** — defer/skip that one upstream change for now, keep our version, and revisit it as a standalone task later rather than blocking the whole sync.

## Local-only files

`docs/skills/` (scope doc + enterprise PDF) is gitignored — internal planning docs, never committed to any branch.

## Open decision

Considering building a completely fresh UI instead of customizing `ui/` in place.

- Fresh UI = zero frontend merge conflicts ever (we'd stop tracking upstream's `ui/` changes entirely).
- Tradeoff: we lose the implicit signal upstream's UI diffs currently give when the backend API contract changes — we'd need to manually watch `api/` changes and update our frontend to match.
- Not yet decided: whether the fresh UI lives alongside the old `ui/` folder in this repo (kept as fallback/reference) or in a separate repo, and whether `ui/` gets removed eventually.
