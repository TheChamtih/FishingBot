<?php

declare(strict_types=1);

/**
 * Minimal license API for shared hosting.
 * Supports PDO drivers: sqlite (default) and mysql.
 */

$config = require __DIR__ . '/config.php';

$allowOrigin = (string)($config['allow_origin'] ?? '*');
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: ' . $allowOrigin);
header('Access-Control-Allow-Headers: Content-Type, Authorization, X-Admin-Key');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');

if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') {
    http_response_code(204);
    exit;
}

try {
    $pdo = createPdo($config);
    ensureSchema($pdo, $config);

    $path = normalizePath();
    $method = strtoupper((string)($_SERVER['REQUEST_METHOD'] ?? 'GET'));

    if ($method === 'GET' && $path === '/health') {
        jsonResponse(200, [
            'ok' => true,
            'server_time' => nowUtc(),
            'driver' => getDriver($config),
        ]);
    }

    if ($method === 'POST' && $path === '/license/activate') {
        $payload = readJsonBody();
        $licenseKey = normalizeLicenseKey((string)($payload['license_key'] ?? ''));
        $deviceId = normalizeDeviceId((string)($payload['device_id'] ?? ''));

        if ($licenseKey === '' || $deviceId === '') {
            jsonResponse(400, [
                'ok' => false,
                'error' => 'license_key_and_device_id_required',
            ]);
        }

        $license = findLicense($pdo, $licenseKey);
        if ($license === null) {
            jsonResponse(404, [
                'ok' => false,
                'error' => 'license_not_found',
            ]);
        }

        $license = refreshExpiredStatus($pdo, $license);
        if ((string)$license['status'] !== 'active') {
            jsonResponse(403, [
                'ok' => false,
                'error' => 'license_not_active',
                'status' => (string)$license['status'],
                'expires_at' => (string)$license['expires_at'],
                'server_time' => nowUtc(),
            ]);
        }

        if ((string)$license['device_id'] === '') {
            bindDevice($pdo, $licenseKey, $deviceId);
            $license = findLicense($pdo, $licenseKey);
            if ($license === null) {
                throw new RuntimeException('license_reload_failed');
            }
        } elseif (!hashEquals((string)$license['device_id'], $deviceId)) {
            jsonResponse(403, [
                'ok' => false,
                'error' => 'device_mismatch',
                'status' => (string)$license['status'],
                'expires_at' => (string)$license['expires_at'],
                'server_time' => nowUtc(),
            ]);
        }

        markLastCheck($pdo, $licenseKey);
        jsonResponse(200, [
            'ok' => true,
            'status' => (string)$license['status'],
            'expires_at' => (string)$license['expires_at'],
            'server_time' => nowUtc(),
        ]);
    }

    if ($method === 'POST' && $path === '/license/check') {
        $payload = readJsonBody();
        $licenseKey = normalizeLicenseKey((string)($payload['license_key'] ?? ''));
        $deviceId = normalizeDeviceId((string)($payload['device_id'] ?? ''));

        if ($licenseKey === '' || $deviceId === '') {
            jsonResponse(400, [
                'ok' => false,
                'error' => 'license_key_and_device_id_required',
            ]);
        }

        $license = findLicense($pdo, $licenseKey);
        if ($license === null) {
            jsonResponse(404, [
                'ok' => false,
                'error' => 'license_not_found',
            ]);
        }

        $license = refreshExpiredStatus($pdo, $license);

        $deviceOk = ((string)$license['device_id'] !== '' && hashEquals((string)$license['device_id'], $deviceId));
        $active = ((string)$license['status'] === 'active') && $deviceOk;

        markLastCheck($pdo, $licenseKey);

        jsonResponse(200, [
            'ok' => $active,
            'status' => (string)$license['status'],
            'device_ok' => $deviceOk,
            'expires_at' => (string)$license['expires_at'],
            'server_time' => nowUtc(),
        ]);
    }

    if ($method === 'POST' && $path === '/admin/license/create') {
        requireAdmin($config);
        $payload = readJsonBody();

        $days = parsePositiveInt($payload['days'] ?? null);
        $explicitExpiresAt = normalizeDateTime((string)($payload['expires_at'] ?? ''));
        if ($days === null && $explicitExpiresAt === null) {
            jsonResponse(400, [
                'ok' => false,
                'error' => 'days_or_expires_at_required',
            ]);
        }

        $expiresAt = $explicitExpiresAt ?? futureUtcFromDays($days ?? 0);
        $licenseKey = generateLicenseKey();
        $userLabel = trim((string)($payload['user_label'] ?? ''));
        $notes = trim((string)($payload['notes'] ?? ''));

        $stmt = $pdo->prepare(
            'INSERT INTO licenses (license_key, user_label, status, expires_at, created_at, updated_at, notes) VALUES (:license_key, :user_label, :status, :expires_at, :created_at, :updated_at, :notes)'
        );
        $now = nowUtc();
        $stmt->execute([
            ':license_key' => $licenseKey,
            ':user_label' => $userLabel,
            ':status' => 'active',
            ':expires_at' => $expiresAt,
            ':created_at' => $now,
            ':updated_at' => $now,
            ':notes' => $notes,
        ]);

        jsonResponse(200, [
            'ok' => true,
            'license_key' => $licenseKey,
            'expires_at' => $expiresAt,
            'server_time' => $now,
        ]);
    }

    if ($method === 'POST' && $path === '/admin/license/extend') {
        requireAdmin($config);
        $payload = readJsonBody();

        $licenseKey = normalizeLicenseKey((string)($payload['license_key'] ?? ''));
        if ($licenseKey === '') {
            jsonResponse(400, [
                'ok' => false,
                'error' => 'license_key_required',
            ]);
        }

        $license = findLicense($pdo, $licenseKey);
        if ($license === null) {
            jsonResponse(404, [
                'ok' => false,
                'error' => 'license_not_found',
            ]);
        }

        $days = parsePositiveInt($payload['days'] ?? null);
        $explicitExpiresAt = normalizeDateTime((string)($payload['expires_at'] ?? ''));
        if ($days === null && $explicitExpiresAt === null) {
            jsonResponse(400, [
                'ok' => false,
                'error' => 'days_or_expires_at_required',
            ]);
        }

        $newExpiresAt = $explicitExpiresAt;
        if ($newExpiresAt === null) {
            $base = (string)$license['expires_at'];
            $baseTs = strtotime($base . ' UTC');
            $nowTs = strtotime(nowUtc() . ' UTC');
            $fromTs = max($baseTs ?: 0, $nowTs ?: 0);
            $newExpiresAt = gmdate('Y-m-d H:i:s', $fromTs + (($days ?? 0) * 86400));
        }

        $status = (string)$license['status'] === 'revoked' ? 'revoked' : 'active';
        $stmt = $pdo->prepare('UPDATE licenses SET expires_at = :expires_at, status = :status, updated_at = :updated_at WHERE license_key = :license_key');
        $stmt->execute([
            ':expires_at' => $newExpiresAt,
            ':status' => $status,
            ':updated_at' => nowUtc(),
            ':license_key' => $licenseKey,
        ]);

        jsonResponse(200, [
            'ok' => true,
            'license_key' => $licenseKey,
            'expires_at' => $newExpiresAt,
            'status' => $status,
            'server_time' => nowUtc(),
        ]);
    }

    if ($method === 'POST' && $path === '/admin/license/revoke') {
        requireAdmin($config);
        $payload = readJsonBody();

        $licenseKey = normalizeLicenseKey((string)($payload['license_key'] ?? ''));
        if ($licenseKey === '') {
            jsonResponse(400, [
                'ok' => false,
                'error' => 'license_key_required',
            ]);
        }

        $license = findLicense($pdo, $licenseKey);
        if ($license === null) {
            jsonResponse(404, [
                'ok' => false,
                'error' => 'license_not_found',
            ]);
        }

        $stmt = $pdo->prepare('UPDATE licenses SET status = :status, updated_at = :updated_at WHERE license_key = :license_key');
        $stmt->execute([
            ':status' => 'revoked',
            ':updated_at' => nowUtc(),
            ':license_key' => $licenseKey,
        ]);

        jsonResponse(200, [
            'ok' => true,
            'license_key' => $licenseKey,
            'status' => 'revoked',
            'server_time' => nowUtc(),
        ]);
    }

    if ($method === 'POST' && $path === '/admin/license/reset-device') {
        requireAdmin($config);
        $payload = readJsonBody();

        $licenseKey = normalizeLicenseKey((string)($payload['license_key'] ?? ''));
        if ($licenseKey === '') {
            jsonResponse(400, [
                'ok' => false,
                'error' => 'license_key_required',
            ]);
        }

        $license = findLicense($pdo, $licenseKey);
        if ($license === null) {
            jsonResponse(404, [
                'ok' => false,
                'error' => 'license_not_found',
            ]);
        }

        $stmt = $pdo->prepare('UPDATE licenses SET device_id = :device_id, device_bound_at = :device_bound_at, updated_at = :updated_at WHERE license_key = :license_key');
        $stmt->execute([
            ':device_id' => '',
            ':device_bound_at' => null,
            ':updated_at' => nowUtc(),
            ':license_key' => $licenseKey,
        ]);

        jsonResponse(200, [
            'ok' => true,
            'license_key' => $licenseKey,
            'server_time' => nowUtc(),
        ]);
    }

    jsonResponse(404, [
        'ok' => false,
        'error' => 'route_not_found',
        'path' => $path,
        'method' => $method,
    ]);
} catch (Throwable $e) {
    jsonResponse(500, [
        'ok' => false,
        'error' => 'internal_error',
        'message' => $e->getMessage(),
    ]);
}

function getDriver(array $config): string
{
    $driver = strtolower(trim((string)($config['db']['driver'] ?? 'sqlite')));
    return $driver === 'mysql' ? 'mysql' : 'sqlite';
}

function createPdo(array $config): PDO
{
    $driver = getDriver($config);

    if ($driver === 'mysql') {
        $mysql = $config['db']['mysql'] ?? [];
        $host = (string)($mysql['host'] ?? '127.0.0.1');
        $port = (int)($mysql['port'] ?? 3306);
        $db = (string)($mysql['database'] ?? '');
        $user = (string)($mysql['user'] ?? '');
        $pass = (string)($mysql['password'] ?? '');
        $charset = (string)($mysql['charset'] ?? 'utf8mb4');

        if ($db === '' || $user === '') {
            throw new RuntimeException('mysql_config_incomplete');
        }

        $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=%s', $host, $port, $db, $charset);
        return new PDO($dsn, $user, $pass, [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES => false,
        ]);
    }

    $sqlitePath = (string)($config['db']['sqlite_path'] ?? (__DIR__ . '/storage/licenses.sqlite'));
    $sqliteDir = dirname($sqlitePath);
    if (!is_dir($sqliteDir)) {
        if (!mkdir($sqliteDir, 0775, true) && !is_dir($sqliteDir)) {
            throw new RuntimeException('sqlite_dir_create_failed');
        }
    }

    $pdo = new PDO('sqlite:' . $sqlitePath, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
    $pdo->exec('PRAGMA foreign_keys = ON');
    return $pdo;
}

function ensureSchema(PDO $pdo, array $config): void
{
    $driver = getDriver($config);

    if ($driver === 'mysql') {
        $pdo->exec(
            'CREATE TABLE IF NOT EXISTS licenses (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                license_key VARCHAR(128) NOT NULL UNIQUE,
                user_label VARCHAR(191) NOT NULL DEFAULT \'\',
                status VARCHAR(32) NOT NULL DEFAULT \'active\',
                device_id VARCHAR(191) NOT NULL DEFAULT \'\',
                device_bound_at DATETIME NULL,
                expires_at DATETIME NOT NULL,
                last_check_at DATETIME NULL,
                notes TEXT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                INDEX idx_expires_at (expires_at),
                INDEX idx_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
        );
        return;
    }

    $pdo->exec(
        'CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT NOT NULL UNIQUE,
            user_label TEXT NOT NULL DEFAULT \'\',
            status TEXT NOT NULL DEFAULT \'active\',
            device_id TEXT NOT NULL DEFAULT \'\',
            device_bound_at TEXT NULL,
            expires_at TEXT NOT NULL,
            last_check_at TEXT NULL,
            notes TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )'
    );
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_licenses_expires_at ON licenses (expires_at)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses (status)');
}

function normalizePath(): string
{
    $rawPath = (string)(parse_url((string)($_SERVER['REQUEST_URI'] ?? '/'), PHP_URL_PATH) ?? '/');
    $scriptDir = (string)dirname((string)($_SERVER['SCRIPT_NAME'] ?? '/index.php'));
    $scriptDir = str_replace('\\', '/', $scriptDir);
    if ($scriptDir !== '/' && $scriptDir !== '.' && $scriptDir !== '') {
        if (substr($rawPath, 0, strlen($scriptDir)) === $scriptDir) {
            $rawPath = (string)substr($rawPath, strlen($scriptDir));
        }
    }
    $rawPath = '/' . ltrim($rawPath, '/');
    return $rawPath === '' ? '/' : $rawPath;
}

function readJsonBody(): array
{
    $raw = (string)file_get_contents('php://input');
    if ($raw === '') {
        return [];
    }
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        throw new RuntimeException('invalid_json_payload');
    }
    return $data;
}

function requireAdmin(array $config): void
{
    $expected = trim((string)($config['admin_key'] ?? ''));
    if ($expected === '' || $expected === 'CHANGE_ME_TO_LONG_RANDOM_SECRET') {
        throw new RuntimeException('admin_key_not_configured');
    }

    $provided = trim((string)($_SERVER['HTTP_X_ADMIN_KEY'] ?? ''));
    if ($provided === '') {
        $auth = trim((string)($_SERVER['HTTP_AUTHORIZATION'] ?? ''));
        if (stripos($auth, 'Bearer ') === 0) {
            $provided = trim((string)substr($auth, 7));
        }
    }

    if ($provided === '' || !hashEquals($expected, $provided)) {
        jsonResponse(401, [
            'ok' => false,
            'error' => 'admin_unauthorized',
        ]);
    }
}

function findLicense(PDO $pdo, string $licenseKey): ?array
{
    $stmt = $pdo->prepare('SELECT * FROM licenses WHERE license_key = :license_key LIMIT 1');
    $stmt->execute([':license_key' => $licenseKey]);
    $row = $stmt->fetch();
    return is_array($row) ? $row : null;
}

function refreshExpiredStatus(PDO $pdo, array $license): array
{
    if ((string)$license['status'] === 'active') {
        $expires = strtotime((string)$license['expires_at'] . ' UTC');
        $now = strtotime(nowUtc() . ' UTC');
        if ($expires !== false && $now !== false && $now > $expires) {
            $stmt = $pdo->prepare('UPDATE licenses SET status = :status, updated_at = :updated_at WHERE license_key = :license_key');
            $stmt->execute([
                ':status' => 'expired',
                ':updated_at' => nowUtc(),
                ':license_key' => (string)$license['license_key'],
            ]);
            $license['status'] = 'expired';
        }
    }
    return $license;
}

function bindDevice(PDO $pdo, string $licenseKey, string $deviceId): void
{
    $stmt = $pdo->prepare('UPDATE licenses SET device_id = :device_id, device_bound_at = :device_bound_at, updated_at = :updated_at WHERE license_key = :license_key');
    $stmt->execute([
        ':device_id' => $deviceId,
        ':device_bound_at' => nowUtc(),
        ':updated_at' => nowUtc(),
        ':license_key' => $licenseKey,
    ]);
}

function markLastCheck(PDO $pdo, string $licenseKey): void
{
    $stmt = $pdo->prepare('UPDATE licenses SET last_check_at = :last_check_at, updated_at = :updated_at WHERE license_key = :license_key');
    $stmt->execute([
        ':last_check_at' => nowUtc(),
        ':updated_at' => nowUtc(),
        ':license_key' => $licenseKey,
    ]);
}

function normalizeLicenseKey(string $licenseKey): string
{
    $clean = strtoupper(trim($licenseKey));
    $clean = preg_replace('/[^A-Z0-9\-]/', '', $clean) ?? '';
    return trim($clean);
}

function normalizeDeviceId(string $deviceId): string
{
    $clean = trim($deviceId);
    if ($clean === '') {
        return '';
    }
    if (strlen($clean) > 191) {
        $clean = substr($clean, 0, 191);
    }
    return $clean;
}

function parsePositiveInt($value): ?int
{
    if ($value === null || $value === '') {
        return null;
    }
    if (is_numeric($value)) {
        $v = (int)$value;
        return $v > 0 ? $v : null;
    }
    return null;
}

function normalizeDateTime(string $value): ?string
{
    $clean = trim($value);
    if ($clean === '') {
        return null;
    }
    $ts = strtotime($clean . ' UTC');
    if ($ts === false) {
        return null;
    }
    return gmdate('Y-m-d H:i:s', $ts);
}

function futureUtcFromDays(int $days): string
{
    $days = max(1, $days);
    return gmdate('Y-m-d H:i:s', time() + ($days * 86400));
}

function generateLicenseKey(): string
{
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    $parts = [];
    for ($i = 0; $i < 4; $i++) {
        $part = '';
        for ($j = 0; $j < 5; $j++) {
            $part .= $chars[random_int(0, strlen($chars) - 1)];
        }
        $parts[] = $part;
    }
    return implode('-', $parts);
}

function nowUtc(): string
{
    return gmdate('Y-m-d H:i:s');
}

function hashEquals(string $a, string $b): bool
{
    if (function_exists('hash_equals')) {
        return hash_equals($a, $b);
    }
    return $a === $b;
}

function jsonResponse(int $status, array $payload): void
{
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}
