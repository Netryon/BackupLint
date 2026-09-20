# Demo assets

GIFs and PNGs are produced from **real** `backuplint scan` runs against disposable
fixtures using short `/srv/...` paths.

## Regenerate

```bash
cd /path/to/backuplint
source .venv/bin/activate
./docs/demo/prepare-fixtures.sh   # needs sudo for /srv demo dirs
python3 docs/demo/render-gifs.py
```

Outputs:

- `backup-audit-fail.gif` / `.png` — primary README demo
- `backup-audit-pass.gif` / `.png`
- `restic.gif` / `restic-audit.png`

Optional VHS tapes remain for hosts where Chromium recording works; prefer
`render-gifs.py` on headless systems.
