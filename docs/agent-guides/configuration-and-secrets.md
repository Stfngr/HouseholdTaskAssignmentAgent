# Configuration And Secrets

## Configuration

- `config/Settings.json` and `config/tasks.json` are strict UTF-8 JSON. Preserve rejection of duplicate keys, unknown fields, invalid types, and invalid values.
- `Settings.json` capitalization is intentional on case-sensitive filesystems.
- Resident and task IDs are stable technical identities. Do not reuse an existing ID for a different resident or task.
- Configuration changes apply only to uncommitted planning. Historical names, assignments, and statistics remain snapshots of prior state.

## Credentials

- `.env`, `storage/`, virtual environments, and credential values stay untracked.
- Never log, print, commit, or expose Telegram tokens, dashboard tokens, or full credential-bearing request URLs.
- Existing process environment takes precedence over `.env`; do not change this without compatibility need.
- Dashboard synchronization is disabled when both `DASHBOARD_URL` and `DASHBOARD_TOKEN` are absent, and configuration is invalid when only one is set.

## State

- `storage/state.json` is durable operational data, not editable configuration.
- Preserve strict validation before loading and saving state, including duplicate JSON-key rejection and finite JSON values.
- Persist schema migrations atomically. Unknown schemas and corrupt state must stop new allocations without overwriting stored data.
