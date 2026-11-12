<?php

declare(strict_types=1);

setlocale(LC_NUMERIC, 'C');
ini_set('serialize_precision', '-1');
set_error_handler(static function (int $level, string $message): bool {
    if ($level === E_WARNING && $message === 'unexpected NAN value was coerced to string') {
        return true;
    }
    throw new ErrorException($message, 0, $level);
});
$values = [0.0, -0.0, 0.59, -0.59, 0.1, 0.5, 1.5, 2.5, 9.99999999999999,
    1e-308, 1e-300, 1e-20, 1e20, 1e23, 1e300, PHP_FLOAT_MIN, PHP_FLOAT_MAX, INF, -INF, NAN];
foreach (['0000000000000001', '8000000000000001', '000fffffffffffff',
    '0010000000000000', '3fefffffffffffff', '3ff0000000000001',
    '4340000000000000', '7fefffffffffffff'] as $bits) {
    $values[] = unpack('E', hex2bin($bits))[1];
}
mt_srand(23604, MT_RAND_MT19937);
for ($i = 0; $i < 2048; $i++) {
    $values[] = unpack('E', pack('N2', mt_rand(0, 0xffffffff), mt_rand(0, 0xffffffff)))[1];
}
$hash = hash_init('sha256');
foreach ($values as $value) {
    $bits = bin2hex(pack('E', $value));
    $serialized = serialize($value);
    $roundtrip = unserialize($serialized);
    if (!is_nan($value) && pack('E', $value) !== pack('E', $roundtrip)) {
        throw new RuntimeException('Float serialization changed bits: ' . $bits);
    }
    $result = [$bits, $serialized];
    foreach ([1, 2, 6, 14, 17, 20] as $precision) {
        ini_set('precision', (string) $precision);
        $cast = (string) $value;
        $copy = $value;
        settype($copy, 'string');
        if ($copy !== $cast) {
            throw new RuntimeException('Inconsistent string cast: ' . $bits);
        }
        $result[] = [$cast, sprintf('%.*g', $precision, $value),
            sprintf('%.*f', $precision, $value), number_format($value, $precision, '.', '')];
    }
    hash_update($hash, json_encode($result, JSON_THROW_ON_ERROR) . "\n");
}
printf("Passed %d float cases: %s\n", count($values), hash_final($hash));
