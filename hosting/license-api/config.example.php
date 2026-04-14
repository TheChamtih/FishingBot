<?php

declare(strict_types=1);

return [
    // Set a long random string. Keep private.
    'admin_key' => 'CHANGE_ME_TO_LONG_RANDOM_SECRET',

    // CORS policy for your launcher.
    'allow_origin' => '*',

    'db' => [
        // 'sqlite' (recommended for quick start) or 'mysql'.
        'driver' => 'sqlite',

        // Used when driver=sqlite.
        'sqlite_path' => __DIR__ . '/storage/licenses.sqlite',

        // Used when driver=mysql.
        'mysql' => [
            'host' => '127.0.0.1',
            'port' => 3306,
            'database' => 'YOUR_DB_NAME',
            'user' => 'YOUR_DB_USER',
            'password' => 'YOUR_DB_PASSWORD',
            'charset' => 'utf8mb4',
        ],
    ],
];
