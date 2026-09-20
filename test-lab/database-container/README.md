# Database-container lab

Deploys PostgreSQL with a bind-mounted data directory.

```bash
./verify-coverage.sh
```

Expected:

- data path protected when listed in `backup_paths`
- database workload warning present
- `Result: WARN`, exit code `0`
