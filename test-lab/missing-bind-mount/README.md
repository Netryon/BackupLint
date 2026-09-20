# Missing bind-mount lab

Omits `./vaultwarden` from `backup_paths` while backing up `./sonarr`.

```bash
./verify-coverage.sh
```

Expected: BackupLint exits `1` and reports vaultwarden as not protected.
