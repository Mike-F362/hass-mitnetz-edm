# Test fixtures

Captured response shapes from the EDM portal, with every identifier replaced.

The meter point designation (Zählpunktbezeichnung), the MaLo IDs, the internal
line IDs and the measured values here are **invented**. Only the *structure* is
real: key names, nesting, the `root` wrapper, the `"<obis>, <meter id>"` naming
of lines, and the relationship between `timestamps` and `timestampsLeft`.

That distinction matters for publication. A meter point designation together
with a quarter-hourly load profile is personal data under the GDPR: load
profiles reveal occupancy, sleeping patterns and holidays, and are well known
to be re-identifiable. None of that belongs in a public repository.

| File                       | Endpoint                                    |
|----------------------------|---------------------------------------------|
| `measurement_points.json`  | `/sidebarMultiMp/rlm`                       |
| `timeseries_base.json`     | `/data/mpline/genericto/.../base`           |
| `timeseries_hours.json`    | `/data/mpline/genericto/.../hours`          |
| `load_profile_two_days.json` | values only, see below                    |

`load_profile_two_days.json` is not a portal response but two days of
quarter-hourly values for the overlap regression test. It was derived from a
real load profile because the failure depends on its shape (quiet nights, car
charging at several kW, near-zero import while PV covers the load), and then
anonymised: every value is scaled by a deterministic factor between 0.85 and
1.15 and rounded, the dates are moved, and no identifier is included.

Two structural properties the tests depend on:

- `timeseries_base.json` — `timestamps` holds the interval **end**,
  `timestampsLeft` the **start**; they differ by exactly one interval. Reading
  the wrong one shifts every value into the future.
- `timeseries_hours.json` — at an aggregation coarser than the native
  resolution the portal fills `minima`/`maxima`/`minTimestamps`/`maxTimestamps`
  with the extremes of the sub-intervals. At `base` resolution those arrays are
  empty.

To test against the real portal instead, see `tests/live_config.example.json`.
