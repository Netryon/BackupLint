# Complete-backup lab

Verifies mount discovery and successful coverage when lab bind mounts are included in `backup_paths`.

```bash
./verify-discovery.sh
./verify-coverage.sh
```

Expected coverage result: exit code `0` for protected bind mounts. Named volumes may appear as `unsupported` until host-path resolution is implemented.
