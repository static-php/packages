<?php

declare(strict_types=1);

use Package\Target\php as PhpTarget;
use staticphp\hook\FrankenPhp86;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Package\TargetPackage;
use StaticPHP\Registry\Registry;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/hook/FrankenPhp86.php';

final class FrankenPatchTarget extends TargetPackage
{
    public function __construct(private string $source)
    {
        parent::__construct('frankenphp', 'target');
    }

    public function getSourceDir(): string
    {
        return $this->source;
    }
}

function runFrankenPatch(array $arguments, ?array $environment = null, ?string $cwd = null): string
{
    $process = new Process($arguments, $cwd, $environment);
    $process->setTimeout(60);
    $process->mustRun();
    return $process->getOutput();
}

function removeFrankenPatch(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $file) {
        if (!$file->isDot()) {
            removeFrankenPatch($file->getPathname());
        }
    }
    rmdir($path);
}

function frankenArchiveFile(string $archive, string $suffix): string
{
    $members = explode("\n", trim(runFrankenPatch(['tar', '-tf', $archive])));
    $members = array_values(array_filter($members, static fn (string $member): bool => str_ends_with($member, '/' . $suffix)));
    if (count($members) !== 1) {
        throw new RuntimeException("Expected exactly one {$suffix} in {$archive}");
    }
    return runFrankenPatch(['tar', '-xOf', $archive, $members[0]]);
}

function frankenProbeSource(string $block, string $header, string $handlerType): string
{
    return '#include ' . json_encode($header, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n"
        . <<<'C'
#include <assert.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

typedef struct { size_t len; unsigned int refcount; bool persistent; char value[64]; } zend_string;
typedef struct { zend_string *value; } zval;
static unsigned int copies, releases, calls, flushes;
static size_t expected_chunk;
static const char *expected_handler;
#define PHP_OUTPUT_HANDLER_STDFLAGS 112
#define ZSTR_LEN(s) ((s)->len)
#define PG(name) globals.name

static zend_string *zend_string_dup(zend_string *source, bool persistent)
{
    assert(!persistent);
    zend_string *copy = malloc(sizeof(*copy));
    assert(copy != NULL);
    *copy = *source;
    copy->refcount = 1;
    copy->persistent = persistent;
    copies++;
    return copy;
}

static void set_string(zval *value, const char *text)
{
    zend_string original = {strlen(text), 1, true, {0}};
    strcpy(original.value, text);
    value->value = zend_string_dup(&original, false);
}

#define ZVAL_STRING(zv, text) set_string((zv), (text))
#define ZVAL_STR(zv, string) ((zv)->value = (string))

C
        . 'static struct { ' . $handlerType . " output_handler; int output_buffering; int implicit_flush; } globals;\n"
        . <<<'C'

static void php_output_start_user(zval *handler, size_t chunk, int flags)
{
    assert(flags == PHP_OUTPUT_HANDLER_STDFLAGS);
    assert(chunk == expected_chunk);
    assert((handler == NULL) == (expected_handler == NULL));
    if (handler != NULL) {
        assert(strcmp(handler->value->value, expected_handler) == 0);
        assert(!handler->value->persistent);
#if PHP_VERSION_ID >= 80600
        assert(handler->value != PG(output_handler));
#endif
    }
    calls++;
}

static void zval_ptr_dtor(zval *value)
{
    assert(value->value->refcount == 1);
    free(value->value);
    releases++;
}

static void php_output_set_implicit_flush(int enabled)
{
    assert(enabled == 1);
    flushes++;
}

static void start_output(void)
{
C
        . "\n" . $block . "\n}\n"
        . <<<'C'

static void check(const char *text, int buffering, int flush, unsigned int starts, unsigned int implicit)
{
    copies = releases = calls = flushes = 0;
    globals.output_buffering = buffering;
    globals.implicit_flush = flush;
#if PHP_VERSION_ID >= 80600
    zend_string original = {0, 1, true, {0}};
    if (text != NULL) {
        original.len = strlen(text);
        strcpy(original.value, text);
    }
    PG(output_handler) = text == NULL ? NULL : &original;
#else
    PG(output_handler) = (char *)text;
#endif
    expected_handler = text != NULL && text[0] != '\0' ? text : NULL;
    expected_chunk = expected_handler == NULL && buffering > 1 ? (size_t)buffering : 0;
    start_output();
    assert(calls == starts);
    assert(flushes == implicit);
    assert(copies == (expected_handler != NULL ? 1U : 0U));
    assert(releases == copies);
#if PHP_VERSION_ID >= 80600
    assert(original.refcount == 1);
    assert(original.persistent);
    if (text != NULL) {
        assert(strcmp(original.value, text) == 0);
    }
#endif
}

int main(void)
{
    check(NULL, 0, 0, 0, 0);
    check("", 0, 0, 0, 0);
    check("ob_gzhandler", 0, 0, 1, 0);
    check("ob_gzhandler", 4096, 1, 1, 0);
    check(NULL, 1, 1, 1, 0);
    check("", 4096, 1, 1, 0);
    check(NULL, 0, 1, 0, 1);
    check("", 0, 1, 0, 1);
    return 0;
}
C;
}

$options = getopt('', ['downloads:', 'versions:', 'frankenphp:', 'fixture:']);
if (isset($options['fixture'])) {
    require ROOT_DIR . '/src/bootstrap.php';
    Registry::resolve();
    $source = $options['fixture'];
    $file = $source . '/frankenphp.c';
    $before = hash_file('sha256', $file);
    $hook = new FrankenPhp86();
    $applied = $hook->patchBeforeBuild(new FrankenPatchTarget($source));
    $after = hash_file('sha256', $file);
    if ($applied && (!$hook->patchBeforeBuild(new FrankenPatchTarget($source)) || hash_file('sha256', $file) !== $after)) {
        throw new RuntimeException('FrankenPHP output-handler patch is not idempotent');
    }
    echo json_encode(['applied' => $applied, 'changed' => $before !== $after], JSON_THROW_ON_ERROR) . "\n";
    exit;
}

$stage = (new ReflectionMethod(FrankenPhp86::class, 'patchBeforeBuild'))->getAttributes(BeforeStage::class)[0]->newInstance();
if ($stage->package_name !== 'frankenphp' || $stage->stage !== [PhpTarget::class, 'buildFrankenphpForUnix'] || $stage->only_when_package_resolved !== 'frankenphp') {
    throw new RuntimeException('FrankenPHP output-handler patch is not wired before its build stage');
}
$downloads = $options['downloads'] ?? dirname(__DIR__) . '/downloads';
$versions = explode(',', $options['versions'] ?? '8.2.33,8.3.33,8.4.24,8.5.9,8.6.0beta1,8.6.0RC2');
$frankenphp = $options['frankenphp'] ?? $downloads . '/php-frankenphp-caddy-v1.12.7-0-ga765b08.tar.gz';
$original = frankenArchiveFile($frankenphp, 'frankenphp.c');
$patch = dirname(__DIR__) . '/config/patches/frankenphp-php86-output-handler.patch';
$temporary = trim(runFrankenPatch(['mktemp', '-d', '/tmp/php-zts-franken-patches-XXXXXX']));
try {
    $fixtures = [];
    foreach ($versions as $version) {
        if (!preg_match('/^8\.[0-9]+\.[0-9]+(?:(?:alpha|beta|RC)[0-9]+)?$/D', $version)) {
            throw new RuntimeException("Invalid PHP version: {$version}");
        }
        $archives = array_values(array_filter([$downloads . '/php-' . $version . '.tar.gz', $downloads . '/php-' . $version . '.tar.xz'], 'is_file'));
        if (count($archives) !== 1) {
            throw new RuntimeException("Expected exactly one cached PHP {$version} archive in {$downloads}");
        }
        $fixture = $temporary . '/' . $version;
        mkdir($fixture . '/source/php-src/main', 0755, true);
        mkdir($fixture . '/frankenphp');
        foreach (['php_version.h', 'php_globals.h'] as $header) {
            file_put_contents($fixture . '/source/php-src/main/' . $header, frankenArchiveFile($archives[0], 'main/' . $header));
        }
        if (PhpTarget::getPHPVersion($fixture . '/source/php-src') !== $version) {
            throw new RuntimeException("PHP source version does not match {$version}");
        }
        file_put_contents($fixture . '/frankenphp/frankenphp.c', $original);
        $fixtures[$version] = $fixture;
    }
    $patched = null;
    foreach ($fixtures as $version => $fixture) {
        $environment = ['SOURCE_PATH' => $fixture . '/source', 'DOWNLOAD_PATH' => $fixture . '/downloads', 'BUILD_ROOT_PATH' => $fixture . '/buildroot', 'SPC_LOGS_DIR' => $fixture . '/logs', 'SPC_ENABLE_LOG_FILE' => '0'];
        $probe = static fn (): array => json_decode(runFrankenPatch([PHP_BINARY, __FILE__, '--fixture=' . $fixture . '/frankenphp'], $environment, $fixture), true, flags: JSON_THROW_ON_ERROR);
        $eligible = PhpTarget::getPHPVersionID($fixture . '/source/php-src') >= 80600;
        if ($probe() !== ['applied' => $eligible, 'changed' => $eligible]) {
            throw new RuntimeException("Unexpected FrankenPHP patch behavior for PHP {$version}");
        }
        if ($eligible) {
            runFrankenPatch(['patch', '--dry-run', '-p1', '-s', '-f', '-R', '-i', $patch, '-d', $fixture . '/frankenphp']);
            $patched = file_get_contents($fixture . '/frankenphp/frankenphp.c');
            $globals = $fixture . '/source/php-src/main/php_globals.h';
            $contents = file_get_contents($globals);
            file_put_contents($globals, preg_replace('/\bzend_string\s*\*\s*output_handler\s*;/', 'char *output_handler;', $contents, 1, $replacements));
            if ($replacements !== 1) {
                throw new RuntimeException("Missing PHP {$version} output-handler shape guard fixture");
            }
            file_put_contents($fixture . '/frankenphp/frankenphp.c', $original);
            if ($probe() !== ['applied' => false, 'changed' => false]) {
                throw new RuntimeException("PHP {$version} without the zend_string API was patched");
            }
            file_put_contents($globals, $contents);
        }
        echo "Passed PHP {$version}: " . ($eligible ? 'patch applies, repeats safely and checks source API' : 'FrankenPHP source stays byte-identical') . ".\n";
    }
    if ($patched === null || !preg_match('/^#if PHP_VERSION_ID >= 80600\n.*?(?=^    frankenphp_reset_super_globals\(\);)/ms', $patched, $block)) {
        throw new RuntimeException('No patched output-handler block found for native regression');
    }
    foreach ($fixtures as $version => $fixture) {
        if (!preg_match('/^\s*((?:zend_string|char)\s*\*)\s*output_handler\s*;/m', file_get_contents($fixture . '/source/php-src/main/php_globals.h'), $type)) {
            throw new RuntimeException("Missing PHP {$version} output-handler declaration");
        }
        $probe = $fixture . '/output-handler.c';
        file_put_contents($probe, frankenProbeSource($block[0], $fixture . '/source/php-src/main/php_version.h', $type[1]));
        runFrankenPatch(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-o', $fixture . '/output-handler', $probe]);
        runFrankenPatch([$fixture . '/output-handler']);
        echo "Passed PHP {$version}: native output-handler values, ownership and buffering fallbacks.\n";
    }
} finally {
    removeFrankenPatch($temporary);
}
