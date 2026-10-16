# Chicago metro publication and offline recovery

Both Pages deployment workflows are manual-only. Maintenance pushes run only
`snapshot-tests.yml`; they do not regenerate data or change the live site.
Dispatching either deployment workflow is a separate publication decision.
There is no cron or automatic refresh.

## Preserved public snapshot

`python publishing/restore_snapshot.py site` reconstructs the preserved two-region
public pilot into a **new** directory, entirely offline. It uses the existing
`bootstrap/chicagoland.tar.gz.part*` archive plus the checked-in deterministic
`pilot-remainder.tar.gz.part*` archive. The remainder retains NYC's complete public
OSM source, tiles, provenance, reports, and the exact original manifest and root
files. It does not duplicate Chicago's tile or archive bytes.

`bootstrap/pilot-snapshot.json` pins every remainder shard, the combined archive,
the Chicago configuration, and all 6,964 reconstructed files. The Chicago importer
also validates its archive hash, tile count, and content version. Restoration
rejects traversal, absolute paths, symlinks, duplicate archive members, and any
unexpected or mismatched file. A failed restore leaves no destination directory.
The manifest preserves the original timestamps, rather than claiming fresh data.

The input was public Actions artifact 11764694534 from run 38290673759. Its
preserved ZIP SHA-256 is
`44fcc111d920d2681e8ccdc40ef6db11ebb72bf47167a451a54d102915c00508`.
Neither restoration nor the append workflow downloads that expiring artifact.
The remainder archive is a sorted tar of original ZIP members excluding Chicago
`tiles/` and `cycling-tile-database.tar.gz`, with regular file mode 0644, uid/gid
and mtime zero, wrapped in gzip level 9 with empty filename and mtime zero, then
split into 400,000-byte shards. Public OSM data remains ODbL-licensed; attribution
and upstream source/provenance are preserved.

## Exact metro subset

`python publishing/append_metro.py site` validates every listed region against its
tile hashes, tile count, byte count, and content-derived version. Region identities
must be unique and safe path components. It accepts refreshed NYC and additional
valid regions without changing their files or manifest entries. This is support
for maintaining existing data, not a request to expand coverage.

The parent Chicago version remains frozen at `19785f3c1ca31233`. The metro subset
uses unchanged parent tiles and requires version `8d555e92cfa68b53`, 3,868 tiles,
and bounds [-88.65, 41.35, -86.8, 42.65]. Its public source archive is deterministic.
Generation and validation happen in a staging copy before input is changed.
An already-present exact metro is a byte-for-byte no-op, including manifest time;
conflicting metro metadata or bytes cause failure. Original files are preserved.

`append-metro.yml` manually restores the frozen snapshot, appends metro and
publishes it. Because this is frozen recovery, dispatching it after a newer data
release would roll that release back. Review current live coverage before dispatch.
`pilot-pages.yml` manually regenerates bounded NYC, imports the frozen Chicago
parent, appends metro, and publishes only after validation. Changes to Chicago
geometry require a separately reviewed mirror/application version migration.

## Verification

Run the dependency-free maintenance suite:

```
python -m unittest discover -s publishing/tests -v
```

It verifies exact offline reconstruction, idempotence, refreshed NYC plus an extra
region, preservation of original bytes, corrupt source and tile rejection, unsafe
archive members, unsafe region identities, and conflicting metro rejection without
input mutation. The ordinary generator tests remain in `tests/` and use the
repository's documented development dependencies.
