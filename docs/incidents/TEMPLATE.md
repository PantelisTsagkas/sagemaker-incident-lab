# INC-NNN: <short title>

**Date:** YYYY-MM-DD  **Fault flag:** `FAULT=<name>`  **Instance:** ml.m5.large

## Summary
One or two sentences: what users would have experienced, and for how long.

## Detection
Which signal fired first (alarm, dashboard, Logs Insights query)? Paste the query.

## Timeline
- HH:MM fault deployed
- HH:MM first symptom visible
- HH:MM root cause identified
- HH:MM fix deployed, metrics back to baseline

## Evidence
Baseline vs incident p50 / p95 / p99, per stage. Screenshots of CloudWatch graphs.

## Root cause
What actually happened, and why the first guess was (or wasn't) right.

## Fix
What changed and how you verified it.

## Prevention
Alarm, test, or config change that would catch this earlier next time.
