# One-off verified artifact cleanup

Prepared only; author performed no remote mutations.

Target: apfullai/ride-tiles artifact12092658917, name github-pages, successful main run39012173471,133807974bytes, digest sha256:d4bd6b2a45c125c6ad2956202c0de0a6b1be6ad11004064e3e0bab1fea735d7c. User explicitly approved this one artifact and one job-scoped Actions write grant.

Release owner places cleanup.yml at .github/workflows/cleanup-verified-artifact.yml on feat/mellow-z17-mirror only; script/tests at cleanup/. Trigger separately with cleanup/request.json and commit marker [cleanup-verified-artifact-12092658917]. Never change overlays/request.json in cleanup commit. Same ride-tiles-pages concurrency serializes with city and mirror publishing. Do not merge workflow to main. Remove one-off workflow in subsequent mirror-trigger commit after success.

Five-minute job,240-second command cap,20-second individual HTTP timeout, no automatic retries or artifact uploads. Existing ephemeral GITHUB_TOKEN is supplied only to exact allowlisted api.github.com URLs; no token reaches Pages. Redirects never followed. Metadata pins successful run, exact main SHA, artifact name/id/size/digest/unexpired status. Live manifest/snapshot/checkpoint SHA256s must match fresh full reconstruction proof before deletion. Only one DELETE request is possible. HTTP204, subsequent GET404, and unchanged live hashes are required for success. JSON receipt prints to job logs.

If any step fails after DELETE, outcome may need read-only reconciliation; never rerun deletion blindly. Preexisting404 fails metadata verification without deletion. No security settings, credentials, other artifact, branch/main state or publisher source changes are made by script.

Offline tests: python -m unittest discover -s cleanup -p 'test_*.py' -v

Recovery limit: full published snapshot and both crop byte streams were verified independently without this artifact. Local generation-runtime parity wasn't proved (matching osmium absent), and future site availability isn't guaranteed.
