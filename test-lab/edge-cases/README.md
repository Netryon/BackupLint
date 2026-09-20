# Edge-cases lab (Scenario E)

Backup path `./app` must not be treated as covering mount `./app2`.

```bash
./verify-coverage.sh
```

Expected: `Result: FAIL`, exit code `1`.
