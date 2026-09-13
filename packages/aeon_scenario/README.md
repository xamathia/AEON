# ÆON live scenario assembly

`aeon_scenario` assembles observations already obtained and validated by the
caller into canonical engine input. The module makes no network reads, does not
consult the clock, infer locations or grant permissions.

## API

```python
from packages.aeon_scenario import assemble_scenario

result = assemble_scenario(
    scenario_id="session-2026-09-14",
    timezone="Europe/Paris",
    horizon={
        "start": "2026-09-14T08:00:00+02:00",
        "end": "2026-09-14T20:00:00+02:00",
    },
    calendar={
        "events": [{
            "id": "focus",
            "title": "Private block",
            "planned_start": "2026-09-14T10:00:00+02:00",
            "planned_end": "2026-09-14T11:00:00+02:00",
            "classification": "flexible",
            "private": True,
            "overrun_minutes": {"min": 0, "mode": 0, "max": 0},
            "source": {
                "provider": "google_calendar",
                "reference": "google_calendar:primary:focus",
                "synthetic": False,
                "assumption": "uncalibrated overrun, zero prior",
            },
        }],
        "complete": True,
        "read_at": 1789372800.0,
    },
    travel_edges=[],
    constraints=[],
    seed=42,
    samples=1000,
)

assert result["status"] == "ready"
```

All arguments are keyword-only and copied before processing. The call always
returns an object with this shape:

```json
{
  "status": "ready",
  "scenario": {"schema_version": "1.0", "mode": "live"},
  "issues": [],
  "provenance": {
    "calendar_read_at": 1789372800.0,
    "calendar_complete": true
  },
  "assumptions": []
}
```

When the Calendar read is partial, contains no events or lacks an adjacent edge,
`status` is `incomplete` and `scenario` is `null`. The `CALENDAR_INCOMPLETE`,
`NO_EVENTS` and `MISSING_TRAVEL_EDGE` codes are accumulated and sorted. A missing
edge is never replaced by zero travel time; an explicit zero must retain its
source and assumption.

Structural errors raise `ValueError` with a fixed message that does not repeat
event titles, Gmail content or arbitrary values. Constraints are canonical engine
objects: this boundary accepts neither Gmail text nor raw LLM output.

## Provenance and limits

Events are sorted by UTC instant without changing identifiers or canonical
fields. All sources, including those marked `synthetic=true`, are preserved in
the overall `mode="live"` scenario. Nonempty source assumptions are deduplicated
and sorted.

`calendar.complete` describes only the completeness of the supplied read. It does
not prove absolute freshness, an actual account connection or calibrated
distributions. A `ready` result can be passed to
`packages.aeon_engine.simulate_day`; an `incomplete` result must not be simulated.
