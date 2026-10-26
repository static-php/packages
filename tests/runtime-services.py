#!/usr/bin/env python3

import argparse
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import tarfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src/runtime/runtime-services"
NATIVE_IMAGE = None


class ServiceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="spp-runtime-services-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "root"
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "calls"
        self.log.write_text("")
        self.active = self.directory / "active"
        self.active.write_text("")
        self.validation_failure = self.directory / "validation-failure"
        self.validation_warning = self.directory / "validation-warning"
        self.restart_failure = self.directory / "restart-failure"
        self.environment = dict(os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"], SPP_TEST_LOG=str(self.log), SPP_TEST_ACTIVE=str(self.active), SPP_TEST_VALIDATE_FAIL=str(self.validation_failure), SPP_TEST_VALIDATE_WARN=str(self.validation_warning), SPP_TEST_RESTART_FAIL=str(self.restart_failure))
        self.write("/usr/share/php-zts/services/php-fpm-zts", "fpm|php-fpm-zts|/usr/sbin/php-fpm-zts|/etc/php-zts/php-fpm.conf\n")
        self.write("/etc/php-zts/php-fpm.conf", "configuration\n")
        self.write("/usr/lib/php-zts/lib/libfixture.so.1", "library-one\n")
        self.write("/usr/sbin/php-fpm-zts", """#!/bin/sh
printf 'validate %s\\n' "$*" >> "$SPP_TEST_LOG"
if [ -e "$SPP_TEST_VALIDATE_FAIL" ]; then echo 'invalid configuration' >&2; exit 1; fi
if [ -e "$SPP_TEST_VALIDATE_WARN" ]; then echo 'PHP Startup: Unable to load dynamic library broken.so' >&2; fi
""", executable=True)
        self.mock("systemctl", """#!/bin/sh
case "$1" in
    --quiet) grep -Fx "$3" "$SPP_TEST_ACTIVE" >/dev/null ;;
    daemon-reload) printf 'daemon-reload\\n' >> "$SPP_TEST_LOG" ;;
    restart|start)
        printf 'systemd %s %s\\n' "$1" "$2" >> "$SPP_TEST_LOG"
        [ ! -e "$SPP_TEST_RESTART_FAIL" ] ;;
    *) printf 'unexpected systemctl call: %s\\n' "$*" >&2; exit 2 ;;
esac
""")
        self.mock("rc-service", """#!/bin/sh
case "$2" in
    status) grep -Fx "$1" "$SPP_TEST_ACTIVE" >/dev/null ;;
    restart|start)
        printf 'openrc %s %s\\n' "$2" "$1" >> "$SPP_TEST_LOG"
        [ ! -e "$SPP_TEST_RESTART_FAIL" ] ;;
    *) printf 'unexpected rc-service call: %s\\n' "$*" >&2; exit 2 ;;
esac
""")
        self.mock("deb-systemd-helper", "#!/bin/sh\nprintf 'debian-helper %s %s\\n' \"$1\" \"$2\" >> \"$SPP_TEST_LOG\"\n")
        self.mock("rc-update", "#!/bin/sh\nprintf 'openrc-enable %s %s %s\\n' \"$1\" \"$2\" \"${3-}\" >> \"$SPP_TEST_LOG\"\n")

    def write(self, relative, content, executable=False):
        path = self.root / relative.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if executable:
            path.chmod(0o755)
        return path

    def mock(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def systemd(self, active=True):
        (self.root / "run/systemd/system").mkdir(parents=True)
        self.active.write_text("php-fpm-zts.service\n" if active else "")

    def invoke(self, action="finish", success=True):
        result = subprocess.run(["sh", str(HELPER), "--root", str(self.root), action], env=self.environment, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def restarts(self):
        return [line for line in self.log.read_text().splitlines() if " restart " in line]

    def starts(self):
        return [line for line in self.log.read_text().splitlines() if " start " in line]

    def queue_start(self, service="php-fpm-zts"):
        return self.write("/run/php-zts-runtime/start/" + service, "")

    def replace_library(self, content="library-two\n"):
        path = self.root / "usr/lib/php-zts/lib/libfixture.so.1"
        replacement = path.with_suffix(".replacement")
        replacement.write_text(content)
        replacement.replace(path)

    def test_coalesces_repeat_finish_and_library_only_change(self):
        self.systemd()
        self.invoke()
        self.invoke()
        self.assertEqual(self.restarts(), ["systemd restart php-fpm-zts.service"])
        self.replace_library()
        self.invoke()
        self.invoke()
        self.assertEqual(self.restarts(), ["systemd restart php-fpm-zts.service"] * 2)

    def test_stopped_disabled_service_never_starts_or_enables(self):
        self.systemd(active=False)
        self.invoke("begin")
        self.replace_library()
        self.invoke()
        self.assertEqual(self.restarts(), [])
        self.assertNotIn("validate", self.log.read_text())
        self.assertEqual(self.starts(), [])
        self.assertNotIn("enable", self.log.read_text())

    def test_fresh_debian_queue_starts_once_at_finish(self):
        self.systemd(active=False)
        self.write("/etc/debian_version", "11\n")
        self.mock("deb-systemd-invoke", "#!/bin/sh\nprintf 'debian %s %s\\n' \"$1\" \"$2\" >> \"$SPP_TEST_LOG\"\n")
        self.invoke()
        self.queue_start()
        self.assertEqual(self.starts(), [])
        self.assertNotIn("debian-helper", self.log.read_text())
        self.invoke()
        self.invoke()
        self.assertEqual(self.starts(), ["debian start php-fpm-zts.service"])
        self.assertEqual(self.restarts(), [])
        self.assertEqual(self.log.read_text().count("debian-helper unmask php-fpm-zts.service"), 1)
        self.assertEqual(self.log.read_text().count("debian-helper enable php-fpm-zts.service"), 1)
        self.assertFalse((self.root / "run/php-zts-runtime/start/php-fpm-zts").exists())

    def test_failed_fresh_validation_retains_queue_without_starting(self):
        self.systemd(active=False)
        self.write("/etc/debian_version", "11\n")
        self.mock("deb-systemd-invoke", "#!/bin/sh\nprintf 'debian %s %s\\n' \"$1\" \"$2\" >> \"$SPP_TEST_LOG\"\n")
        pending = self.queue_start()
        self.validation_failure.touch()
        self.invoke(success=False)
        self.assertEqual(self.starts(), [])
        self.assertNotIn("debian-helper", self.log.read_text())
        self.assertTrue(pending.exists())
        self.validation_failure.unlink()
        self.invoke()
        self.assertEqual(self.starts(), ["debian start php-fpm-zts.service"])
        self.assertFalse(pending.exists())

    def test_fresh_openrc_queue_enables_and_starts_at_finish(self):
        (self.root / "run/openrc").mkdir(parents=True)
        self.queue_start()
        self.assertEqual(self.starts(), [])
        self.invoke()
        self.invoke()
        self.assertEqual(self.starts(), ["openrc start php-fpm-zts"])
        self.assertEqual(self.log.read_text().count("openrc-enable add php-fpm-zts default"), 1)

    def test_generated_franken_hooks_queue_only_fresh_install(self):
        self.systemd(active=False)
        self.mock("getent", "#!/bin/sh\nexit 0\n")
        self.mock("chown", "#!/bin/sh\nexit 0\n")
        self.mock("setcap", "#!/bin/sh\nexit 0\n")
        self.mock("dpkg-trigger", "#!/bin/sh\nprintf 'dpkg-trigger %s\\n' \"$*\" >> \"$SPP_TEST_LOG\"\n")
        code = r'''define('BASE_PATH', $argv[1]); define('TEMP_DIR', $argv[2]);
require BASE_PATH . '/src/util/RuntimeServices.php';
$class = 'staticphp\\util\\RuntimeServices';
echo json_encode(['deb'=>$class::frankenPostInstall('deb','frankenphp','/usr/bin/frankenphp'),
'apk'=>$class::frankenPostInstall('apk','frankenphp','/usr/bin/frankenphp'),
'apk_upgrade'=>$class::frankenPostInstall('apk','frankenphp','/usr/bin/frankenphp',true),
'rpm'=>$class::frankenPostInstall('rpm','frankenphp','/usr/bin/frankenphp'),
'rpm_upgrade'=>$class::frankenPostInstall('rpm','frankenphp','/usr/bin/frankenphp',true)]);'''
        result = subprocess.run(["php", "-r", code, str(ROOT), str(self.directory)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        scripts = json.loads(result.stdout)
        pending = self.root / "run/php-zts-runtime/start/frankenphp"

        def invoke_hook(name, *arguments):
            contents = Path(scripts[name]).read_text()
            for prefix in ["/run/", "/var/lib/", "/var/log/"]:
                contents = contents.replace(prefix, str(self.root) + prefix)
            staged = self.directory / (name + "-isolated.sh")
            staged.write_text(contents)
            hook = subprocess.run(["sh", str(staged), *arguments], env=self.environment, capture_output=True, text=True)
            self.assertEqual(hook.returncode, 0, hook.stdout + hook.stderr)

        invoke_hook("deb", "configure", "1.13.2-1")
        self.assertFalse(pending.exists())
        invoke_hook("apk_upgrade", "1.13.3-r2", "1.13.3-r1")
        self.assertFalse(pending.exists())
        invoke_hook("deb", "configure")
        self.assertTrue(pending.exists())
        self.assertEqual(self.starts(), [])
        pending.unlink()
        invoke_hook("apk", "1.13.3-r1")
        self.assertTrue(pending.exists())
        self.assertEqual(self.starts(), [])
        pending.unlink()
        self.mock("deb-systemd-helper", "#!/bin/sh\nprintf 'debian-helper %s\\n' \"$*\" >> \"$SPP_TEST_LOG\"\n[ \"$1\" != --quiet ]\n")
        invoke_hook("deb", "configure")
        self.assertFalse(pending.exists())
        self.assertIn("debian-helper update-state frankenphp.service", self.log.read_text())
        self.assertIn("systemctl preset", Path(scripts["rpm"]).read_text())
        self.assertNotIn("systemctl preset", Path(scripts["rpm_upgrade"]).read_text())
        self.assertNotIn("/run/php-zts-runtime/start", Path(scripts["rpm_upgrade"]).read_text())

    def test_begin_snapshot_restores_running_service_stopped_by_package_hook(self):
        self.systemd()
        self.invoke("begin")
        self.active.write_text("")
        self.replace_library()
        self.invoke()
        self.assertEqual(self.restarts(), ["systemd restart php-fpm-zts.service"])
        self.assertFalse((self.root / "run/php-zts-runtime/before").exists())
        self.assertFalse((self.root / "run/php-zts-runtime/active").exists())

    def test_unchanged_transaction_does_not_restart(self):
        self.systemd()
        self.invoke("begin")
        self.invoke()
        self.assertEqual(self.restarts(), [])

    def test_configuration_failure_retains_retry_state(self):
        self.systemd()
        self.invoke("begin")
        self.replace_library()
        self.validation_failure.touch()
        self.invoke(success=False)
        self.assertEqual(self.restarts(), [])
        self.assertTrue((self.root / "run/php-zts-runtime/before").exists())
        self.validation_failure.unlink()
        self.invoke()
        self.assertEqual(self.restarts(), ["systemd restart php-fpm-zts.service"])
        self.invoke()
        self.assertEqual(len(self.restarts()), 1)

    def test_runtime_startup_warning_prevents_restart(self):
        self.systemd()
        self.validation_warning.touch()
        self.invoke(success=False)
        self.assertEqual(self.restarts(), [])
        self.validation_warning.unlink()
        self.invoke()
        self.assertEqual(len(self.restarts()), 1)

    def test_restart_failure_can_be_retried(self):
        self.systemd()
        self.restart_failure.touch()
        self.invoke(success=False)
        self.restart_failure.unlink()
        self.invoke()
        self.invoke()
        self.assertEqual(len(self.restarts()), 2)

    def test_debian_uses_policy_aware_service_invocation(self):
        self.systemd()
        self.write("/etc/debian_version", "11\n")
        self.mock("deb-systemd-invoke", "#!/bin/sh\nprintf 'debian %s %s\\n' \"$1\" \"$2\" >> \"$SPP_TEST_LOG\"\n")
        self.invoke()
        self.assertEqual(self.restarts(), ["debian restart php-fpm-zts.service"])
        self.assertNotIn("systemd restart", self.log.read_text())

    def test_openrc_restarts_only_active_service(self):
        (self.root / "run/openrc").mkdir(parents=True)
        self.active.write_text("php-fpm-zts\n")
        self.invoke()
        self.invoke()
        self.assertEqual(self.restarts(), ["openrc restart php-fpm-zts"])
        self.active.write_text("")
        self.replace_library()
        self.invoke()
        self.assertEqual(len(self.restarts()), 1)

    def test_no_service_manager_does_not_invoke_host_services(self):
        self.invoke()
        self.assertEqual(self.log.read_text(), "")

    def test_same_size_subsecond_runtime_change_is_detected(self):
        self.systemd()
        path = self.root / "usr/lib/php-zts/lib/libfixture.so.1"
        seconds = int(time.time())
        os.utime(path, ns=(seconds * 10**9 + 100, seconds * 10**9 + 100))
        self.invoke()
        path.write_text("library-two\n")
        os.utime(path, ns=(seconds * 10**9 + 200, seconds * 10**9 + 200))
        self.invoke()
        self.assertEqual(len(self.restarts()), 2)

    def test_versioned_module_directory_change_is_detected(self):
        self.systemd()
        self.write("/usr/share/php-zts/services/php-fpm-zts", "fpm|php-fpm-zts|/usr/sbin/php-fpm-zts|/etc/php-zts/php-fpm.conf|/usr/lib/php-zts86/modules\n")
        module = self.write("/usr/lib/php-zts86/modules/fixture.so", "module-one\n")
        self.invoke()
        replacement = module.with_suffix(".new")
        replacement.write_text("module-two\n")
        replacement.replace(module)
        self.invoke()
        self.assertEqual(len(self.restarts()), 2)

    def test_frankenphp_uses_validation_before_restart(self):
        self.systemd()
        self.write("/usr/share/php-zts/services/frankenphp", "frankenphp|frankenphp|/usr/bin/frankenphp|/etc/frankenphp/Caddyfile|/usr/lib/php-zts/modules\n")
        self.write("/etc/frankenphp/Caddyfile", "http://localhost { respond hello }\n")
        self.write("/usr/bin/frankenphp", "#!/bin/sh\nprintf 'franken-validate %s\\n' \"$*\" >> \"$SPP_TEST_LOG\"\n", executable=True)
        self.active.write_text("frankenphp.service\n")
        self.invoke()
        self.assertEqual(self.restarts(), ["systemd restart frankenphp.service"])
        self.assertIn("franken-validate validate --config " + str(self.root / "etc/frankenphp/Caddyfile") + " --adapter caddyfile", self.log.read_text())

    def test_native_apk_commit_hook_refreshes_once_after_library_upgrade(self):
        if NATIVE_IMAGE is None:
            self.skipTest("pass --native-image to exercise Alpine commit hooks")
        nfpm = shutil.which("nfpm") or "/home/m/go/bin/nfpm"
        if not Path(nfpm).is_file() and shutil.which(nfpm) is None:
            self.skipTest("nFPM is unavailable")
        (self.root / "run/openrc").mkdir(parents=True)
        self.active.write_text("php-fpm-zts\n")
        architecture = "arm64" if platform.machine() in {"arm64", "aarch64"} else "amd64"
        apk_architecture = "aarch64" if architecture == "arm64" else "x86_64"
        repo = self.directory / "repo" / apk_architecture
        repo.mkdir(parents=True)
        payload = self.directory / "library"
        config = self.directory / "package.json"
        for release in [1, 2, 3]:
            payload.write_text(f"library-{release}\n")
            config.write_text(json.dumps({"name":"php-zts-lib-fixture", "arch":architecture, "version":"1.0.0", "release":str(release), "maintainer":"Test <test@example.invalid>", "description":"fixture", "license":"MIT", "contents":[{"src":str(payload), "dst":"/usr/lib/php-zts/lib/libfixture.so.1"}]}))
            subprocess.run([nfpm, "package", "--config", str(config), "--packager", "apk", "--target", str(repo / f"php-zts-lib-fixture-1.0.0-r{release}.apk")], check=True, capture_output=True)
        (self.directory / "repositories").write_text("/work/repo\n")
        shutil.copyfile(HELPER, self.directory / "runtime-services")
        shutil.copyfile(ROOT / "src/runtime/apk-commit-hook", self.directory / "commit-hook")
        script = """set -eu
export PATH=/work/bin:$PATH
export SPP_TEST_LOG=/work/calls SPP_TEST_ACTIVE=/work/active
export SPP_TEST_VALIDATE_FAIL=/work/validation-failure SPP_TEST_VALIDATE_WARN=/work/validation-warning SPP_TEST_RESTART_FAIL=/work/restart-failure
cp -a /work/root/. /
mkdir -p /usr/libexec/php-zts /etc/apk/commit_hooks.d
cp /work/runtime-services /usr/libexec/php-zts/runtime-services
cat > /etc/apk/commit_hooks.d/php-zts-runtime <<'HOOK'
#!/bin/sh
export PATH=/work/bin:/usr/sbin:/usr/bin:/sbin:/bin
export SPP_TEST_LOG=/work/calls SPP_TEST_ACTIVE=/work/active
export SPP_TEST_VALIDATE_FAIL=/work/validation-failure SPP_TEST_VALIDATE_WARN=/work/validation-warning SPP_TEST_RESTART_FAIL=/work/restart-failure
exec sh /work/commit-hook "$@"
HOOK
chmod 755 /usr/libexec/php-zts/runtime-services /etc/apk/commit_hooks.d/php-zts-runtime
cd /work/repo/ARCH
client() { apk --allow-untrusted --no-network --repositories-file /work/repositories "$@"; }
apk index --allow-untrusted -o APKINDEX.tar.gz php-zts-lib-fixture-1.0.0-r1.apk
client add php-zts-lib-fixture
: > /work/calls
apk index --allow-untrusted -o APKINDEX.tar.gz php-zts-lib-fixture-1.0.0-r2.apk
client update
client upgrade
[ "$(grep -c '^openrc restart php-fpm-zts$' /work/calls)" = 1 ]
client upgrade
[ "$(grep -c '^openrc restart php-fpm-zts$' /work/calls)" = 1 ]
: > /work/active
apk index --allow-untrusted -o APKINDEX.tar.gz php-zts-lib-fixture-1.0.0-r3.apk
client update
client upgrade
[ "$(grep -c '^openrc restart php-fpm-zts$' /work/calls)" = 1 ]
echo native-runtime-commit-hook-passed
""".replace("ARCH", apk_architecture)
        container = "spp-runtime-service-" + self.directory.name
        try:
            result = subprocess.run(["docker", "run", "--rm", "--name", container, "--network", "none", "-v", str(self.directory) + ":/work", NATIVE_IMAGE, "sh", "-c", script], capture_output=True, text=True, timeout=60)
        finally:
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + "\nService calls:\n" + self.log.read_text())
        self.assertIn("native-runtime-commit-hook-passed", result.stdout)


class RuntimeServicePackageTests(unittest.TestCase):
    def test_fpm_generated_debian_postinst_queues_install_and_upgrade(self):
        if shutil.which("fpm") is None:
            self.skipTest("FPM is required to inspect its real DEB maintainer scripts")
        with tempfile.TemporaryDirectory(prefix="spp-fpm-service-script-") as temporary:
            directory = Path(temporary)
            code = r"""define('BASE_PATH', $argv[1]); define('TEMP_DIR', $argv[2]);
require BASE_PATH . '/src/util/RuntimeServices.php';
$class = 'staticphp\\util\\RuntimeServices';
echo json_encode(['deb'=>$class::serviceArgs('deb','php-fpm-zts'),
'rpm'=>$class::serviceArgs('rpm','php-fpm-zts')]);"""
            generated = subprocess.run(["php", "-r", code, str(ROOT), temporary], capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
            arguments = json.loads(generated.stdout)
            self.assertIn("--before-upgrade", arguments["rpm"])
            data = directory / "data"
            data.write_text("service trigger fixture\n")
            archive = directory / "fixture.deb"
            packaged = subprocess.run(["fpm", "-s", "dir", "-t", "deb", "--name", "spp-service-trigger-fixture",
                "--version", "1", "--architecture", "all", "--maintainer", "Fixture <fixture@example.invalid>",
                "--package", str(archive), *arguments["deb"], str(data) + "=/usr/share/spp-service-trigger-fixture"],
                capture_output=True, text=True)
            self.assertEqual(packaged.returncode, 0, packaged.stdout + packaged.stderr)
            members = subprocess.check_output(["ar", "t", str(archive)], text=True).splitlines()
            control = next(name.strip() for name in members if name.strip().startswith("control.tar"))
            with tarfile.open(fileobj=io.BytesIO(subprocess.check_output(["ar", "p", str(archive), control]))) as tar:
                postinst = next(member for member in tar if member.name.lstrip("./") == "postinst")
                script = directory / "postinst"
                script.write_bytes(tar.extractfile(postinst).read())
            mockbin = directory / "bin"
            mockbin.mkdir()
            calls = directory / "calls"
            mock = mockbin / "dpkg-trigger"
            mock.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SPP_TRIGGER_CALLS"\n')
            mock.chmod(0o755)
            environment = dict(os.environ, PATH=str(mockbin) + ":" + os.environ["PATH"], SPP_TRIGGER_CALLS=str(calls))
            for native_arguments in [["configure"], ["configure", "0.9-1"]]:
                with self.subTest(native_arguments=native_arguments):
                    calls.write_text("")
                    installed = subprocess.run(["sh", str(script), *native_arguments], env=environment, capture_output=True, text=True)
                    self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                    self.assertEqual(calls.read_text().splitlines(), ["--no-await php-zts-runtime"], script.read_text())
            self.assertNotIn("--before-upgrade", arguments["deb"])

    def test_generated_layout_queues_configure_and_finalizes_only_when_triggered(self):
        with tempfile.TemporaryDirectory(prefix="spp-layout-service-script-") as temporary:
            directory = Path(temporary)
            code = r"""define('BASE_PATH', $argv[1]); define('TEMP_DIR', $argv[2]);
require BASE_PATH . '/src/util/RuntimeServices.php';
echo json_encode(staticphp\util\RuntimeServices::layoutArgs('deb'));"""
            generated = subprocess.run(["php", "-r", code, str(ROOT), temporary], capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
            arguments = json.loads(generated.stdout)
            mockbin = directory / "bin"
            mockbin.mkdir()
            calls = directory / "calls"
            calls.write_text("")
            for name, label in [("dpkg-trigger", "queue"), ("runtime-services", "finish")]:
                mock = mockbin / name
                mock.write_text('#!/bin/sh\nprintf "' + label + ' %s\\n" "$*" >> "$SPP_TRIGGER_CALLS"\n')
                mock.chmod(0o755)
            staged = directory / "postinst"
            staged.write_text(Path(arguments[1]).read_text().replace("/usr/libexec/php-zts/runtime-services", str(mockbin / "runtime-services")))
            environment = dict(os.environ, PATH=str(mockbin) + ":" + os.environ["PATH"], SPP_TRIGGER_CALLS=str(calls))
            configured = subprocess.run(["sh", str(staged), "configure"], env=environment, capture_output=True, text=True)
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            self.assertEqual(calls.read_text().splitlines(), ["queue --no-await php-zts-runtime"])
            triggered = subprocess.run(["sh", str(staged), "triggered", "php-zts-runtime"], env=environment, capture_output=True, text=True)
            self.assertEqual(triggered.returncode, 0, triggered.stdout + triggered.stderr)
            self.assertEqual(calls.read_text().splitlines(), ["queue --no-await php-zts-runtime", "finish finish"])

    def test_generated_package_integration(self):
        with tempfile.TemporaryDirectory(prefix="spp-runtime-scripts-") as temporary:
            code = r'''define('BASE_PATH', $argv[1]); define('TEMP_DIR', $argv[2]);
require BASE_PATH . '/src/util/RuntimeServices.php';
$class = 'staticphp\\util\\RuntimeServices';
$rpm = $class::layoutArgs('rpm'); $deb = $class::layoutArgs('deb');
$apk = $class::apkScripts('php-fpm-zts');
$descriptor = $class::descriptor('fpm', 'php-fpm-zts', '/usr/sbin/php-fpm-zts', '/etc/php-zts/php-fpm.conf');
echo json_encode(['rpm'=>$rpm,'deb'=>$deb,'apk'=>$apk,'descriptor'=>$descriptor,'apk_layout'=>$class::layoutConfig('apk')]);'''
            result = subprocess.run(["php", "-r", code, str(ROOT), temporary], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            generated = json.loads(result.stdout)
            self.assertIn("%transfiletriggerin", Path(generated["rpm"][1]).read_text())
            self.assertIn("/usr/lib64/php-zts", Path(generated["rpm"][1]).read_text())
            self.assertIn("--deb-interest-noawait", generated["deb"])
            self.assertIn("/usr/lib/php-zts", generated["deb"])
            self.assertIn("triggered)", Path(generated["deb"][1]).read_text())
            self.assertIn("configure)", Path(generated["deb"][1]).read_text())
            self.assertIn("preupgrade", generated["apk"])
            self.assertIn("postupgrade", generated["apk"])
            self.assertIn("/etc/apk/commit_hooks.d/php-zts-runtime", generated["apk_layout"]["files"].values())
            descriptor_path = next(iter(generated["descriptor"]))
            self.assertEqual(Path(descriptor_path).read_text(), "fpm|php-fpm-zts|/usr/sbin/php-fpm-zts|/etc/php-zts/php-fpm.conf|/usr/lib/php-zts/modules\n")
            for path in Path(temporary).glob("*.sh"):
                checked = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    import sys
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--native-image")
    arguments, remaining = parser.parse_known_args()
    NATIVE_IMAGE = arguments.native_image
    unittest.main(argv=[sys.argv[0], *remaining])
