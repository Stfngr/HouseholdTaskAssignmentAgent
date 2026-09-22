# Operations

- Docker is primary supported deployment route. CI builds and publishes only `linux/arm64` images from `main` after Python 3.12 tests pass.
- Container runs as unprivileged UID/GID `10001`, with read-only application/config mounts and writable persistent `storage` mount.
- `deploy/household-agent.service` remains systemd alternative. Never run Docker and systemd concurrently with same Telegram credentials.
- Do not manually edit or delete `storage/state.json`; doing so can lose history/cooldowns and duplicate outgoing notifications.
- Stop running service before replacing related configuration files or taking consistent state backups.
- `python3 main.py` starts real service and can send messages. Use `--check-config` or `--preview` for safe local inspection.
