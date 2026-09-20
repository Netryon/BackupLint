# Missing named-volume lab (Scenario C)

Creates a named volume and deliberately omits its host mountpoint from `backup_paths`.

```bash
./verify-coverage.sh
```

Expected: `Result: FAIL`, exit code `1`.
