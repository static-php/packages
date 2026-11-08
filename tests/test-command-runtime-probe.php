<?php

declare(strict_types=1);

use staticphp\Command\TestCommand;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

$command = (new ReflectionClass(TestCommand::class))->newInstanceWithoutConstructor();
$generator = new ReflectionMethod(TestCommand::class, 'runtimeProbeScript');
$probe = $generator->invoke($command);
if (!is_string($probe)) {
    throw new RuntimeException('Runtime probe generator did not return PHP source');
}
$body = preg_replace('/\A\s*<\?php\b/', '', $probe, 1, $openingTags);
if ($openingTags !== 1) {
    throw new RuntimeException('Runtime probe lacks its PHP opening tag');
}

$temporary = new Process(['mktemp', '-d', sys_get_temp_dir() . '/spp-runtime-probe-test-XXXXXX']);
$temporary->mustRun();
$directory = trim($temporary->getOutput());
$files = [];
$checks = 0;
$run = static function (string $name, string $source, bool $ready, bool $encoded = false) use ($directory, &$files, &$checks): void {
    $script = $directory . '/' . $name . '.php';
    $trace = $directory . '/' . $name . '.trace';
    $files[] = $script;
    $files[] = $trace;
    file_put_contents($script, $source);
    $process = new Process([PHP_BINARY, '-n', $script, $trace]);
    $process->setTimeout(10);
    $process->run();
    $output = $process->getOutput();
    if ($ready ? !$process->isSuccessful() || $output !== 'PONG' : $output === 'PONG' || str_contains($output, 'PONG')) {
        throw new RuntimeException("Unexpected readiness result for {$name}: exit=" . var_export($process->getExitCode(), true)
            . ' stdout=' . var_export($output, true) . ' stderr=' . $process->getErrorOutput());
    }
    if ($encoded && (!is_file($trace) || !str_contains((string) file_get_contents($trace), "encoded\n"))) {
        throw new RuntimeException("{$name} did not exercise PNG encoding");
    }
    $checks++;
};

// A namespace changes name resolution while leaving the generated probe body intact.
$fixture = static function (string $scenario) use ($body): string {
    return "<?php\nnamespace SppRuntimeProbeFixture;\nconst SCENARIO = " . var_export($scenario, true) . ";\n" . <<<'PHP'
function extension_loaded(string $extension): bool
{
    return SCENARIO !== 'absent' && $extension === 'imagick';
}
class Imagick
{
    private bool $created = false;
    private bool $formatted = false;

    public function newImage(int $width, int $height, mixed $color): bool
    {
        if (SCENARIO === 'absent' || $width !== 2 || $height !== 2) {
            throw new \RuntimeException('Runtime probe did not create a 2x2 image');
        }
        $this->created = true;
        return true;
    }
    public function setImageFormat(string $format): bool
    {
        if (!$this->created || strtolower($format) !== 'png') {
            throw new \RuntimeException('Runtime probe did not select PNG encoding');
        }
        $this->formatted = true;
        return true;
    }
    public function getImageBlob(): string
    {
        if (!$this->created || !$this->formatted) {
            throw new \RuntimeException('Runtime probe did not prepare the PNG image');
        }
        file_put_contents($GLOBALS['argv'][1], "encoded\n", FILE_APPEND);
        if (SCENARIO === 'exception') {
            throw new \RuntimeException('Imagick encoding fixture failed');
        }
        $png = base64_decode('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEUlEQVR4nGP4z8DwH4QZYAwAR8oH+WdZbrcAAAAASUVORK5CYII=', true);
        return match (SCENARIO) {
            'bad-signature' => 'INVALID!' . substr($png, 8),
            'bad-ihdr' => substr_replace($png, 'IDAT', 12, 4),
            'wrong-width' => substr_replace($png, pack('N', 3), 16, 4),
            'wrong-height' => substr_replace($png, pack('N', 3), 20, 4),
            'truncated' => substr($png, 0, 16),
            default => $png,
        };
    }
    public function clear(): bool { return true; }
    public function destroy(): bool { return true; }
}
PHP
        . "\n" . $body;
};

try {
    $run('unmodified', $probe, true);
    $run('absent', $fixture('absent'), true);
    $run('valid-png', $fixture('valid-png'), true, true);
    foreach (['bad-signature', 'bad-ihdr', 'wrong-width', 'wrong-height', 'truncated', 'exception'] as $scenario) {
        $run($scenario, $fixture($scenario), false, true);
    }
    echo "Passed {$checks} TestCommand runtime-probe checks.\n";
} finally {
    foreach ($files as $file) {
        if (file_exists($file) && !unlink($file)) {
            throw new RuntimeException('Could not remove runtime-probe test artifact: ' . $file);
        }
    }
    if (!rmdir($directory)) {
        throw new RuntimeException('Could not remove runtime-probe test directory: ' . $directory);
    }
}
