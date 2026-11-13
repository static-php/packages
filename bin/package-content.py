#!/usr/bin/env python3
"""Compare native package contents without generated build timestamps."""

import gzip
import hashlib
import io
from pathlib import Path
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET


def tar_contents(data):
    files, bodies = {}, {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        for member in archive:
            name = member.name.removeprefix('./')
            if not name:
                continue
            if name in files:
                raise RuntimeError('Duplicate native package member: ' + name)
            body = archive.extractfile(member).read() if member.isfile() else None
            bodies[name] = body
            normalized = body
            if body is not None and re.fullmatch(r'usr/share/doc/[^/]+/changelog.gz', name):
                text = gzip.decompress(body)
                match = re.fullmatch(rb'(.+\n\n  \* Package created with FPM\.\n\n -- [^\n]+?)[ ]{2}[A-Z][a-z]{2}, [^\n]+\n+', text)
                if match:
                    normalized = match[1]
            files[name] = (member.type, member.mode, member.uid, member.gid, member.linkname,
                {key: value for key, value in member.pax_headers.items() if key not in ('mtime', 'atime', 'ctime')},
                hashlib.sha256(normalized).hexdigest() if normalized is not None else None)
    return files, bodies


def deb_contents(path):
    content = Path(path).read_bytes()
    if not content.startswith(b'!<arch>\n'):
        raise RuntimeError('Invalid DEB archive')
    offset, members = 8, {}
    while offset < len(content):
        header = content[offset:offset + 60]
        if len(header) != 60 or header[58:] != b'`\n':
            raise RuntimeError('Invalid DEB member header')
        name, size = header[:16].decode().strip().rstrip('/'), int(header[48:58])
        data = content[offset + 60:offset + 60 + size]
        if name in members or len(data) != size:
            raise RuntimeError('Invalid DEB member: ' + name)
        members[name] = data
        offset += 60 + size + size % 2
    if members.get('debian-binary') != b'2.0\n':
        raise RuntimeError('Unsupported DEB format')
    def archive(prefix):
        selected = [value for name, value in members.items() if name.startswith(prefix + '.tar')]
        if len(selected) != 1:
            raise RuntimeError('Missing or ambiguous DEB ' + prefix)
        return tar_contents(selected[0])
    files, bodies = archive('data')
    control, controls = archive('control')
    if 'control' not in controls:
        raise RuntimeError('Missing DEB control metadata')
    checksums = controls.get('md5sums')
    if checksums is not None:
        names = []
        for line in checksums.decode().splitlines():
            digest, name = line.split(None, 1)
            name = name.lstrip('*')
            if bodies.get(name) is None or hashlib.md5(bodies[name]).hexdigest() != digest:
                raise RuntimeError('DEB file checksum mismatch: ' + name)
            names.append(name)
        control['md5sums'] = (*control['md5sums'][:-1], tuple(sorted(names)))
    return files, control


def rpm_contents(path):
    subprocess.run(['rpm', '-K', '--nosignature', str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    xml = subprocess.check_output(['rpm', '-qp', '--nosignature', '--xml', str(path)], stderr=subprocess.DEVNULL)
    ignored = {'Buildtime', 'Buildhost', 'Filemtimes', 'Changelogtime', 'Payloaddigest', 'Payloaddigestalt',
               'Rsaheader', 'Dsaheader', 'Sha1header', 'Sha256header', 'Headerimmutable'}
    metadata = {tag.attrib['name']: [(value.tag, value.text) for value in tag]
            for tag in ET.fromstring(xml).iter('rpmTag')
            if tag.attrib['name'] not in ignored and not tag.attrib['name'].startswith('Sig')}
    files, links = {}, {}
    with subprocess.Popen(['rpm2cpio', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        source = process.stdout
        while True:
            header = source.read(110)
            if len(header) != 110 or header[:6] not in (b'070701', b'070702'):
                raise RuntimeError('Invalid RPM payload')
            fields = [int(header[offset:offset + 8], 16) for offset in range(6, 110, 8)]
            inode, mode, uid, gid, nlink, _, size, major, minor, rmajor, rminor, length, _ = fields
            name = source.read(length)
            if len(name) != length or not name.endswith(b'\0'):
                raise RuntimeError('Invalid RPM payload name')
            name = name[:-1].decode().removeprefix('./')
            source.read(-(110 + length) % 4)
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                block = source.read(min(remaining, 1024 * 1024))
                if not block:
                    raise RuntimeError('Truncated RPM payload')
                digest.update(block)
                remaining -= len(block)
            source.read(-size % 4)
            if name == 'TRAILER!!!':
                break
            if name in files:
                raise RuntimeError('Duplicate RPM payload member: ' + name)
            files[name] = (mode, uid, gid, rmajor, rminor, digest.hexdigest())
            if nlink > 1 and mode & 0o170000 == 0o100000:
                links.setdefault((major, minor, inode), []).append(name)
        source.read()
        if process.wait():
            raise RuntimeError('Cannot read RPM payload')
    return metadata, files, sorted(sorted(names) for names in links.values())


def equivalent(left, right, kind):
    reader = {'deb': deb_contents, 'rpm': rpm_contents}[kind]
    return reader(left) == reader(right)
