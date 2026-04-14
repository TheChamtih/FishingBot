<?php

declare(strict_types=1);

return [
    // TODO: change before production use.
    'admin_key' => 'CHANGE_ME_TO_LONG_RANDOM_SECRET',
    'allow_origin' => '*',

    'db' => [
        'driver' => 'mysql',
        'sqlite_path' => __DIR__ . '/storage/licenses.sqlite',
        'mysql' => [
            'host' => 'localhost',
            'port' => 3306,
            'database' => 'kaufmanma3',
            'user' => 'kaufmanma3',
            'password' => 'N1GEWCNRWTXaE*TH',
            'charset' => 'utf8mb4',
        ],
    ],
];
