#!/usr/bin/env python3
"""Identify library compilers and their runtime inputs without image-only tools."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


def output(arguments):
    return subprocess.check_output(arguments, stderr=subprocess.STDOUT, universal_newlines=True).strip()


def compiler_inputs():
    paths = set()
    for command in ['cc', 'gcc', 'g++', 'ld', 'as', 'ar', 'ranlib', 'strip', 'objcopy']:
        path = shutil.which(command)
        if path:
            paths.add(str(Path(path).resolve()))
    for command in ['cc1', 'cc1plus', 'lto1', 'collect2']:
        value = output(['gcc', '-print-prog-name=' + command])
        path = Path(value) if os.path.isabs(value) else Path(shutil.which(value) or value)
        if path.is_file():
            paths.add(str(path.resolve()))
    plugin = Path(output(['gcc', '-print-file-name=liblto_plugin.so']))
    if plugin.is_file():
        paths.add(str(plugin.resolve()))
    for value in list(paths):
        process = subprocess.run(['ldd', value], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        for path in re.findall(r'(?:=>\s+|^\s+)(/[^\s()]+)', process.stdout, re.M):
            if Path(path).is_file():
                paths.add(str(Path(path).resolve()))
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in sorted(paths)}


def system_headers():
    release = Path('/etc/os-release').read_text()
    if re.search(r'^ID=["\']?alpine', release, re.M):
        queries = [['apk', 'info', '-v', name] for name in ['musl-dev', 'linux-headers']]
    elif re.search(r'^ID=["\']?(?:debian|ubuntu)', release, re.M):
        queries = [['dpkg-query', '-W', '-f=${Package}:${Version}', name] for name in ['libc6-dev', 'linux-libc-dev']]
    else:
        queries = [['rpm', '-q', '--qf', '%{NAME}:%{VERSION}-%{RELEASE}', name] for name in ['glibc-devel', 'glibc-headers', 'kernel-headers']]
    values = []
    for query in queries:
        process = subprocess.run(query, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        if process.returncode == 0:
            values.append(process.stdout.strip())
    return sorted(values)


def compiler_flag_inputs(environment):
    paths = set()
    for value in environment.get('environment', {}).values():
        if not isinstance(value, str):
            continue
        tokens = iter(shlex.split(value))
        for token in tokens:
            if token in ['-specs', '--specs']:
                filename = next(tokens, '')
            elif token.startswith(('-specs=', '--specs=')):
                filename = token.split('=', 1)[1]
            else:
                continue
            if not filename:
                raise ValueError('Compiler specs flag requires a file')
            path = Path(filename)
            if not path.is_absolute() and not path.is_file():
                path = Path(output(['gcc', '-print-file-name=' + filename]))
            paths.add(str(path.absolute()))
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() if Path(name).is_file() else None for name in sorted(paths)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--build-environment', type=Path)
    args = parser.parse_args()
    environment = json.loads(args.build_environment.read_text()) if args.build_environment else {}
    print(json.dumps({'schema': 1, 'compiler': output(['gcc', '-v']), 'linker': output(['ld', '--version']), 'target': output(['gcc', '-dumpmachine']), 'files': compiler_inputs(), 'flag_files': compiler_flag_inputs(environment), 'system_headers': system_headers()}, sort_keys=True))


if __name__ == '__main__':
    main()
