# ÆON

**Your calendar describes what you planned. ÆON explores what could happen.**

[Watch the demonstration](deliverables/video/AEON-demo.mp4).

## See the fragile parts of your day

ÆON combines your calendar, a deadline found in a selected email, and travel observations to show where a day could unravel. Its Shadow Calendar compares planned times with projected arrivals and estimates lateness across 1,000 simulated futures.

Choose the appointment to protect, the personal blocks you allow to move, and an acceptable time window. ÆON compares up to three alternatives with their before/after times, signed shifts and remaining risk. Preview a plan locally, then explicitly authorize a Calendar update if it suits you. Receipts record the result, and conditional undo restores the original times only if the event has not changed since the update.

The built-in synthetic day works without credentials. Its traffic incident adds 12 minutes to possible journey durations so you can explore a cascade and compare another plan.

## Connected services

| Service | Implemented use |
| --- | --- |
| Google Calendar | Separate consent for reading a chosen period and for applying a selected plan to personal, standalone events without other attendees; update receipts and conditional undo |
| Gmail | Read up to 10 excerpts from your search; select one for interpretation and confirm its proposed arrival deadline before it affects a forecast |
| Claude Haiku 4.5 | Interpret only the selected excerpt and relevant event summaries after your consent; Structured Outputs, strict validation, at most 300 output tokens and no automatic retries |
| Nominatim + OSRM | Default address lookup and driving route option, without a Google key or billing account; no live traffic; public services intended for light, manual use |
| Google Routes | Optional route observations between explicitly entered addresses, using a separate server key and your Google billing configuration |

An imported forecast requires a complete Calendar read of at most 100 events and explicit travel information between consecutive events. Locations are never inferred from event titles. A same-location declaration must reflect an actual shared location.

Simulation, plan search and previews reuse observations: **zero LLM calls in simulation and zero external calls in plan search**. Route observations are cached for five minutes. Explicit route requests have a 20-attempt session budget; Google Routes also has a persistent application limit of 3,000 attempts per UTC day. Failures consume the applicable request budget, while cache hits do not. These request limits are not a monetary cap.

## Run locally

Requirements: Python 3.9+ and a recent browser. The application uses the Python standard library and needs no npm installation.

From the project directory:

```sh
scripts/dev
```

Open [ÆON at 127.0.0.1:8787](http://127.0.0.1:8787). For another port, use `scripts/dev --port 8788`. Start with the synthetic day, or configure the connected services:

1. Copy [.env.example](.env.example) to a local `.env` without replacing an existing configuration, and restrict its permissions: `cp -n .env.example .env`, then `chmod 600 .env`.
2. For Calendar and Gmail, enable their APIs in your Google project and create a Desktop OAuth client. Set `AEON_GOOGLE_CLIENT_ID`, the optional `AEON_GOOGLE_CLIENT_SECRET`, and `AEON_GOOGLE_REDIRECT_URI=http://127.0.0.1:8787/` with the actual port. Add any required test users to your consent screen.
3. For Claude, set `AEON_ANTHROPIC_API_KEY`. For optional Google Routes, set `AEON_GOOGLE_ROUTES_API_KEY` and configure API restrictions, quotas and billing. OSRM needs neither key.
4. Restart the server. In a normal browser, connect Calendar and Gmail separately, then explicitly read a period and search excerpts. Select a message and relevant events, analyze with Claude, and confirm its proposed deadline.
5. Refresh the imported calendar, supply travel, calculate its forecast and search alternatives. Connect separate Calendar write access before applying a chosen plan.

Credentials stay on the local server. Sessions, OAuth tokens, source caches and action receipts stay in memory for at most one hour and disappear on restart. The Google route counter persists locally. Forgetting a source clears its local connection and data; it does not revoke consent at Google. The server listens on loopback.

## Reliability and measured demonstration

Run the product tests with Python 3.9+ and a Node.js version supporting `node --test`:

```sh
python3 scripts/test
```

The current product suite passes 345 Python tests and 62 JavaScript tests. It covers the numerical engine, bounded planner, connectors, OAuth, scenario assembly, strict deadline extraction, HTTP integration, action policies, stale-state handling, conditional undo, persistent route budgets and browser-side behavior. External services use injected transports in automated tests. JavaScript syntax is checked as well.

A measured demonstration used English synthetic event and email content stored in real Google accounts: three Calendar events, one Gmail excerpt, one Claude extraction and one Google Routes observation. Claude proposed an 18:00 arrival deadline. Across 1,000 draws, the dinner's estimated lateness risk changed from 100% to 0% when a selected personal block moved from 17:00–18:00 to 15:00–16:00. Seven arrangements were evaluated and three candidates returned. The chosen update was observed in Google Calendar, then undo restored the original times.

Those percentages describe the model under its stated, uncalibrated assumptions. The 120-minute shift is a change in schedule, not measured time saved. Search is bounded and does not guarantee a global optimum; no email sending is implemented.
