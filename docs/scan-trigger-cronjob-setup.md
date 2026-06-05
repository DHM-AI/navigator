# Reliable Scan Trigger — cron-job.org (fixes GitHub's broken scheduler)

**Problem (2026-06-05):** GitHub Actions' cron scheduler is unreliable — on 2026-06-05
it fired ZERO daily scans during market hours (ZEUS ran 11h late, ORACLE 10h late).
For day trading this is fatal: no scans = no trades.

**Fix:** trigger the scan from cron-job.org (external, cloud, reliable) which calls
GitHub's API to dispatch the workflow. Same mechanism your AEGIS jobs already use.
Compute still runs on GitHub (free, public repo) — only the *trigger* moves off
GitHub's broken cron.

---

## The job to create on cron-job.org

You already have cron-job.org jobs that dispatch `trail_stops.yml` (AEGIS). The
**fastest path is to DUPLICATE one of those AEGIS jobs** and change 3 things
(URL, schedule, title). The Authorization header / PAT is already set on those jobs.

If creating fresh, here's everything:

| Field | Value |
|---|---|
| **Title** | `Illuminati — Daily Scan trigger` |
| **URL** | `https://api.github.com/repos/DHM-AI/navigator/actions/workflows/daily_scan.yml/dispatches` |
| **Request method** | `POST` |
| **Request body** | `{"ref":"main"}` |

**Headers** (Advanced → Headers):
```
Authorization: token <YOUR_ILLUMINATI_PAT>
Accept: application/vnd.github.v3+json
Content-Type: application/json
```
*(Copy the exact Authorization header value from one of your existing AEGIS
cron-job.org jobs — it's the same PAT.)*

**Schedule** (set job timezone = `America/New_York`):
- **Minutes:** 0 and 30
- **Hours:** 9, 10, 11, 12, 13, 14, 15
- **Days:** Mon–Fri
- → fires every 30 min, 9:00 AM–3:30 PM ET

*(Optional precision: untick the 9:00 slot — pre-market, scores but the
market-hours gate blocks trades anyway, so it's harmless either way. The scan
won't double-trade: duplicate-position guards prevent re-buying the same ticker.)*

---

## Verify it works
1. Save the job, then click **"Run now"** ("Test run") on cron-job.org.
2. Expected response: **HTTP 204** (GitHub accepted the dispatch).
3. Check GitHub Actions → Daily Scan → a new run should appear within seconds.
4. From then on it fires automatically every 30 min during market hours,
   regardless of whether GitHub's own cron is working.

## Optional second layer (Mac launchd backup)
`scripts/morning_trigger.sh` already does this exact dispatch from your Mac. To
run it as a local backup (only works when the Mac is on), it needs
`ILLUMINATI_PAT` added to `market-predictions/.env`, then a launchd plist on a
30-min StartInterval (same pattern as `com.illuminati.aegis.plist`). cron-job.org
above is the primary/reliable path; the launchd is just redundancy.

Built 2026-06-05.
