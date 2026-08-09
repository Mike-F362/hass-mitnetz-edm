## What does this change, and why?

<!-- The reasoning matters more than the diff; the diff is already visible. -->

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest -m "not live"` passes
- [ ] No real meter point designations, MaLo IDs or load profiles added
      anywhere — see CONTRIBUTING.md
- [ ] CHANGELOG.md updated under `## [Unreleased]`
- [ ] If entity IDs, unique IDs, units, state classes or statistic IDs change:
      said so explicitly above, because that breaks existing installations
