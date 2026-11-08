<?php

declare(strict_types=1);

function libraryRecipePatches(array &$recipe, string $source, string $file, array $patchRoots): void
{
    foreach (token_get_all("<?php\n" . $source) as $token) {
        if (!is_array($token) || $token[0] !== T_CONSTANT_ENCAPSED_STRING) {
            continue;
        }
        $literal = substr($token[1], 1, -1);
        if (!str_ends_with($literal, '.patch')) {
            continue;
        }
        $candidates = [$literal, dirname($file) . '/' . ltrim($literal, '/')];
        foreach ($patchRoots as $root) {
            $candidates[] = $root . '/' . ltrim($literal, '/');
        }
        foreach ($candidates as $candidate) {
            if (is_file($candidate)) {
                $recipe['patches'][$literal] = hash_file('sha256', $candidate);
                break;
            }
        }
    }
}

function libraryRecipeCallback(array &$recipe, array|string|Closure $callback, array $patchRoots): void
{
    $method = is_array($callback)
        ? new ReflectionMethod($callback[0], $callback[1])
        : new ReflectionFunction($callback);
    if (($file = $method->getFileName()) === false) {
        return;
    }
    $source = implode('', array_slice(file($file), $method->getStartLine() - 1, $method->getEndLine() - $method->getStartLine() + 1));
    $identity = $method instanceof ReflectionMethod ? $method->getDeclaringClass()->getName() . '::' . $method->getName() : $method->getName();
    $recipe['code'][$identity] = hash('sha256', $source);
    libraryRecipePatches($recipe, $source, $file, $patchRoots);
}

function libraryRecipeArtifact(array &$recipe, object $artifact, array $patchRoots): void
{
    foreach (['getSourceExtractCallback', 'getBinaryExtractCallback'] as $getter) {
        if (($callback = $artifact->$getter()) !== null) {
            libraryRecipeCallback($recipe, $callback, $patchRoots);
        }
    }
    foreach (['after_source_extract_callbacks', 'after_binary_extract_callbacks'] as $field) {
        $callbacks = (new ReflectionProperty($artifact, $field))->getValue($artifact);
        foreach ($callbacks as $item) {
            libraryRecipeCallback($recipe, $field === 'after_source_extract_callbacks' ? $item : $item['callback'], $patchRoots);
        }
    }
}
