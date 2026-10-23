<?php

declare(strict_types=1);

function check(bool $ok, string $name): void
{
    if (!$ok) {
        throw new RuntimeException("Runtime smoke failed: {$name}");
    }
}

check(preg_match('/^php-zts$/', basename(PHP_BINARY)) === 1 || PHP_SAPI !== 'cli', 'packaged PHP binary');
check(json_decode(json_encode(['shared' => true]), true)['shared'], 'JSON round trip');
check(preg_match('/shared/', 'shared runtime') === 1, 'PCRE');
if (extension_loaded('zlib')) {
    check(gzdecode(gzencode('shared runtime')) === 'shared runtime', 'zlib');
}
if (extension_loaded('openssl')) {
    $key = str_repeat('k', 32);
    $iv = str_repeat('i', 16);
    $cipher = openssl_encrypt('shared runtime', 'aes-256-cbc', $key, OPENSSL_RAW_DATA, $iv);
    check(is_string($cipher) && openssl_decrypt($cipher, 'aes-256-cbc', $key, OPENSSL_RAW_DATA, $iv) === 'shared runtime', 'OpenSSL provider');
}
if (extension_loaded('curl')) {
    check(is_array(curl_version()) && curl_init() instanceof CurlHandle, 'curl');
}
if (extension_loaded('mbstring')) {
    check(mb_strlen('é', 'UTF-8') === 1, 'mbstring');
}
if (extension_loaded('intl')) {
    check(Normalizer::normalize("e\u{301}") === 'é', 'ICU data');
}
if (extension_loaded('pdo_sqlite')) {
    $db = new PDO('sqlite::memory:');
    check((int) $db->query('SELECT 42')->fetchColumn() === 42, 'SQLite');
}
if (extension_loaded('sodium')) {
    check(strlen(sodium_crypto_generichash('shared runtime')) === SODIUM_CRYPTO_GENERICHASH_BYTES, 'sodium');
}
if (extension_loaded('bz2')) {
    check(bzdecompress(bzcompress('shared runtime')) === 'shared runtime', 'bzip2');
}
if (extension_loaded('gmp')) {
    check(gmp_strval(gmp_add('40', '2')) === '42', 'GMP');
}
if (extension_loaded('gd')) {
    $image = imagecreatetruecolor(2, 2);
    ob_start();
    imagepng($image);
    $png = ob_get_clean();
    check(is_string($png) && str_starts_with($png, "\x89PNG\r\n\x1a\n"), 'GD PNG encoder');
    check(imagecreatefromstring($png) instanceof GdImage, 'GD PNG decoder');
    imagedestroy($image);
}
if (extension_loaded('imagick')) {
    $image = new Imagick();
    $image->newImage(2, 2, 'red');
    $image->setImageFormat('png');
    check(str_starts_with($image->getImageBlob(), "\x89PNG\r\n\x1a\n"), 'ImageMagick PNG coder');
    $image->clear();
}
if (extension_loaded('xsl') && extension_loaded('dom')) {
    $xml = new DOMDocument();
    $xml->loadXML('<value>42</value>');
    $style = new DOMDocument();
    $style->loadXML('<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:output method="text"/><xsl:template match="/"><xsl:value-of select="value"/></xsl:template></xsl:stylesheet>');
    $processor = new XSLTProcessor();
    check($processor->importStylesheet($style) && trim($processor->transformToXML($xml)) === '42', 'libxslt');
}
if (extension_loaded('zip')) {
    $file = tempnam(sys_get_temp_dir(), 'spp-zip-');
    try {
        $zip = new ZipArchive();
        check($zip->open($file, ZipArchive::OVERWRITE) === true, 'zip create');
        check($zip->addFromString('test', 'shared runtime') && $zip->close(), 'zip write');
        check($zip->open($file) === true && $zip->getFromName('test') === 'shared runtime', 'zip read');
        $zip->close();
    } finally {
        unlink($file);
    }
}

header('Content-Type: application/json');
$iniFiles = array_filter(array_map(static fn($file) => trim((string) $file), array_merge(
    [php_ini_loaded_file()],
    preg_split('/,\s*/', php_ini_scanned_files() ?: '', flags: PREG_SPLIT_NO_EMPTY),
)));
$loadedObjects = [];
foreach (file('/proc/self/maps', FILE_IGNORE_NEW_LINES) as $line) {
    if (preg_match('~\s(/[^\r\n]+)$~', $line, $match) && ($path = realpath($match[1])) !== false) {
        $loadedObjects[$path] = true;
    }
}
echo json_encode([
    'ok' => true,
    'version' => PHP_VERSION,
    'sapi' => PHP_SAPI,
    'extensions' => get_loaded_extensions(),
    'zend_extensions' => get_loaded_extensions(true),
    'ini_files' => array_values($iniFiles),
    'loaded_objects' => array_keys($loadedObjects),
    'features' => [
        'mbregex' => function_exists('mb_ereg'),
        'password-argon2' => in_array('argon2id', password_algos(), true),
    ],
    'vm' => defined('ZEND_VM_KIND') ? ZEND_VM_KIND : 'unknown',
    'extension_dir' => ini_get('extension_dir'),
], JSON_THROW_ON_ERROR), "\n";
