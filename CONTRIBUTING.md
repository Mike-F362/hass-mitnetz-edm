# Contributing

Bug reports, especially from meters whose portal accounts expose OBIS lines
this integration has never seen, are the most useful thing you can send.

## Never commit real meter data

This is the one rule that matters more than style.

A meter point designation together with a load profile is personal data under
the GDPR. Test fixtures under `tests/fixtures/` therefore use **invented**
meter point designations, MaLo IDs, line IDs and values. Only the *structure*
of those files is real.

`json/`, `reports/`, `*.har` and `tests/live_config.json` are git-ignored for
this reason. Please keep it that way, and check diagnostics and logs before
attaching them to an issue.

To exercise the real portal, copy `tests/live_config.example.json` to
`tests/live_config.json`, fill it in, and run `pytest -m live`. That file stays
on your machine.

## Development

```bash
pip install -r requirements-test.txt
pytest -m "not live"
ruff check .
ruff format .
```

Python 3.13, matching Home Assistant. The suite runs against a real Home
Assistant instance including the recorder, via
`pytest-homeassistant-custom-component` — pin the release matching your Home
Assistant version if the fixtures misbehave.

A Docker-based runner is available if you would rather not install Home
Assistant locally; see the `mitnetz-tests` service in the development
compose file.

## Things worth knowing before you change them

- **The portal labels an interval by its end.** `timestamps` is the interval
  end, `timestampsLeft` the start. Points are anchored to the start; reading
  the wrong field shifts every value one interval into the future and
  mis-buckets long-term statistics. See `tests/test_interval_edges.py`.
- **The German strings in `obis.py` are search patterns, not display text.**
  `NAME_HINTS_POWER` and `NAME_HINTS_ENERGY` are matched against the portal's
  own wording when an OBIS code cannot be parsed. Translating them breaks the
  fallback silently. Display names live in `const.py` as `NAME_*`.
- **Changing entity IDs, unique IDs, units, state classes or statistic IDs is
  a breaking change**, even when it looks cosmetic: it breaks users'
  dashboards, automations and recorded history. Say so in the pull request.

## Pull requests

Keep the diff focused, update `CHANGELOG.md` under `## [Unreleased]`, and
explain *why* in the commit body — the *what* is already in the diff.
