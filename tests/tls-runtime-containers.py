#!/usr/bin/env python3

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_service_containers', ROOT / 'tests/runtime-service-containers.py')
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)
BODY = 'SPP_NATIVE_TLS_TRUST_OK'
UNSET = ['LD_LIBRARY_PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'CURL_CA_BUNDLE', 'OPENSSL_CONF', 'OPENSSL_MODULES', 'OPENSSL_ENGINES', 'PHPRC', 'PHP_INI_SCAN_DIR', 'GODEBUG',
         'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']

PHP_PROBE = r'''<?php
$url=$argv[1];
$warnings=[];
set_error_handler(function($severity,$message)use(&$warnings){$warnings[]=$message;return true;});
$body=file_get_contents($url);
restore_error_handler();
$curl=curl_init($url);
curl_setopt_array($curl,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_CONNECTTIMEOUT=>5,CURLOPT_TIMEOUT=>10]);
$curlBody=curl_exec($curl);
$result=['php_version'=>PHP_VERSION,'openssl_version'=>OPENSSL_VERSION_TEXT,
'cert_locations'=>openssl_get_cert_locations(),
'ini'=>['openssl.cafile'=>ini_get('openssl.cafile'),'openssl.capath'=>ini_get('openssl.capath'),'curl.cainfo'=>ini_get('curl.cainfo')],
'curl_version'=>curl_version(),'stream'=>['success'=>$body!==false,'body'=>$body,'warnings'=>$warnings],
'curl'=>['success'=>$curlBody!==false,'body'=>$curlBody,'errno'=>curl_errno($curl),'error'=>curl_error($curl)],
'private_library_root'=>realpath('/usr/lib/php-zts/lib'),
'openssl_maps'=>array_values(array_filter(explode("\n",file_get_contents('/proc/self/maps')),fn($line)=>preg_match('~/lib(?:ssl|crypto)\.so~',$line))),
'provider_maps'=>array_values(array_filter(explode("\n",file_get_contents('/proc/self/maps')),fn($line)=>str_contains($line,'ossl-modules/')))];
echo json_encode($result,JSON_THROW_ON_ERROR|JSON_PRETTY_PRINT)."\n";
'''

OPENSSL_PROBE = r'''
import ctypes,glob,json,os,pathlib,subprocess
paths=glob.glob('/usr/lib/php-zts/lib/libcrypto.so.*')
if not paths: raise RuntimeError('Private OpenSSL libcrypto is missing')
path=next((p for p in paths if os.path.islink(p)),paths[0])
crypto=ctypes.CDLL(path)
crypto.OPENSSL_info.argtypes=[ctypes.c_int];crypto.OPENSSL_info.restype=ctypes.c_char_p
crypto.OPENSSL_init_crypto.argtypes=[ctypes.c_uint64,ctypes.c_void_p];crypto.OPENSSL_init_crypto.restype=ctypes.c_int
crypto.CONF_modules_load_file.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong];crypto.CONF_modules_load_file.restype=ctypes.c_int
crypto.OSSL_PROVIDER_load.argtypes=[ctypes.c_void_p,ctypes.c_char_p];crypto.OSSL_PROVIDER_load.restype=ctypes.c_void_p
crypto.OSSL_PROVIDER_unload.argtypes=[ctypes.c_void_p];crypto.OSSL_PROVIDER_unload.restype=ctypes.c_int
config=crypto.OPENSSL_info(1001).decode();engines=crypto.OPENSSL_info(1002).decode();modules=crypto.OPENSSL_info(1003).decode()
if crypto.CONF_modules_load_file(None,None,0)!=1: raise RuntimeError('Private OpenSSL default configuration does not parse')
if crypto.OPENSSL_init_crypto(0x40,None)!=1: raise RuntimeError('Private OpenSSL default configuration failed')
provider=crypto.OSSL_PROVIDER_load(None,b'default')
if not provider: raise RuntimeError('Private OpenSSL default provider failed')
configuration=pathlib.Path(config)/'openssl.cnf'
mapped=[x.split()[-1] for x in pathlib.Path('/proc/self/maps').read_text().splitlines() if 'ossl-modules/' in x]
private=os.path.realpath('/usr/lib/php-zts/lib')
if any(not os.path.realpath(p).startswith(private+'/') for p in [modules,engines]): raise RuntimeError('Compiled OpenSSL plugin path escapes private library directory')
if any(not os.path.realpath(p).startswith(private+'/') for p in mapped): raise RuntimeError('Loaded an external provider: '+str(mapped))
print(json.dumps({'library':path,'config_dir':config,'config_file':str(configuration),'config_exists':configuration.is_file(),
'config_sha256':subprocess.check_output(['sha256sum',str(configuration)],universal_newlines=True).split()[0] if configuration.is_file() else None,
'engines_dir':engines,'resolved_engines_dir':os.path.realpath(engines),'modules_dir':modules,'resolved_modules_dir':os.path.realpath(modules),'default_provider_loaded':True,'mapped_provider_modules':mapped}))
crypto.OSSL_PROVIDER_unload(provider)
'''

SERVER = r'''
import http.server,pathlib,socketserver,ssl,threading
class ThreadingHTTPServer(socketserver.ThreadingMixIn,http.server.HTTPServer):
    daemon_threads=True
class TLS(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body=b'SPP_NATIVE_TLS_TRUST_OK'
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args): pass
class Repository(http.server.SimpleHTTPRequestHandler):
    def translate_path(self,path):
        translated=super().translate_path(path[5:] if path.startswith('/repo/') else '/__missing__')
        return str(pathlib.Path('/repo')/pathlib.Path(translated).relative_to(pathlib.Path.cwd()))
repository=ThreadingHTTPServer(('0.0.0.0',8080),Repository)
threading.Thread(target=repository.serve_forever,daemon=True).start()
tls=ThreadingHTTPServer(('0.0.0.0',8443),TLS)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain('/tls/server.crt','/tls/server.key')
tls.socket=context.wrap_socket(tls.socket,server_side=True)
pathlib.Path('/tls/ready').touch()
tls.serve_forever()
'''


def issuer(directory):
    environment = {key: value for key, value in os.environ.items() if key not in UNSET}
    commands = [
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=SPP ephemeral native trust test', '-addext', 'basicConstraints=critical,CA:TRUE', '-keyout', str(directory / 'ca.key'), '-out', str(directory / 'ca.crt')],
        ['openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=tls-server', '-keyout', str(directory / 'server.key'), '-out', str(directory / 'server.csr')],
        ['openssl', 'x509', '-req', '-in', str(directory / 'server.csr'), '-CA', str(directory / 'ca.crt'), '-CAkey', str(directory / 'ca.key'), '-CAcreateserial', '-days', '2', '-extfile', str(directory / 'extensions'), '-out', str(directory / 'server.crt')],
    ]
    (directory / 'extensions').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:tls-server,DNS:localhost,IP:127.0.0.1\n')
    for command in commands:
        subprocess.run(command, env=environment, check=True, capture_output=True, timeout=30)


class Harness(services.Harness):
    def __init__(self, args):
        super().__init__(args)
        self.report = {'schema': 1, 'type': args.type, 'architecture': None, 'php_stream': args.php_stream,
                       'scope': 'One trust/profile check per package format and architecture, using default PHP stream/cURL and FrankenPHP Go TLS trust; execution mode is recorded.',
                       'commands': [], 'complete': False, 'public_changes': False, 'environment_overrides': False}

    def execute(self, container, *arguments, **kwargs):
        options = [piece for key in UNSET for piece in ['-u', key]]
        return self.docker('exec', container, 'env', *options, *arguments, **kwargs)

    def native(self):
        self.network = 'spp-tls-' + self.token
        self.docker('network', 'create', self.network)
        container = 'spp-tls-' + self.token
        self.containers.append(container)
        self.docker('run', '-d', '--name', container, '--network', self.network, '--network-alias', 'repo', '--network-alias', 'tls-server',
                    '-v', str(self.args.repo.resolve()) + ':/repo:ro', self.args.image, 'sleep', 'infinity')
        bootstrap = {
            'rpm': 'dnf -y install python3 shadow-utils',
            'deb': '. /etc/os-release; if [ "$VERSION_CODENAME" = bullseye ]; then printf "deb http://archive.debian.org/debian bullseye main\\ndeb http://archive.debian.org/debian-security bullseye-security main\\n" > /etc/apt/sources.list; fi; rm -rf /var/lib/apt/lists/*; apt-get -o Acquire::Check-Valid-Until=false update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3',
            'apk': 'apk add --no-cache python3',
        }
        self.execute(container, 'sh', '-ec', bootstrap[self.args.type])
        self.report['architecture'] = self.execute(container, 'uname', '-m').stdout.strip()
        tls = self.directory / 'tls'
        tls.mkdir()
        issuer(tls)
        (tls / 'server.py').write_text(SERVER)
        self.docker('cp', str(tls), container + ':/tls')
        self.docker('exec', '-d', container, 'sh', '-c', 'exec python3 /tls/server.py > /tls/server.log 2>&1')
        self.wait(container, 'test', '-e', '/tls/ready')
        self.configure_repository(container)
        self.remove_cas(container)
        packages = [self.args.prefix + '-cli', self.args.prefix + '-curl', self.args.frankenphp]
        installer = {'rpm': ['dnf', '-y', 'install'], 'deb': ['apt-get', '-y', 'install'], 'apk': ['apk', 'add']}[self.args.type]
        self.execute(container, *installer, *packages)
        self.report['frankenphp_version'] = self.execute(container, '/usr/bin/' + self.args.frankenphp, '--version').stdout.strip()
        query = {'rpm': ['rpm', '-q', '--requires', 'php-zts-lib-openssl'], 'deb': ['dpkg-query', '-W', '-f', '${Depends}', 'php-zts-lib-openssl'], 'apk': ['apk', 'info', '-R', 'php-zts-lib-openssl']}[self.args.type]
        relationships = self.execute(container, *query).stdout
        if 'ca-certificates' not in relationships:
            raise RuntimeError('Private OpenSSL package header has no hard ca-certificates dependency')
        self.report['openssl_dependencies'] = relationships
        if not self.ca_installed(container):
            raise RuntimeError('Installing the private OpenSSL runtime did not install ca-certificates')
        self.report['ca_dependency_installed'] = True
        self.write(container, '/tls/client.php', PHP_PROBE)
        self.report['openssl_defaults'] = json.loads(self.execute(container, 'python3', '-c', OPENSSL_PROBE).stdout)
        expected = '/etc/pki/tls' if self.args.type == 'rpm' else '/etc/ssl'
        if self.report['openssl_defaults']['config_dir'] != expected:
            raise RuntimeError('OpenSSL compiled configuration directory differs from the native profile')
        if not self.report['openssl_defaults']['config_exists']:
            raise RuntimeError('The default native OpenSSL configuration file is missing')
        self.report['before_trust'] = self.probe(container, False)
        self.start_franken(container)
        self.report['franken_before_trust'] = self.franken_response(container, False)
        self.stop_franken(container)
        destination = '/etc/pki/ca-trust/source/anchors/spp-ephemeral.crt' if self.args.type == 'rpm' else '/usr/local/share/ca-certificates/spp-ephemeral.crt'
        self.execute(container, 'mkdir', '-p', str(Path(destination).parent))
        self.docker('cp', str(tls / 'ca.crt'), container + ':' + destination)
        self.execute(container, 'update-ca-trust' if self.args.type == 'rpm' else 'update-ca-certificates')
        self.report['after_trust'] = self.probe(container, True)
        self.start_franken(container)
        self.report['franken_after_trust'] = self.franken_response(container, True)
        self.stop_franken(container)
        self.report['ca_sha256'] = hashlib.sha256((tls / 'ca.crt').read_bytes()).hexdigest()

    def wait(self, container, *arguments):
        for _ in range(60):
            result = self.execute(container, *arguments, check=False)
            if result.returncode == 0:
                return
            time.sleep(.1)
        raise RuntimeError('Container operation did not become ready: ' + ' '.join(arguments))

    def ca_installed(self, container):
        command = {'rpm': ['rpm', '-q', 'ca-certificates'], 'deb': ['dpkg-query', '-W', '-f', '${db:Status-Status}', 'ca-certificates'], 'apk': ['apk', 'info', '-e', 'ca-certificates']}[self.args.type]
        result = self.execute(container, *command, check=False)
        return result.returncode == 0 and (self.args.type != 'deb' or result.stdout == 'installed')

    def remove_cas(self, container):
        if self.args.type == 'rpm':
            if self.ca_installed(container):
                self.execute(container, 'rpm', '-e', '--justdb', '--nodeps', 'ca-certificates')
            self.report['ca_removal'] = 'Removed RPM ownership database entry, retaining the system trust files used by dnf HTTPS transport; runtime installation must restore the package through its hard dependency.'
        elif self.args.type == 'deb':
            self.execute(container, 'apt-get', '-y', 'purge', 'ca-certificates')
        else:
            self.execute(container, 'apk', 'del', 'ca-certificates', check=False)
        if self.ca_installed(container):
            raise RuntimeError('CA prerequisite remains installed before runtime package installation')
        self.report['ca_absent_before_runtime_install'] = True

    def probe(self, container, trusted):
        result = json.loads(self.execute(container, '/usr/bin/php' + self.args.binary_suffix, '/tls/client.php', 'https://tls-server:8443/').stdout)
        for name in ['stream', 'curl']:
            actual = result[name]
            if actual['success'] != trusted or (trusted and actual['body'] != BODY):
                raise RuntimeError(name + ' default TLS trust result differs: ' + json.dumps(actual))
        if not trusted:
            if result['curl']['errno'] != 60 or not any('certificate verify failed' in warning.lower() for warning in result['stream']['warnings']):
                raise RuntimeError('Untrusted PHP requests failed for a reason other than certificate verification: ' + json.dumps(result))
        mapped = {line.split()[-1] for line in result['openssl_maps']}
        private = result['private_library_root']
        if not private or not all(any(Path(path).name.startswith(name + '.so') for path in mapped) for name in ['libssl', 'libcrypto']):
            raise RuntimeError('PHP did not load both packaged OpenSSL libraries')
        if any(not path.startswith(private + '/') for path in mapped | {line.split()[-1] for line in result['provider_maps']}):
            raise RuntimeError('PHP loaded an OpenSSL library or provider outside the private runtime: ' + json.dumps(result))
        if not result['php_version'].startswith(self.args.php_stream + '.'):
            raise RuntimeError('Installed PHP does not match the requested stream')
        return result

    def start_franken(self, container):
        self.write(container, '/tls/Caddyfile', '{\n admin off\n auto_https off\n}\n:8089 {\n reverse_proxy https://tls-server:8443\n}\n')
        self.write(container, '/tls/franken-start', '#!/bin/sh\necho $$ > /tls/franken.pid\nexec /usr/bin/' + self.args.frankenphp + ' run --config /tls/Caddyfile --adapter caddyfile > /tls/franken.log 2>&1\n')
        self.docker('exec', '-d', container, 'env', *[piece for key in UNSET for piece in ['-u', key]], 'sh', '/tls/franken-start')
        self.wait(container, 'python3', '-c', 'import socket;s=socket.create_connection(("127.0.0.1",8089),1);s.close()')

    def stop_franken(self, container):
        self.execute(container, 'sh', '-ec', 'kill -TERM "$(cat /tls/franken.pid)"; for i in $(seq 1 100); do if ! kill -0 "$(cat /tls/franken.pid)" 2>/dev/null; then exit 0; fi; sleep .1; done; exit 1')

    def franken_response(self, container, trusted):
        program = 'import urllib.request,urllib.error,json\ntry:\n r=urllib.request.urlopen("http://127.0.0.1:8089/",timeout=10); print(json.dumps({"status":r.status,"body":r.read().decode()}))\nexcept urllib.error.HTTPError as e: print(json.dumps({"status":e.code,"body":e.read().decode()}))'
        result = json.loads(self.execute(container, 'python3', '-c', program).stdout)
        log = self.execute(container, 'cat', '/tls/franken.log').stdout
        if trusted:
            if result != {'status': 200, 'body': BODY}:
                raise RuntimeError('FrankenPHP Go TLS did not trust the native CA store: ' + json.dumps(result) + log[-3000:])
        elif result['status'] != 502 or 'certificate signed by unknown authority' not in log:
            raise RuntimeError('FrankenPHP did not reject the untrusted issuer: ' + json.dumps(result) + log[-3000:])
        return dict(result, log_tail=log[-3000:])


def self_test():
    with tempfile.TemporaryDirectory(prefix='spp-tls-fixture-') as temporary:
        directory = Path(temporary)
        issuer(directory)
        server = SERVER.replace("('0.0.0.0',8080)", "('127.0.0.1',0)")
        server = server.replace("('0.0.0.0',8443)", "('127.0.0.1',0)")
        server = server.replace("pathlib.Path('/tls/ready').touch()", "pathlib.Path('/tls/ready').write_text(str(tls.server_port))")
        server = server.replace("'/tls/", repr(str(directory) + '/')[:-1])
        process = subprocess.Popen([sys.executable, '-c', server], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready = directory / 'ready'
            for _ in range(60):
                if ready.is_file():
                    break
                if process.poll() is not None:
                    raise RuntimeError('Container TLS fixture exited: ' + ''.join(process.communicate()))
                time.sleep(.1)
            if not ready.is_file():
                raise RuntimeError('Container TLS fixture did not start')
            url = 'https://localhost:' + ready.read_text() + '/'
            try:
                urllib.request.urlopen(url, timeout=5)
                raise RuntimeError('Fixture issuer was unexpectedly trusted')
            except urllib.error.URLError as error:
                if not isinstance(error.reason, ssl.SSLCertVerificationError):
                    raise
            trusted = ssl.create_default_context(cafile=str(directory / 'ca.crt'))
            if urllib.request.urlopen(url, context=trusted, timeout=5).read().decode() != BODY:
                raise RuntimeError('Fixture trusted TLS body differs')
            source = directory / 'probe.php'
            source.write_text(PHP_PROBE)
            subprocess.run(['php', '-l', str(source)], check=True, capture_output=True)
            compile(OPENSSL_PROBE, '<openssl-probe>', 'exec')
            compile(SERVER, '<server>', 'exec')
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    print('Passed ephemeral issuer/server, untrusted rejection, trusted success, and probe syntax. Native package trust is a separate run.')


def main():
    parser = argparse.ArgumentParser(description='Native default PHP/OpenSSL/cURL and FrankenPHP Go TLS trust; one run per unique format/architecture profile.')
    parser.add_argument('--type', choices=['rpm', 'deb', 'apk'])
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--image')
    parser.add_argument('--arch', choices=['x86_64', 'arm64'])
    parser.add_argument('--allow-emulation', action='store_true', help='Allow explicitly labeled local QEMU validation; CI requires a native engine')
    parser.add_argument('--deb-distribution', default='./')
    parser.add_argument('--php-stream', default='8.6')
    parser.add_argument('--prefix', default='php-zts')
    parser.add_argument('--binary-suffix', default='-zts')
    parser.add_argument('--frankenphp', default='frankenphp')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.type is None or args.output is None or any(path is None or not path.exists() for path in [args.repo, args.key]):
        parser.error('Provide --type, signed native --repo, its public --key, and --output')
    args.image = args.image or {'rpm': 'almalinux:8', 'deb': 'debian:11', 'apk': 'alpine:3.21'}[args.type]
    args.probe_only = False
    harness = Harness(args)
    status = 1
    try:
        harness.native()
        harness.report.update(state='passed', complete=True)
        status = 0
    except BaseException as error:
        harness.report.update(state='failed', error=type(error).__name__ + ': ' + str(error))
        harness.report['diagnostics'] = {}
        for container in harness.containers:
            try:
                result = harness.execute(container, 'cat', '/tls/franken.log', check=False, timeout=10)
                harness.report['diagnostics'][container] = result.stdout[-5000:] + result.stderr[-1000:]
                server = harness.execute(container, 'cat', '/tls/server.log', check=False, timeout=10)
                harness.report['diagnostics'][container] += '\n' + server.stdout[-5000:] + server.stderr[-1000:]
            except BaseException as diagnostic:
                harness.report['diagnostics'][container] = str(diagnostic)
    finally:
        harness.cleanup()
        cleanup = harness.report['cleanup']
        if any(value is not True for value in [*cleanup['containers'].values(), *cleanup['images'].values(), cleanup['temporary_directory_removed']]) or (cleanup['network'] is not None and cleanup['network'] is not True):
            harness.report.update(state='failed', cleanup_incomplete=True)
            status = 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(harness.report, indent=2) + '\n')
    print(json.dumps({'state': harness.report['state'], 'output': str(args.output), 'cleanup': harness.report['cleanup']}))
    return status


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
