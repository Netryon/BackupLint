# Realistic backup coverage labs

| Scenario | Lab | Expected |
|----------|-----|----------|
| A — everything backed up | `complete-backup/` | PASS |
| B — bind mount omitted | `missing-bind-mount/` | FAIL |
| C — named volume omitted | `missing-volume/` | FAIL |
| D — parent path covers apps | `nested-paths/` | PASS |
| E — similar name not parent | `edge-cases/` | FAIL |

Run all:

```bash
./test-lab/run-milestone6.sh
```
