#!/usr/bin/env php
<?php

declare(strict_types=1);

require dirname(__DIR__) . '/.github/actions/shared-libraries/recipe-inputs.php';

$directory = sys_get_temp_dir() . '/library-recipe-inputs-' . bin2hex(random_bytes(8));
if (!mkdir($directory, 0700) || !mkdir($directory . '/patches', 0700)) {
    throw new RuntimeException('Cannot create recipe input fixture');
}
$fixture = $directory . '/hooks.php';
$targetPatch = $directory . '/patches/target.patch';
$localPatch = $directory . '/local.patch';
$unrelatedPatch = $directory . '/patches/unrelated.patch';
$class = 'RecipeInputsFixture_' . bin2hex(random_bytes(8));
$source = '<?php' . "\n" . 'class ' . $class . <<<'PHP'

{
    public function target(): array
    {
        return ['target.patch', 'local.patch', 'target-original'];
    }

    public function unrelated(): array
    {
        return ['unrelated.patch', 'unrelated-original'];
    }
}
PHP;
$assertions = 0;
$assert = static function (bool $condition, string $message) use (&$assertions): void {
    ++$assertions;
    if (!$condition) {
        throw new RuntimeException($message);
    }
};

try {
    file_put_contents($fixture, $source);
    file_put_contents($targetPatch, 'target patch original');
    file_put_contents($localPatch, 'local patch original');
    file_put_contents($unrelatedPatch, 'unrelated patch original');
    require $fixture;
    $hook = new $class();
    $recipe = static function () use ($hook, $directory): array {
        $result = ['code' => [], 'patches' => []];
        libraryRecipeCallback($result, [$hook, 'target'], [$directory . '/patches']);
        return $result;
    };
    $baseline = $recipe();
    $assert(isset($baseline['code'][$class . '::target']), 'Target callback method must be recorded');
    $assert(!isset($baseline['code'][$class . '::unrelated']), 'Unrelated callback must not be recorded');
    $assert(($baseline['patches']['target.patch'] ?? null) === hash_file('sha256', $targetPatch), 'Patch roots must resolve target patch contents');
    $assert(($baseline['patches']['local.patch'] ?? null) === hash_file('sha256', $localPatch), 'Callback directory must resolve local patch contents');
    $assert(!isset($baseline['patches']['unrelated.patch']), 'Unrelated patch must not be recorded');

    file_put_contents($fixture, str_replace('target-original', 'target-modified', $source));
    $assert($recipe() !== $baseline, 'Changing the target callback body must change its recipe');

    file_put_contents($fixture, str_replace('unrelated-original', 'unrelated-modified', $source));
    $assert($recipe() === $baseline, 'Changing an unrelated callback body must preserve the target recipe');

    file_put_contents($fixture, $source);
    file_put_contents($targetPatch, 'target patch modified');
    $assert($recipe() !== $baseline, 'Changing the target patch must change its recipe');

    file_put_contents($targetPatch, 'target patch original');
    file_put_contents($localPatch, 'local patch modified');
    $assert($recipe() !== $baseline, 'Changing a callback-local patch must change its recipe');

    file_put_contents($localPatch, 'local patch original');
    file_put_contents($unrelatedPatch, 'unrelated patch modified');
    $assert($recipe() === $baseline, 'Changing an unrelated patch must preserve the target recipe');

    echo "Library recipe input tests passed ({$assertions} assertions).\n";
} finally {
    foreach ([$fixture, $targetPatch, $localPatch, $unrelatedPatch] as $file) {
        if (is_file($file)) {
            unlink($file);
        }
    }
    rmdir($directory . '/patches');
    rmdir($directory);
}
