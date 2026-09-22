# Dashboard Sync

- Sync is optional and requires both `DASHBOARD_URL` and `DASHBOARD_TOKEN`.
- `DASHBOARD_URL` must be an HTTP(S) origin without path, query, fragment, or embedded credentials.
- After daily booking, queue a durable authenticated `PUT` to `/api/v1/state/household-agent/current-tasks`.
- Payload represents current local day only: UTC `updated_at`, every configured resident once, `off_day` status, and only tasks actually committed that day.
- Dashboard failure must not block allocation, local state persistence, or Telegram delivery.
- A newer pending daily snapshot replaces an older undelivered snapshot; preserve this stale-update protection.
