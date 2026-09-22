# Household Task Agent

Python 3.12 service that fairly assigns household tasks, persists assignments locally, sends German Telegram messages, and can sync current tasks to a home dashboard.

Install runtime and test dependencies with `python3 -m pip install -r requirements.txt`.

Run before completing code changes:

```bash
python3 -m unittest discover -s tests -v
python3 main.py --check-config
```

Do not use `python3 main.py` for verification: it can send real Telegram messages and must not run alongside deployed service.

Read focused guidance only when task needs it:

- [Architecture](docs/agent-guides/architecture.md)
- [Testing](docs/agent-guides/testing.md)
- [Configuration and Secrets](docs/agent-guides/configuration-and-secrets.md)
- [Operations](docs/agent-guides/operations.md)
- [Dashboard Sync](docs/agent-guides/dashboard-sync.md)
