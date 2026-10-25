#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import runpy
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = runpy.run_path(str(ROOT / 'tests/container-architecture.py'))
BOOTSTRAP = {
    'rpm': 'dnf -y install systemd python3 curl shadow-utils ca-certificates',
    'deb': ". /etc/os-release; if [ \"$VERSION_CODENAME\" = bullseye ]; then printf 'deb http://archive.debian.org/debian bullseye main\\ndeb http://archive.debian.org/debian-security bullseye-security main\\n' > /etc/apt/sources.list; fi; rm -rf /var/lib/apt/lists/*; apt-get -o Acquire::Check-Valid-Until=false update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd-sysv python3 curl ca-certificates; rm -f /usr/sbin/policy-rc.d",
    'apk': "apk add --no-cache openrc python3 curl ca-certificates; sed -i '/getty/d' /etc/inittab; printf '\\nrc_sys=\"docker\"\\nrc_logger=\"NO\"\\n' >> /etc/rc.conf",
}
SERVER = r'''
import http.server,os,pathlib,urllib.parse
class Repository(http.server.SimpleHTTPRequestHandler):
    def translate_path(self,path):
        relative=urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip('/')
        if not relative.startswith('repo/') or any(p in ('.','..') for p in pathlib.PurePosixPath(relative).parts):
            return '/missing'
        relative=relative[5:]
        current=pathlib.Path('/state/current').read_text().strip()
        for profile in [current,'old']:
            target=pathlib.Path('/sources')/profile/relative
            if target.exists(): return str(target)
        return '/missing'
http.server.ThreadingHTTPServer(('0.0.0.0',8080),Repository).serve_forever()
'''
FCGI = r'''<?php
function record($type,$body) { return pack('CCnnCC',1,$type,1,strlen($body),0,0).$body; }
function size($n) { return $n<128?chr($n):pack('N',$n|0x80000000); }
$socket=stream_socket_client('tcp://127.0.0.1:9074',$errno,$error,10);
if (!$socket) {fwrite(STDERR,$error);exit(1);}
$parameters=['SCRIPT_FILENAME'=>'/usr/share/php-zts-service-test/index.php','SCRIPT_NAME'=>'/index.php','REQUEST_URI'=>'/index.php','REQUEST_METHOD'=>'GET','SERVER_PROTOCOL'=>'HTTP/1.1','GATEWAY_INTERFACE'=>'CGI/1.1','REMOTE_ADDR'=>'127.0.0.1','SERVER_ADDR'=>'127.0.0.1','SERVER_NAME'=>'localhost','SERVER_PORT'=>'80'];
$body=''; foreach($parameters as $name=>$value) {$body.=size(strlen($name)).size(strlen($value)).$name.$value;}
fwrite($socket,record(1,pack('nCxxxxx',1,0)).record(4,$body).record(4,'').record(5,''));
$output='';$errors='';
function readBytes($socket,$count) {$result='';while(strlen($result)<$count){$chunk=fread($socket,$count-strlen($result));if($chunk===false||$chunk===''){throw new RuntimeException('Truncated FastCGI response');}$result.=$chunk;}return $result;}
while(true){$h=unpack('Cversion/Ctype/nid/nlength/Cpadding/Creserved',readBytes($socket,8));$body=readBytes($socket,$h['length']);if($h['padding'])readBytes($socket,$h['padding']);if($h['type']===6)$output.=$body;if($h['type']===7)$errors.=$body;if($h['type']===3)break;}
if($errors!==''){fwrite(STDERR,$errors);exit(1);}echo $output;
'''


class Harness:
    def __init__(self, args):
        self.args = args
        self.temporary = tempfile.TemporaryDirectory(prefix='spp-real-services-')
        self.directory = Path(self.temporary.name)
        self.token = self.directory.name.rsplit('-', 1)[-1]
        self.containers = []
        self.images = []
        self.network = None
        self.execution = None
        self.runtime_evidence = []
        self.report = {'schema': 1, 'type': args.type, 'php_stream': args.php_stream, 'scenarios': [], 'commands': [], 'real_service_manager': True, 'probe_only': args.probe_only, 'scope': 'Real service lifecycle; local signed repository URL. Production URL migration is validated separately.', 'host_cgroup_bind': False, 'public_changes': False, 'complete': False}

    def command(self, command, check=True, timeout=1800):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        self.report['commands'].append({'command': command, 'returncode': result.returncode, 'stdout_tail': result.stdout[-2500:], 'stderr_tail': result.stderr[-2500:]})
        if check and result.returncode:
            raise RuntimeError('Command failed: ' + ' '.join(command) + '\n' + result.stdout[-2500:] + result.stderr[-2500:])
        return result

    def docker(self, *arguments, **kwargs):
        resource_role = kwargs.pop('resource_role', 'client')
        if arguments[0] != 'run':
            return self.command(['docker', *arguments], **kwargs)
        identity = ARCHITECTURE['engine_identity'](self.command, getattr(self.args, 'arch', None),
                                                  getattr(self.args, 'allow_emulation', False))
        if self.execution is not None and identity != self.execution:
            raise RuntimeError('Docker engine/execution mode changed during runtime validation')
        self.execution = identity
        self.report.update(execution=identity, native=identity['native'], emulated=identity['emulated'],
                           runtime_evidence=self.runtime_evidence)
        limits = ARCHITECTURE['resource_limits'](resource_role)
        result = self.command(['docker', 'run', *ARCHITECTURE['container_options'](identity),
                               *ARCHITECTURE['resource_options'](limits), *arguments[1:]], **kwargs)
        self.runtime_evidence.append(ARCHITECTURE['container_identity'](self.command, result.stdout.strip(), identity, limits))
        return result

    def execute(self, container, *arguments, **kwargs):
        return self.docker('exec', container, 'env', '-u', 'LD_LIBRARY_PATH', '-u', 'PHPRC', '-u', 'PHP_INI_SCAN_DIR', *arguments, **kwargs)

    def write(self, container, path, contents):
        source = self.directory / ('write-' + str(len(self.report['commands'])))
        source.write_text(contents)
        self.execute(container, 'mkdir', '-p', str(Path(path).parent))
        self.docker('cp', str(source), container + ':' + path)

    def bootstrap(self):
        name = 'spp-services-bootstrap-' + self.token
        self.containers.append(name)
        self.docker('run', '-d', '--name', name, self.args.image, 'sleep', 'infinity', resource_role='repo')
        self.execute(name, 'sh', '-ec', BOOTSTRAP[self.args.type])
        image = 'spp-services-' + self.token + ':init'
        self.images.append(image)
        self.docker('commit', '--change', 'ENV container=docker', name, image)
        self.docker('rm', '-f', name)
        self.containers.remove(name)
        return image

    def boot(self, image, scenario):
        name = 'spp-services-' + self.token + '-' + scenario
        self.containers.append(name)
        options = ['run', '-d', '--name', name, '--tmpfs', '/run', '--tmpfs', '/run/lock', '--tmpfs', '/tmp']
        if self.network:
            options += ['--network', self.network]
        if self.args.type != 'apk':
            options += ['--privileged', '--cgroupns=private', '--stop-signal', 'SIGRTMIN+3']
        self.docker(*options, image, '/sbin/init')
        for _ in range(40):
            if self.args.type == 'apk':
                ready = self.execute(name, 'rc-status', '--runlevel', check=False, timeout=5).stdout.strip() == 'default'
            else:
                ready = self.execute(name, 'systemctl', 'is-system-running', check=False, timeout=5).stdout.strip() in ('running', 'degraded')
            if ready:
                return name
            if self.docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip() != 'true':
                break
            time.sleep(.5)
        raise RuntimeError('Init did not become ready: ' + self.docker('logs', '--tail', '60', name, check=False).stdout)

    def repositories(self, image):
        self.network = 'spp-services-' + self.token
        self.docker('network', 'create', self.network)
        state = self.directory / 'state'
        state.mkdir()
        (state / 'server.py').write_text(SERVER)
        (state / 'current').write_text('old')
        name = 'spp-services-repo-' + self.token
        self.containers.append(name)
        arguments = ['run', '-d', '--name', name, '--network', self.network, '--network-alias', 'repo', '-v', str(state) + ':/state:ro']
        for profile, directory in [('old', self.args.old_repo), ('candidate', self.args.candidate_repo), ('library', self.args.library_repo)]:
            arguments += ['-v', str(directory.resolve()) + ':/sources/' + profile + ':ro']
        self.docker(*arguments, image, 'python3', '/state/server.py', resource_role='repo')
        self.server_state = state / 'current'

    def select(self, profile):
        temporary = self.server_state.with_suffix('.new')
        temporary.write_text(profile)
        temporary.replace(self.server_state)

    def configure_repository(self, container):
        key = self.args.key.resolve()
        if self.args.type == 'rpm':
            self.docker('cp', str(key), container + ':/etc/pki/rpm-gpg/php-zts-test-key')
            self.execute(container, 'rpm', '--import', '/etc/pki/rpm-gpg/php-zts-test-key')
            self.write(container, '/etc/yum.repos.d/php-zts-service-test.repo', '[php-zts-service-test]\nname=PHP ZTS service upgrade test\nbaseurl=http://repo:8080/repo/\nenabled=1\ngpgcheck=1\nmetadata_expire=0\ngpgkey=file:///etc/pki/rpm-gpg/php-zts-test-key\n')
            self.execute(container, 'dnf', '-y', 'module', 'enable', 'php-zts:static-' + self.args.php_stream)
        elif self.args.type == 'deb':
            suffix = '.asc' if b'BEGIN PGP PUBLIC KEY' in key.read_bytes() else '.gpg'
            destination = '/usr/share/keyrings/php-zts-service-test' + suffix
            self.docker('cp', str(key), container + ':' + destination)
            distribution = self.args.deb_distribution
            self.write(container, '/etc/apt/sources.list.d/php-zts-service-test.list', 'deb [signed-by=' + destination + '] http://repo:8080/repo/ ' + distribution + '\n')
            self.execute(container, 'apt-get', 'update')
        else:
            self.docker('cp', str(key), container + ':/etc/apk/keys/' + key.name)
            self.execute(container, 'sh', '-ec', "printf '\\nhttp://repo:8080/repo\\n' >> /etc/apk/repositories")
            self.execute(container, 'apk', 'update')

    def install_old(self, container):
        packages = [self.args.prefix + '-fpm', self.args.frankenphp]
        if self.args.type == 'rpm':
            self.execute(container, 'dnf', '-y', 'install', *packages)
        elif self.args.type == 'deb':
            self.execute(container, 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-y', 'install', *packages)
        else:
            self.execute(container, 'apk', 'add', *packages)

    def upgrade(self, container):
        if self.args.type == 'rpm':
            self.execute(container, 'dnf', '-y', 'upgrade')
        elif self.args.type == 'deb':
            self.execute(container, 'apt', 'update')
            self.execute(container, 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt', '-y', 'upgrade')
        else:
            self.execute(container, 'apk', 'update')
            self.execute(container, 'apk', 'upgrade')

    def service(self, container, service, operation, check=True):
        if self.args.type == 'apk':
            if operation in ('enable', 'disable'):
                return self.execute(container, 'rc-update', 'add' if operation == 'enable' else 'del', service, 'default', check=check)
            return self.execute(container, 'rc-service', service, operation, check=check)
        return self.execute(container, 'systemctl', operation, service + '.service', check=check)

    def active(self, container, service):
        operation = 'status' if self.args.type == 'apk' else 'is-active'
        return self.service(container, service, operation, check=False).returncode == 0

    def enabled(self, container, service):
        if self.args.type == 'apk':
            return self.execute(container, 'test', '-L', '/etc/runlevels/default/' + service, check=False).returncode == 0
        return self.service(container, service, 'is-enabled', check=False).returncode == 0

    def configure_services(self, container):
        confdir = '/etc/' + self.args.prefix
        if self.args.type == 'apk' and self.execute(container, 'test', '-f', '/etc/init.d/' + self.service_names[0], check=False).returncode:
            unit = (ROOT / 'src/ini/php-fpm.openrc').read_text().replace('@binary_suffix@', self.args.binary_suffix).replace('@confdir@', confdir)
            self.write(container, '/etc/init.d/' + self.service_names[0], unit)
            self.execute(container, 'chmod', '0755', '/etc/init.d/' + self.service_names[0])
            self.report['legacy_fpm_openrc_fixture'] = 'Old APK has no FPM OpenRC unit; this controlled real unit starts its actual old binary before migration.'
        for service in self.service_names:
            self.service(container, service, 'stop', check=False)
        self.write(container, confdir + '/fpm.d/www.conf', '[www]\nuser=frankenphp\ngroup=frankenphp\nlisten=127.0.0.1:9074\npm=static\npm.max_children=2\ncatch_workers_output=yes\n')
        self.write(container, '/etc/frankenphp/Caddyfile', '{\n admin off\n auto_https off\n frankenphp\n}\n:8087 {\n root * /usr/share/php-zts-service-test\n php_server\n}\n')
        self.write(container, '/usr/share/php-zts-service-test/index.php', '<?php echo "SPP_SERVICE_OK";\n')
        self.write(container, '/tmp/fcgi.php', FCGI)
        self.execute(container, 'chmod', '0755', '/usr/share/php-zts-service-test')
        self.execute(container, 'chmod', '0644', '/usr/share/php-zts-service-test/index.php')
        for service in self.service_names:
            self.service(container, service, 'enable')
            self.service(container, service, 'start')

    @property
    def service_names(self):
        return ['php-fpm' + self.args.binary_suffix, self.args.frankenphp]

    def versions(self, container):
        if self.args.type == 'rpm':
            raw = self.execute(container, 'rpm', '-qa', '--qf', '%{NAME}|%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n').stdout
        elif self.args.type == 'deb':
            raw = self.execute(container, 'dpkg-query', '-W', '-f', '${Package}|${Version}\n').stdout
        else:
            program = "import pathlib; print('\\n'.join(next(x[2:] for x in p.splitlines() if x.startswith('P:'))+'|'+next(x[2:] for x in p.splitlines() if x.startswith('V:')) for p in pathlib.Path('/lib/apk/db/installed').read_text().split('\\n\\n') if p.strip()))"
            raw = self.execute(container, 'python3', '-c', program).stdout
        return {name: version for name, version in (line.split('|', 1) for line in raw.splitlines()) if name.startswith(self.args.prefix) or name.startswith(self.args.frankenphp) or name.startswith('php-zts-lib-') or name == 'php-zts-runtime-layout'}

    def inspect(self, container, service):
        if self.args.type == 'apk':
            pidfile = '/run/' + service + ('/openrc.pid' if service.startswith('php-fpm') else '/' + service + '.pid')
            pid = int(self.execute(container, 'cat', pidfile).stdout.strip())
        else:
            pid = int(self.execute(container, 'systemctl', 'show', '-p', 'MainPID', '--value', service + '.service').stdout.strip())
        if pid <= 1:
            raise RuntimeError('Invalid managed service PID: ' + service)
        executable = ('/usr/sbin/' if service.startswith('php-fpm') else '/usr/bin/') + service
        program = r'''import pathlib,os,json,sys
p=int(sys.argv[1]); rows=[]; expected=sys.argv[2]
actual=os.readlink('/proc/%d/exe'%p)
if actual.endswith(' (deleted)'): raise RuntimeError('Deleted service executable: '+actual)
mapped=os.stat('/proc/%d/exe'%p); installed=os.stat(expected)
if (mapped.st_dev,mapped.st_ino)!=(installed.st_dev,installed.st_ino): raise RuntimeError('Stale service executable: '+actual)
for line in pathlib.Path('/proc/%d/maps'%p).read_text().splitlines():
    fields=line.split(None,5)
    if len(fields)<6: continue
    name=fields[5]
    if '/php-zts/' not in name and '/libphp-zts' not in name: continue
    if name.endswith(' (deleted)'): raise RuntimeError('Deleted runtime mapping: '+name)
    st=os.stat(name)
    if st.st_ino != int(fields[4]): raise RuntimeError('Stale runtime inode: '+name)
    rows.append({'path':name,'inode':st.st_ino})
print(json.dumps({'pid':p,'exe':actual,'exe_inode':mapped.st_ino,'runtime_mappings':rows}))'''
        return json.loads(self.execute(container, 'python3', '-c', program, str(pid), executable).stdout)

    def health(self, container, expected_active, expected_enabled=None):
        expected_enabled = expected_enabled or {service: True for service in self.service_names}
        for _ in range(30):
            try:
                result = {}
                for service in self.service_names:
                    if not expected_active[service]:
                        enabled = self.enabled(container, service)
                        if self.active(container, service) or enabled != expected_enabled[service]:
                            raise RuntimeError('Stopped service or its enablement changed state: ' + service)
                        result[service] = {'active': False, 'enabled': enabled}
                        continue
                    if not self.active(container, service):
                        raise RuntimeError('Service is inactive: ' + service)
                    if self.enabled(container, service) != expected_enabled[service]:
                        raise RuntimeError('Active service enablement changed: ' + service)
                    if service.startswith('php-fpm'):
                        body = self.execute(container, '/usr/bin/php' + self.args.binary_suffix, '-n', '/tmp/fcgi.php', timeout=15).stdout
                    else:
                        body = self.execute(container, 'curl', '--fail', '--silent', '--max-time', '10', 'http://127.0.0.1:8087/').stdout
                    if 'SPP_SERVICE_OK' not in body:
                        raise RuntimeError('Incorrect service response: ' + service)
                    result[service] = dict(self.inspect(container, service), active=True, enabled=self.enabled(container, service), response_sha256=hashlib.sha256(body.encode()).hexdigest())
                return result
            except (RuntimeError, ValueError):
                time.sleep(.5)
        raise RuntimeError('Services did not reach expected healthy state')

    def scenario(self, image, scenario):
        self.select('old')
        container = self.boot(image, scenario)
        row = {'scenario': scenario}
        self.report['scenarios'].append(row)
        self.configure_repository(container)
        self.install_old(container)
        self.configure_services(container)
        active = {name: True for name in self.service_names}
        enabled = {name: True for name in self.service_names}
        if scenario == 'stopped':
            self.service(container, self.args.frankenphp, 'stop')
            active[self.args.frankenphp] = False
        row['old_versions'] = self.versions(container)
        row['old_services'] = self.health(container, active, enabled)
        pool = '/etc/' + self.args.prefix + '/fpm.d/www.conf'
        old_pool = self.execute(container, 'sha256sum', pool).stdout.split()[0]
        old_caddy = self.execute(container, 'sha256sum', '/etc/frankenphp/Caddyfile').stdout.split()[0]
        self.select('candidate')
        self.upgrade(container)
        row['upgraded_versions'] = self.versions(container)
        for package in [self.args.prefix + '-cli', self.args.prefix + '-fpm', self.args.frankenphp]:
            if row['upgraded_versions'].get(package) == row['old_versions'].get(package):
                raise RuntimeError('Normal upgrade held a runtime package back: ' + package)
        row['upgraded_services'] = self.health(container, active, enabled)
        if any(active[service] and not row['upgraded_services'][service]['runtime_mappings'] for service in self.service_names):
            raise RuntimeError('Upgraded service has no private shared runtime mappings')
        for service in self.service_names:
            if active[service] and row['old_services'][service]['pid'] == row['upgraded_services'][service]['pid']:
                raise RuntimeError('Service did not replace its runtime after core upgrade: ' + service)
        if old_pool != self.execute(container, 'sha256sum', pool).stdout.split()[0]:
            raise RuntimeError('Edited FPM pool configuration changed')
        if old_caddy != self.execute(container, 'sha256sum', '/etc/frankenphp/Caddyfile').stdout.split()[0]:
            raise RuntimeError('Edited Caddyfile changed')
        if scenario == 'stopped':
            self.service(container, self.args.frankenphp, 'start')
            self.service(container, self.args.frankenphp, 'enable')
            self.service(container, self.service_names[0], 'stop')
            self.service(container, self.service_names[0], 'disable')
            active = {self.service_names[0]: False, self.args.frankenphp: True}
            enabled[self.service_names[0]] = False
        before_library = self.health(container, active, enabled)
        self.select('library')
        self.upgrade(container)
        row['library_updated_versions'] = self.versions(container)
        changed = {name for name, version in row['library_updated_versions'].items() if row['upgraded_versions'].get(name) != version}
        if not changed or any(not name.startswith('php-zts-lib-') for name in changed):
            raise RuntimeError('Expected a library-only update; changed packages: ' + ','.join(sorted(changed)))
        row['library_services'] = self.health(container, active, enabled)
        if old_pool != self.execute(container, 'sha256sum', pool).stdout.split()[0] or old_caddy != self.execute(container, 'sha256sum', '/etc/frankenphp/Caddyfile').stdout.split()[0]:
            raise RuntimeError('Library-only update changed edited service configuration')
        if any(active[service] and not row['library_services'][service]['runtime_mappings'] for service in self.service_names):
            raise RuntimeError('Library-updated service has no private shared runtime mappings')
        for service in self.service_names:
            if active[service] and before_library[service]['pid'] == row['library_services'][service]['pid']:
                raise RuntimeError('Library-only update left the old service process running: ' + service)
        self.upgrade(container)
        row['noop_services'] = self.health(container, active, enabled)
        for service in self.service_names:
            if active[service] and row['library_services'][service]['pid'] != row['noop_services'][service]['pid']:
                raise RuntimeError('No-op upgrade restarted service: ' + service)
        row['passed'] = True
        self.docker('rm', '-f', container)
        self.containers.remove(container)

    def cleanup(self):
        receipt = {'containers': {}, 'images': {}, 'network': None}
        def remove(*arguments):
            try:
                result = self.docker(*arguments, check=False, timeout=30)
                return result.returncode == 0 or 'No such' in result.stderr
            except BaseException as error:
                return str(error)
        for container in reversed(self.containers):
            receipt['containers'][container] = remove('rm', '-f', container)
        if self.network:
            receipt['network'] = remove('network', 'rm', self.network)
        for image in reversed(self.images):
            receipt['images'][image] = remove('image', 'rm', '-f', image)
        try:
            self.temporary.cleanup()
        except OSError as error:
            receipt['temporary_directory_error'] = str(error)
        receipt['temporary_directory_removed'] = not self.directory.exists()
        self.report['cleanup'] = receipt


def main():
    parser = argparse.ArgumentParser(description='Real init-system PHP service upgrade validation; never mounts host cgroups or changes host services.')
    parser.add_argument('--type', choices=['rpm', 'deb', 'apk'], required=True)
    parser.add_argument('--image')
    parser.add_argument('--arch', choices=['x86_64', 'arm64'])
    parser.add_argument('--allow-emulation', action='store_true', help='Allow explicitly labeled local QEMU validation; CI requires a native engine')
    parser.add_argument('--old-repo', type=Path)
    parser.add_argument('--candidate-repo', type=Path)
    parser.add_argument('--library-repo', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--deb-distribution', default='./')
    parser.add_argument('--php-stream', default='8.6')
    parser.add_argument('--prefix', default='php-zts')
    parser.add_argument('--binary-suffix', default='-zts')
    parser.add_argument('--frankenphp', default='frankenphp')
    parser.add_argument('--scenario', choices=['running', 'stopped', 'all'], default='all')
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.image = args.image or {'rpm': 'almalinux:8', 'deb': 'debian:11', 'apk': 'alpine:3.21'}[args.type]
    if not args.probe_only and any(not path or not path.exists() for path in [args.old_repo, args.candidate_repo, args.library_repo, args.key]):
        parser.error('Provide existing signed old/candidate/library repository directories and their public key')
    harness = Harness(args)
    status = 1
    try:
        image = harness.bootstrap()
        if args.probe_only:
            container = harness.boot(image, 'probe')
            harness.report['pid1'] = harness.execute(container, 'cat', '/proc/1/comm').stdout.strip()
        else:
            harness.repositories(image)
            for scenario in ['running', 'stopped'] if args.scenario == 'all' else [args.scenario]:
                harness.scenario(image, scenario)
        harness.report['complete'] = True
        harness.report['state'] = 'passed'
        status = 0
    except BaseException as error:
        harness.report['state'] = 'failed'
        harness.report['error'] = type(error).__name__ + ': ' + str(error)
        harness.report['diagnostics'] = {}
        for container in harness.containers:
            try:
                logs = harness.docker('logs', '--tail', '60', container, check=False, timeout=10)
                diagnostic = {'container_logs': (logs.stdout + logs.stderr)[-6000:]}
                if args.type != 'apk':
                    journal = harness.execute(container, 'journalctl', '--no-pager', '-n', '60', '-u', harness.service_names[0], '-u', harness.service_names[1], check=False, timeout=10)
                    diagnostic['service_journal'] = (journal.stdout + journal.stderr)[-6000:]
                harness.report['diagnostics'][container] = diagnostic
            except BaseException as diagnostic_error:
                harness.report['diagnostics'][container] = {'error': str(diagnostic_error)}
    finally:
        harness.cleanup()
        cleanup = harness.report['cleanup']
        if any(value is not True for value in [*cleanup['containers'].values(), *cleanup['images'].values(), cleanup['temporary_directory_removed']]) or (cleanup['network'] is not None and cleanup['network'] is not True):
            harness.report['state'] = 'failed'
            harness.report['cleanup_incomplete'] = True
            status = 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(harness.report, indent=2) + '\n')
    print(json.dumps({'state': harness.report['state'], 'output': str(args.output), 'cleanup': harness.report['cleanup']}))
    return status


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
