<?php

declare(strict_types=1);

$config = require __DIR__ . '/config.php';
$adminKey = trim((string)($config['admin_key'] ?? ''));
$adminKeyConfigured = $adminKey !== '' && $adminKey !== 'CHANGE_ME_TO_LONG_RANDOM_SECRET';

$secureCookie = !empty($_SERVER['HTTPS']) && strtolower((string)$_SERVER['HTTPS']) !== 'off';
if (PHP_VERSION_ID >= 70300) {
    session_set_cookie_params([
        'lifetime' => 0,
        'path' => '/',
        'secure' => $secureCookie,
        'httponly' => true,
        'samesite' => 'Lax',
    ]);
} else {
    session_set_cookie_params(0, '/; samesite=Lax', '', $secureCookie, true);
}
session_start();

header('Content-Type: text/html; charset=utf-8');

if (empty($_SESSION['license_admin_csrf'])) {
    $_SESSION['license_admin_csrf'] = bin2hex(random_bytes(16));
}
$csrfToken = (string)$_SESSION['license_admin_csrf'];

$fatalError = '';
$error = '';
$success = '';
$generatedLicenseKey = '';
$generatedExpiresAt = '';
$listSearch = panelNormalizeLicenseKey((string)($_GET['q'] ?? ''));
$listStatus = panelNormalizeStatus((string)($_GET['status'] ?? ''));
$listExpiresFromRaw = panelNormalizeDateInput((string)($_GET['expires_from'] ?? ''));
$listExpiresToRaw = panelNormalizeDateInput((string)($_GET['expires_to'] ?? ''));
$listExpiresFrom = panelDateStartUtc($listExpiresFromRaw);
$listExpiresTo = panelDateEndUtc($listExpiresToRaw);
$listPerPage = panelParsePerPage($_GET['per_page'] ?? null);
$listPage = panelParsePage($_GET['page'] ?? null);
$listTotal = 0;
$listPages = 1;
$listFrom = 0;
$listTo = 0;
$listQueryUrl = panelBuildListUrl($listPage, $listPerPage, $listSearch, $listStatus, $listExpiresFromRaw, $listExpiresToRaw);

$flash = $_SESSION['license_admin_flash'] ?? null;
if (isset($_SESSION['license_admin_flash'])) {
    unset($_SESSION['license_admin_flash']);
}
if (is_array($flash)) {
    $error = trim((string)($flash['error'] ?? ''));
    $success = trim((string)($flash['success'] ?? ''));
    $generatedLicenseKey = trim((string)($flash['generated_license_key'] ?? ''));
    $generatedExpiresAt = trim((string)($flash['generated_expires_at'] ?? ''));
}

if (isset($_GET['logout'])) {
    $_SESSION = [];
    if (ini_get('session.use_cookies')) {
        $params = session_get_cookie_params();
        setcookie(session_name(), '', time() - 42000, $params['path'], $params['domain'], (bool)$params['secure'], (bool)$params['httponly']);
    }
    session_destroy();
    header('Location: admin_panel.php');
    exit;
}

$isAuthenticated = !empty($_SESSION['license_admin_auth']) && $_SESSION['license_admin_auth'] === true;

try {
    $pdo = panelCreatePdo($config);
    panelEnsureSchema($pdo, $config);
} catch (Throwable $e) {
    $dbError = trim((string)$e->getMessage());
    if ($dbError === 'mysql_config_incomplete') {
        $dbError = 'не заполнены параметры MySQL в config.php';
    } elseif ($dbError === 'sqlite_dir_create_failed') {
        $dbError = 'не удалось создать директорию для SQLite-базы';
    }
    $fatalError = 'Ошибка БД: ' . $dbError;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && $fatalError === '') {
    $action = trim((string)($_POST['panel_action'] ?? ''));

    if ($action === 'login') {
        if (!$adminKeyConfigured) {
            $error = 'Ключ администратора не настроен в config.php';
        } else {
            $provided = trim((string)($_POST['admin_key'] ?? ''));
            if ($provided !== '' && panelHashEquals($adminKey, $provided)) {
                session_regenerate_id(true);
                $_SESSION['license_admin_auth'] = true;
                $_SESSION['license_admin_login_at'] = time();
                $isAuthenticated = true;
                $_SESSION['license_admin_flash'] = [
                    'success' => 'Вход выполнен успешно.',
                ];
                header('Location: ' . $listQueryUrl);
                exit;
            } else {
                $error = 'Неверный ключ администратора.';
            }
        }
    } elseif (!$isAuthenticated) {
        $error = 'Требуется авторизация.';
    } else {
        $flashError = '';
        $flashSuccess = '';
        $flashGeneratedLicenseKey = '';
        $flashGeneratedExpiresAt = '';

        $postedCsrf = trim((string)($_POST['csrf_token'] ?? ''));
        if ($postedCsrf === '' || !panelHashEquals($csrfToken, $postedCsrf)) {
            $flashError = 'Некорректный CSRF-токен.';
        } else {
            if ($action === 'create') {
                $days = panelParsePositiveInt($_POST['days'] ?? null);
                $explicitExpiresAt = panelNormalizeDateTime((string)($_POST['expires_at'] ?? ''));
                if ($days === null && $explicitExpiresAt === null) {
                    $flashError = 'Укажи количество дней или дату истечения.';
                } else {
                    $expiresAt = $explicitExpiresAt ?? panelFutureUtcFromDays($days ?? 0);
                    $licenseKey = panelGenerateLicenseKey();
                    $userLabel = trim((string)($_POST['user_label'] ?? ''));
                    $notes = trim((string)($_POST['notes'] ?? ''));
                    $now = panelNowUtc();

                    $stmt = $pdo->prepare(
                        'INSERT INTO licenses (license_key, user_label, status, expires_at, created_at, updated_at, notes) VALUES (:license_key, :user_label, :status, :expires_at, :created_at, :updated_at, :notes)'
                    );
                    $stmt->execute([
                        ':license_key' => $licenseKey,
                        ':user_label' => $userLabel,
                        ':status' => 'active',
                        ':expires_at' => $expiresAt,
                        ':created_at' => $now,
                        ':updated_at' => $now,
                        ':notes' => $notes,
                    ]);

                    $flashGeneratedLicenseKey = $licenseKey;
                    $flashGeneratedExpiresAt = $expiresAt;
                    $flashSuccess = 'Лицензия успешно создана.';
                }
            } elseif ($action === 'extend') {
                $licenseKey = panelNormalizeLicenseKey((string)($_POST['license_key'] ?? ''));
                if ($licenseKey === '') {
                    $flashError = 'Поле "Ключ лицензии" обязательно.';
                } else {
                    $license = panelFindLicense($pdo, $licenseKey);
                    if ($license === null) {
                        $flashError = 'Лицензия не найдена.';
                    } else {
                        $days = panelParsePositiveInt($_POST['days'] ?? null);
                        $explicitExpiresAt = panelNormalizeDateTime((string)($_POST['expires_at'] ?? ''));
                        if ($days === null && $explicitExpiresAt === null) {
                            $flashError = 'Укажи количество дней или дату истечения.';
                        } else {
                            $newExpiresAt = $explicitExpiresAt;
                            if ($newExpiresAt === null) {
                                $baseTs = strtotime((string)$license['expires_at'] . ' UTC');
                                $nowTs = strtotime(panelNowUtc() . ' UTC');
                                $fromTs = max($baseTs ?: 0, $nowTs ?: 0);
                                $newExpiresAt = gmdate('Y-m-d H:i:s', $fromTs + (($days ?? 0) * 86400));
                            }

                            $status = (string)$license['status'] === 'revoked' ? 'revoked' : 'active';
                            $stmt = $pdo->prepare('UPDATE licenses SET expires_at = :expires_at, status = :status, updated_at = :updated_at WHERE license_key = :license_key');
                            $stmt->execute([
                                ':expires_at' => $newExpiresAt,
                                ':status' => $status,
                                ':updated_at' => panelNowUtc(),
                                ':license_key' => $licenseKey,
                            ]);
                            $flashSuccess = 'Срок лицензии продлен.';
                        }
                    }
                }
            } elseif ($action === 'revoke') {
                $licenseKey = panelNormalizeLicenseKey((string)($_POST['license_key'] ?? ''));
                if ($licenseKey === '') {
                    $flashError = 'Поле "Ключ лицензии" обязательно.';
                } else {
                    $license = panelFindLicense($pdo, $licenseKey);
                    if ($license === null) {
                        $flashError = 'Лицензия не найдена.';
                    } else {
                        $stmt = $pdo->prepare('UPDATE licenses SET status = :status, updated_at = :updated_at WHERE license_key = :license_key');
                        $stmt->execute([
                            ':status' => 'revoked',
                            ':updated_at' => panelNowUtc(),
                            ':license_key' => $licenseKey,
                        ]);
                        $flashSuccess = 'Лицензия отозвана.';
                    }
                }
            } elseif ($action === 'reset-device') {
                $licenseKey = panelNormalizeLicenseKey((string)($_POST['license_key'] ?? ''));
                if ($licenseKey === '') {
                    $flashError = 'Поле "Ключ лицензии" обязательно.';
                } else {
                    $license = panelFindLicense($pdo, $licenseKey);
                    if ($license === null) {
                        $flashError = 'Лицензия не найдена.';
                    } else {
                        $stmt = $pdo->prepare('UPDATE licenses SET device_id = :device_id, device_bound_at = :device_bound_at, updated_at = :updated_at WHERE license_key = :license_key');
                        $stmt->execute([
                            ':device_id' => '',
                            ':device_bound_at' => null,
                            ':updated_at' => panelNowUtc(),
                            ':license_key' => $licenseKey,
                        ]);
                        $flashSuccess = 'Привязка к устройству сброшена.';
                    }
                }
            } else {
                $flashError = 'Неизвестное действие.';
            }
        }

        $_SESSION['license_admin_flash'] = [
            'error' => $flashError,
            'success' => $flashSuccess,
            'generated_license_key' => $flashGeneratedLicenseKey,
            'generated_expires_at' => $flashGeneratedExpiresAt,
        ];
        header('Location: ' . $listQueryUrl);
        exit;
    }
}

$licenses = [];
$stats = [
    'total' => 0,
    'active' => 0,
    'expired' => 0,
    'revoked' => 0,
];

if ($fatalError === '' && $isAuthenticated) {
    try {
        panelRefreshExpiredStatuses($pdo);

        $rows = $pdo->query('SELECT status, COUNT(*) AS cnt FROM licenses GROUP BY status')->fetchAll();
        foreach ($rows as $row) {
            $status = strtolower((string)($row['status'] ?? ''));
            $cnt = (int)($row['cnt'] ?? 0);
            $stats['total'] += $cnt;
            if (isset($stats[$status])) {
                $stats[$status] = $cnt;
            }
        }

        $whereParts = [];
        $params = [];
        if ($listSearch !== '') {
            $whereParts[] = 'license_key LIKE :search';
            $params[':search'] = '%' . $listSearch . '%';
        }
        if ($listStatus !== '') {
            $whereParts[] = 'status = :status';
            $params[':status'] = $listStatus;
        }
        if ($listExpiresFrom !== null) {
            $whereParts[] = 'expires_at >= :expires_from';
            $params[':expires_from'] = $listExpiresFrom;
        }
        if ($listExpiresTo !== null) {
            $whereParts[] = 'expires_at <= :expires_to';
            $params[':expires_to'] = $listExpiresTo;
        }
        $whereSql = $whereParts ? (' WHERE ' . implode(' AND ', $whereParts)) : '';

        $countStmt = $pdo->prepare('SELECT COUNT(*) AS cnt FROM licenses' . $whereSql);
        $countStmt->execute($params);
        $listTotal = (int)($countStmt->fetchColumn() ?: 0);

        $listPages = max(1, (int)ceil($listTotal / $listPerPage));
        if ($listPage > $listPages) {
            $listPage = $listPages;
        }
        $offset = ($listPage - 1) * $listPerPage;

        $sql = 'SELECT license_key, user_label, status, device_id, expires_at, last_check_at, updated_at, notes FROM licenses'
            . $whereSql
            . ' ORDER BY updated_at DESC LIMIT ' . (int)$listPerPage . ' OFFSET ' . (int)$offset;
        $stmt = $pdo->prepare($sql);
        $stmt->execute($params);
        $licenses = $stmt->fetchAll();

        if ($listTotal > 0) {
            $listFrom = $offset + 1;
            $listTo = min($listTotal, $offset + count($licenses));
        }
    } catch (Throwable $e) {
        $error = 'Ошибка загрузки данных: ' . $e->getMessage();
    }
}

function panelE($value): string
{
    return htmlspecialchars((string)$value, ENT_QUOTES, 'UTF-8');
}

function panelOld(string $name): string
{
    return isset($_POST[$name]) ? trim((string)$_POST[$name]) : '';
}

function panelShortText(string $value, int $max = 18): string
{
    $text = trim($value);
    if ($text === '') {
        return '-';
    }
    if (strlen($text) <= $max) {
        return $text;
    }
    return substr($text, 0, $max - 3) . '...';
}

function panelStatusClass(string $status): string
{
    $s = strtolower(trim($status));
    if ($s === 'active') {
        return 'status-active';
    }
    if ($s === 'revoked') {
        return 'status-revoked';
    }
    if ($s === 'expired') {
        return 'status-expired';
    }
    return 'status-other';
}

function panelStatusLabel(string $status): string
{
    $s = strtolower(trim($status));
    if ($s === 'active') {
        return 'активна';
    }
    if ($s === 'revoked') {
        return 'отозвана';
    }
    if ($s === 'expired') {
        return 'истекла';
    }
    return 'неизвестно';
}

function panelNormalizeStatus(string $status): string
{
    $value = strtolower(trim($status));
    if (in_array($value, ['active', 'expired', 'revoked'], true)) {
        return $value;
    }
    return '';
}

function panelNormalizeDateInput(string $value): string
{
    $clean = trim($value);
    if ($clean === '' || !preg_match('/^\d{4}-\d{2}-\d{2}$/', $clean)) {
        return '';
    }
    return $clean;
}

function panelDateStartUtc(string $date): ?string
{
    if ($date === '') {
        return null;
    }
    $ts = strtotime($date . ' 00:00:00 UTC');
    if ($ts === false) {
        return null;
    }
    return gmdate('Y-m-d H:i:s', $ts);
}

function panelDateEndUtc(string $date): ?string
{
    if ($date === '') {
        return null;
    }
    $ts = strtotime($date . ' 23:59:59 UTC');
    if ($ts === false) {
        return null;
    }
    return gmdate('Y-m-d H:i:s', $ts);
}

function panelRefreshExpiredStatuses(PDO $pdo): void
{
    $now = panelNowUtc();
    $stmt = $pdo->prepare('UPDATE licenses SET status = :expired, updated_at = :updated_at WHERE status = :active AND expires_at < :now');
    $stmt->execute([
        ':expired' => 'expired',
        ':updated_at' => $now,
        ':active' => 'active',
        ':now' => $now,
    ]);
}

function panelGetDriver(array $config): string
{
    $driver = strtolower(trim((string)($config['db']['driver'] ?? 'sqlite')));
    return $driver === 'mysql' ? 'mysql' : 'sqlite';
}

function panelCreatePdo(array $config): PDO
{
    $driver = panelGetDriver($config);

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

function panelEnsureSchema(PDO $pdo, array $config): void
{
    if (panelGetDriver($config) === 'mysql') {
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

function panelNormalizeLicenseKey(string $licenseKey): string
{
    $clean = strtoupper(trim($licenseKey));
    $clean = preg_replace('/[^A-Z0-9\-]/', '', $clean) ?? '';
    return trim($clean);
}

function panelParsePositiveInt($value): ?int
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

function panelParsePage($value): int
{
    if ($value === null || $value === '') {
        return 1;
    }
    $page = (int)$value;
    return $page > 0 ? $page : 1;
}

function panelParsePerPage($value): int
{
    $allowed = [25, 50, 100, 200];
    if ($value === null || $value === '') {
        return 25;
    }
    $perPage = (int)$value;
    return in_array($perPage, $allowed, true) ? $perPage : 25;
}

function panelBuildListUrl(
    int $page,
    int $perPage,
    string $search,
    string $status = '',
    string $expiresFrom = '',
    string $expiresTo = ''
): string
{
    $query = [
        'page' => max(1, $page),
        'per_page' => panelParsePerPage($perPage),
    ];
    if ($search !== '') {
        $query['q'] = $search;
    }
    $statusFilter = panelNormalizeStatus($status);
    if ($statusFilter !== '') {
        $query['status'] = $statusFilter;
    }

    $from = panelNormalizeDateInput($expiresFrom);
    $to = panelNormalizeDateInput($expiresTo);
    if ($from !== '') {
        $query['expires_from'] = $from;
    }
    if ($to !== '') {
        $query['expires_to'] = $to;
    }

    return 'admin_panel.php?' . http_build_query($query);
}

function panelNormalizeDateTime(string $value): ?string
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

function panelFutureUtcFromDays(int $days): string
{
    $days = max(1, $days);
    return gmdate('Y-m-d H:i:s', time() + ($days * 86400));
}

function panelGenerateLicenseKey(): string
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

function panelNowUtc(): string
{
    return gmdate('Y-m-d H:i:s');
}

function panelHashEquals(string $a, string $b): bool
{
    if (function_exists('hash_equals')) {
        return hash_equals($a, $b);
    }
    return $a === $b;
}

function panelFindLicense(PDO $pdo, string $licenseKey): ?array
{
    $stmt = $pdo->prepare('SELECT * FROM licenses WHERE license_key = :license_key LIMIT 1');
    $stmt->execute([':license_key' => $licenseKey]);
    $row = $stmt->fetch();
    return is_array($row) ? $row : null;
}

?><!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Панель управления лицензиями</title>
    <style>
        :root {
            --bg: #0b1220;
            --panel: #111a2b;
            --muted: #9eb3d1;
            --text: #e9f2ff;
            --line: #2b3f62;
            --accent: #2ec4b6;
            --danger: #f87171;
            --warn: #fbbf24;
            --ok: #34d399;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            background: radial-gradient(circle at top, #13213a 0%, #0b1220 60%);
            color: var(--text);
            font-family: Segoe UI, Arial, sans-serif;
            min-height: 100vh;
        }
        .container {
            max-width: 1180px;
            margin: 0 auto;
            padding: 22px;
        }
        .panel {
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 14px;
        }
        h1, h2, h3 { margin: 0 0 12px 0; }
        h1 { font-size: 22px; }
        h2 { font-size: 17px; color: #d8e7ff; }
        .muted { color: var(--muted); }
        .row {
            display: grid;
            grid-template-columns: repeat(12, minmax(0, 1fr));
            gap: 10px;
        }
        .col-3 { grid-column: span 3; }
        .col-4 { grid-column: span 4; }
        .col-6 { grid-column: span 6; }
        .col-8 { grid-column: span 8; }
        .col-12 { grid-column: span 12; }
        @media (max-width: 980px) {
            .col-3, .col-4, .col-6, .col-8, .col-12 { grid-column: span 12; }
        }
        label {
            display: block;
            color: var(--muted);
            font-size: 12px;
            margin-bottom: 4px;
        }
        input, textarea, select, button {
            width: 100%;
            border-radius: 10px;
            border: 1px solid var(--line);
            background: #0a1424;
            color: var(--text);
            padding: 10px 12px;
            font-size: 13px;
        }
        textarea { min-height: 72px; resize: vertical; }
        select { cursor: pointer; }
        button {
            background: #183156;
            border-color: #2d4f7f;
            cursor: pointer;
            font-weight: 600;
            text-align: center;
            transition: background 0.15s ease;
        }
        button:hover { background: #224272; }
        .btn-link {
            display: block;
            width: 100%;
            border-radius: 10px;
            border: 1px solid #2d4f7f;
            background: #183156;
            color: var(--text);
            padding: 10px 12px;
            font-size: 13px;
            font-weight: 600;
            text-decoration: none;
            text-align: center;
            transition: background 0.15s ease;
        }
        .btn-link:hover { background: #224272; }
        .btn-primary { background: #0f766e; border-color: #14b8a6; }
        .btn-primary:hover { background: #11837b; }
        .btn-danger { background: #7f1d1d; border-color: #dc2626; }
        .btn-danger:hover { background: #991b1b; }
        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 12px;
        }
        .topbar a {
            color: #cce3ff;
            text-decoration: none;
            border: 1px solid #355886;
            border-radius: 10px;
            padding: 8px 12px;
            background: #10233f;
        }
        .alert {
            border-radius: 10px;
            padding: 10px 12px;
            margin-bottom: 10px;
            border: 1px solid;
            font-size: 13px;
        }
        .alert-error { border-color: #7f1d1d; background: rgba(127, 29, 29, 0.35); color: #fecaca; }
        .alert-success { border-color: #065f46; background: rgba(6, 95, 70, 0.35); color: #bbf7d0; }
        .kpi {
            border: 1px solid var(--line);
            border-radius: 10px;
            background: #0a1424;
            padding: 10px;
            text-align: center;
        }
        .kpi .value { font-size: 24px; font-weight: 700; display: block; }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }
        th, td {
            border-bottom: 1px solid #223554;
            padding: 8px 6px;
            text-align: left;
            vertical-align: top;
        }
        th { color: #a7c2e8; font-weight: 600; }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 999px;
            font-size: 11px;
            border: 1px solid;
            font-weight: 600;
        }
        .status-active { background: rgba(5, 150, 105, 0.2); border-color: #059669; color: #6ee7b7; }
        .status-expired { background: rgba(180, 83, 9, 0.2); border-color: #d97706; color: #fde68a; }
        .status-revoked { background: rgba(185, 28, 28, 0.2); border-color: #dc2626; color: #fecaca; }
        .status-other { background: rgba(37, 99, 235, 0.2); border-color: #2563eb; color: #bfdbfe; }
        .pager {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            margin-top: 12px;
            flex-wrap: wrap;
        }
        .pager-links {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .pager-links a,
        .pager-links span {
            display: inline-block;
            border: 1px solid #355886;
            border-radius: 8px;
            padding: 6px 10px;
            text-decoration: none;
            background: #10233f;
            color: #d7e9ff;
            font-size: 12px;
        }
        .pager-links .current {
            border-color: #2ec4b6;
            color: #9cf6ed;
            background: #0f2f45;
        }
        code {
            font-family: Consolas, monospace;
            background: #0f1d32;
            border: 1px solid #27466b;
            padding: 2px 5px;
            border-radius: 5px;
            color: #cde7ff;
        }
    </style>
</head>
<body>
<div class="container">
    <div class="panel">
        <div class="topbar">
            <div>
                <h1>Панель управления лицензиями</h1>
                <div class="muted">Вход по ключу администратора из config.php</div>
            </div>
            <?php if ($isAuthenticated): ?>
                <a href="?logout=1">Выйти</a>
            <?php endif; ?>
        </div>

        <?php if ($fatalError !== ''): ?>
            <div class="alert alert-error"><?php echo panelE($fatalError); ?></div>
        <?php endif; ?>

        <?php if ($error !== ''): ?>
            <div class="alert alert-error"><?php echo panelE($error); ?></div>
        <?php endif; ?>

        <?php if ($success !== ''): ?>
            <div class="alert alert-success">
                <?php echo panelE($success); ?>
                <?php if ($generatedLicenseKey !== ''): ?>
                    <br>Ключ: <code><?php echo panelE($generatedLicenseKey); ?></code>
                    <br>Действует до: <code><?php echo panelE($generatedExpiresAt); ?></code>
                <?php endif; ?>
            </div>
        <?php endif; ?>

        <?php if (!$isAuthenticated): ?>
            <form method="post" class="row">
                <input type="hidden" name="panel_action" value="login">
                <div class="col-8">
                    <label>Ключ администратора</label>
                    <input type="password" name="admin_key" autocomplete="current-password" required>
                </div>
                <div class="col-4">
                    <label>&nbsp;</label>
                    <button type="submit" class="btn-primary">Войти</button>
                </div>
            </form>
            <p class="muted" style="margin-top:10px;">
                Адрес панели: <code>.../license-api/admin_panel.php</code>
            </p>
        <?php else: ?>
            <div class="row" style="margin-bottom:12px;">
                <div class="col-3"><div class="kpi"><span class="value"><?php echo (int)$stats['total']; ?></span>Всего</div></div>
                <div class="col-3"><div class="kpi"><span class="value"><?php echo (int)$stats['active']; ?></span>Активные</div></div>
                <div class="col-3"><div class="kpi"><span class="value"><?php echo (int)$stats['expired']; ?></span>Истекшие</div></div>
                <div class="col-3"><div class="kpi"><span class="value"><?php echo (int)$stats['revoked']; ?></span>Отозванные</div></div>
            </div>

            <div class="row" style="margin-bottom:14px;">
                <div class="col-6 panel">
                    <h2>Создание лицензии</h2>
                    <form method="post" class="row">
                        <input type="hidden" name="csrf_token" value="<?php echo panelE($csrfToken); ?>">
                        <input type="hidden" name="panel_action" value="create">

                        <div class="col-6">
                            <label>Дней</label>
                            <input type="number" min="1" name="days" value="<?php echo panelE(panelOld('days')); ?>" placeholder="30">
                        </div>
                        <div class="col-6">
                            <label>Или дата истечения (UTC)</label>
                            <input type="text" name="expires_at" value="<?php echo panelE(panelOld('expires_at')); ?>" placeholder="2026-12-31 23:59:59">
                        </div>
                        <div class="col-12">
                            <label>Метка пользователя</label>
                            <input type="text" name="user_label" value="<?php echo panelE(panelOld('user_label')); ?>" placeholder="имя, email или комментарий">
                        </div>
                        <div class="col-12">
                            <label>Примечание</label>
                            <textarea name="notes" placeholder="Дополнительная заметка"><?php echo panelE(panelOld('notes')); ?></textarea>
                        </div>
                        <div class="col-12">
                            <button type="submit" class="btn-primary">Создать лицензию</button>
                        </div>
                    </form>
                </div>

                <div class="col-6 panel">
                    <h2>Продление лицензии</h2>
                    <form method="post" class="row" style="margin-bottom:12px;">
                        <input type="hidden" name="csrf_token" value="<?php echo panelE($csrfToken); ?>">
                        <input type="hidden" name="panel_action" value="extend">

                        <div class="col-12">
                            <label>Ключ лицензии</label>
                            <input type="text" name="license_key" value="<?php echo panelE(panelOld('license_key')); ?>" placeholder="AAAAA-BBBBB-CCCCC-DDDDD" required>
                        </div>
                        <div class="col-6">
                            <label>Дней</label>
                            <input type="number" min="1" name="days" value="<?php echo panelE(panelOld('days')); ?>" placeholder="30">
                        </div>
                        <div class="col-6">
                            <label>Или дата истечения (UTC)</label>
                            <input type="text" name="expires_at" value="<?php echo panelE(panelOld('expires_at')); ?>" placeholder="2026-12-31 23:59:59">
                        </div>
                        <div class="col-12">
                            <button type="submit">Продлить лицензию</button>
                        </div>
                    </form>

                    <h2>Отзыв и сброс устройства</h2>
                    <form method="post" class="row" style="margin-bottom:8px;">
                        <input type="hidden" name="csrf_token" value="<?php echo panelE($csrfToken); ?>">
                        <input type="hidden" name="panel_action" value="revoke">
                        <div class="col-8">
                            <label>Ключ лицензии</label>
                            <input type="text" name="license_key" placeholder="AAAAA-BBBBB-CCCCC-DDDDD" required>
                        </div>
                        <div class="col-4">
                            <label>&nbsp;</label>
                            <button type="submit" class="btn-danger">Отозвать</button>
                        </div>
                    </form>

                    <form method="post" class="row">
                        <input type="hidden" name="csrf_token" value="<?php echo panelE($csrfToken); ?>">
                        <input type="hidden" name="panel_action" value="reset-device">
                        <div class="col-8">
                            <label>Ключ лицензии</label>
                            <input type="text" name="license_key" placeholder="AAAAA-BBBBB-CCCCC-DDDDD" required>
                        </div>
                        <div class="col-4">
                            <label>&nbsp;</label>
                            <button type="submit">Сбросить устройство</button>
                        </div>
                    </form>
                </div>
            </div>

            <div class="panel">
                <h2>Список лицензий</h2>
                <form method="get" class="row" style="margin-bottom:10px;">
                    <div class="col-3">
                        <label>Поиск по ключу</label>
                        <input type="text" name="q" value="<?php echo panelE($listSearch); ?>" placeholder="AAAAA-BBBBB">
                    </div>
                    <div class="col-2">
                        <label>Статус</label>
                        <select name="status">
                            <option value=""<?php echo $listStatus === '' ? ' selected' : ''; ?>>Все</option>
                            <option value="active"<?php echo $listStatus === 'active' ? ' selected' : ''; ?>>Активна</option>
                            <option value="expired"<?php echo $listStatus === 'expired' ? ' selected' : ''; ?>>Истекла</option>
                            <option value="revoked"<?php echo $listStatus === 'revoked' ? ' selected' : ''; ?>>Отозвана</option>
                        </select>
                    </div>
                    <div class="col-2">
                        <label>Истекает от</label>
                        <input type="date" name="expires_from" value="<?php echo panelE($listExpiresFromRaw); ?>">
                    </div>
                    <div class="col-2">
                        <label>Истекает до</label>
                        <input type="date" name="expires_to" value="<?php echo panelE($listExpiresToRaw); ?>">
                    </div>
                    <div class="col-1">
                        <label>На странице</label>
                        <select name="per_page">
                            <?php foreach ([25, 50, 100, 200] as $perPageOption): ?>
                                <option value="<?php echo (int)$perPageOption; ?>"<?php echo $listPerPage === $perPageOption ? ' selected' : ''; ?>><?php echo (int)$perPageOption; ?></option>
                            <?php endforeach; ?>
                        </select>
                    </div>
                    <div class="col-1">
                        <label>&nbsp;</label>
                        <button type="submit">Применить</button>
                    </div>
                    <div class="col-1">
                        <label>&nbsp;</label>
                        <a class="btn-link" href="admin_panel.php">Сбросить</a>
                    </div>
                </form>
                <div class="muted" style="margin-bottom:8px;">
                    Сортировка: по времени обновления (новые сверху). Показано <?php echo (int)$listFrom; ?>-<?php echo (int)$listTo; ?> из <?php echo (int)$listTotal; ?>.
                </div>
                <table>
                    <thead>
                    <tr>
                        <th>Ключ лицензии</th>
                        <th>Пользователь</th>
                        <th>Статус</th>
                        <th>Истекает</th>
                        <th>Устройство</th>
                        <th>Последняя проверка</th>
                        <th>Обновлено</th>
                        <th>Примечание</th>
                    </tr>
                    </thead>
                    <tbody>
                    <?php if (!$licenses): ?>
                        <tr><td colspan="8" class="muted">Нет данных</td></tr>
                    <?php else: ?>
                        <?php foreach ($licenses as $row): ?>
                            <?php
                                $status = (string)($row['status'] ?? '');
                                $device = (string)($row['device_id'] ?? '');
                            ?>
                            <tr>
                                <td><code><?php echo panelE((string)$row['license_key']); ?></code></td>
                                <td><?php echo panelE((string)($row['user_label'] ?? '-')); ?></td>
                                <td>
                                    <span class="badge <?php echo panelE(panelStatusClass($status)); ?>">
                                        <?php echo panelE(panelStatusLabel($status)); ?>
                                    </span>
                                </td>
                                <td><?php echo panelE((string)($row['expires_at'] ?? '-')); ?></td>
                                <td title="<?php echo panelE($device); ?>"><?php echo panelE(panelShortText($device, 22)); ?></td>
                                <td><?php echo panelE((string)($row['last_check_at'] ?? '-')); ?></td>
                                <td><?php echo panelE((string)($row['updated_at'] ?? '-')); ?></td>
                                <td><?php echo panelE((string)($row['notes'] ?? '-')); ?></td>
                            </tr>
                        <?php endforeach; ?>
                    <?php endif; ?>
                    </tbody>
                </table>
                <?php if ($listPages > 1): ?>
                    <div class="pager">
                        <div class="muted">Страница <?php echo (int)$listPage; ?> из <?php echo (int)$listPages; ?></div>
                        <div class="pager-links">
                            <?php if ($listPage > 1): ?>
                                <a href="<?php echo panelE(panelBuildListUrl(1, $listPerPage, $listSearch, $listStatus, $listExpiresFromRaw, $listExpiresToRaw)); ?>">Первая</a>
                                <a href="<?php echo panelE(panelBuildListUrl($listPage - 1, $listPerPage, $listSearch, $listStatus, $listExpiresFromRaw, $listExpiresToRaw)); ?>">Назад</a>
                            <?php endif; ?>

                            <span class="current"><?php echo (int)$listPage; ?></span>

                            <?php if ($listPage < $listPages): ?>
                                <a href="<?php echo panelE(panelBuildListUrl($listPage + 1, $listPerPage, $listSearch, $listStatus, $listExpiresFromRaw, $listExpiresToRaw)); ?>">Вперед</a>
                                <a href="<?php echo panelE(panelBuildListUrl($listPages, $listPerPage, $listSearch, $listStatus, $listExpiresFromRaw, $listExpiresToRaw)); ?>">Последняя</a>
                            <?php endif; ?>
                        </div>
                    </div>
                <?php endif; ?>
            </div>
        <?php endif; ?>
    </div>
</div>
</body>
</html>
