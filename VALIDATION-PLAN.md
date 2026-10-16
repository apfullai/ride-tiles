# US tile automation acceptance tests

These are required future tests, not claims that implementation exists or has passed.

## Admission and scheduling

1. A fresh public repository with a passed pilot admits one bounded job using a standard runner.
2. A private repository, missing pilot result, nonstandard runner, disabled maintenance flag, or unreadable run history admits no expensive work.
3. Concurrent events serialize. Manual reruns and earlier failed attempts are included in the runtime admission decision.
4. At budget exhaustion, the job exits before source downloads. Failed checks do not trigger automatic rerun loops.
5. A fifth weekly opportunity plus the monthly opportunity stays within six scheduled opportunities. UTC month rollover resets only the new month's budget.
6. Disabled or delayed scheduled workflows do not falsely advance freshness or queue state.

## Coverage and freshness

7. Overdue existing coverage precedes every expansion candidate. Forecast maintenance exceeding the budget blocks growth.
8. Verified unchanged source checksum advances source-check freshness without tile churn; unavailable source does not.
9. Changed source or generator parameters regenerate affected regions only. A source state shared by several selected regions downloads once.
10. Adjacent city candidates can share a catchment without duplicate features or tiles. Census candidate rank remains distinct from the actual coverage bounds.
11. No region can remain silently stale beyond the declared 90-day target; stale metadata is explicit and client fallback behavior is tested.

## Reproducibility and privacy

12. Reordering equivalent source features or changing a generation timestamp does not change tile bytes or URLs.
13. Every published tile contains a decodable `cycleinfra` layer with expected categories and valid coordinates. Source timestamps and attribution are present.
14. Public output contains only allowlisted tile/public metadata files; scan for secrets, private app files, personal routes, GPS records, and private provenance.
15. No telemetry endpoint, tracking identifier, database, KV write, or analytics SDK is introduced.

## Publication and recovery

16. Site bytes include all current and rollback files and must remain below 800,000,000 bytes. Oversize output never deploys.
17. A missing prior snapshot or expired artifact cannot cause an existing region to disappear. Recovery must enumerate and verify the prior published snapshot, or stop.
18. Partial generation, corrupt downloads, validation failures, deploy failures, and cancelled jobs retain the old live manifest and do not advance queue success.
19. After successful publication, anonymous manifest and tile fetches, CORS, binary content, checksums, cache headers, and absent-tile responses are verified.
20. Force each provider failure separately and confirm the client reaches GitHub first and OpenFreeMap second, with no blank cycling layer caused by an empty-but-valid primary response.
21. End-to-end test the deployed commit and provider-order configuration. A successful local generator test alone does not pass the hosting pilot.

