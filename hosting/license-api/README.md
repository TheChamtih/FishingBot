# License API (DB-backed)

Base URL on server:
- `http://kaufmanma3.temp.swtest.ru/license-api`

> Note: HTTPS currently redirects with `302` to HTTP. For POST requests, use direct HTTP to avoid payload loss on redirect.

## Routes

### Admin panel (web)
- `GET /admin_panel.php` (login by admin key from `config.php`)

### Public
- `GET /health`
- `POST /license/activate`
- `POST /license/check`

### Admin (requires `X-Admin-Key` or `Authorization: Bearer <key>`)
- `POST /admin/license/create`
- `POST /admin/license/extend`
- `POST /admin/license/revoke`
- `POST /admin/license/reset-device`

## Request payloads

### POST /license/activate
```json
{
  "license_key": "AAAAA-BBBBB-CCCCC-DDDDD",
  "device_id": "DEVICE-UNIQUE-ID"
}
```

### POST /license/check
```json
{
  "license_key": "AAAAA-BBBBB-CCCCC-DDDDD",
  "device_id": "DEVICE-UNIQUE-ID"
}
```

### POST /admin/license/create
```json
{
  "days": 30,
  "user_label": "username-or-email",
  "notes": "optional"
}
```

Alternative create with explicit expiry:
```json
{
  "expires_at": "2026-12-31 23:59:59",
  "user_label": "username-or-email"
}
```

### POST /admin/license/extend
```json
{
  "license_key": "AAAAA-BBBBB-CCCCC-DDDDD",
  "days": 30
}
```

### POST /admin/license/revoke
```json
{
  "license_key": "AAAAA-BBBBB-CCCCC-DDDDD"
}
```

### POST /admin/license/reset-device
```json
{
  "license_key": "AAAAA-BBBBB-CCCCC-DDDDD"
}
```

## DB settings

Configure in `config.php`:
- SQLite (default): set `db.driver = sqlite` and `db.sqlite_path`
- MySQL: set `db.driver = mysql` and fill `db.mysql.*`

Schema is auto-created on first request.

## Important deployment note

`config.php` in this workspace contains a placeholder `admin_key`.
If you redeploy all files, you may overwrite the production key.
Either:
- update `config.php` with your real key before deploy, or
- deploy `index.php` only and keep server `config.php` unchanged.
