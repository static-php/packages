#!/usr/bin/env python3
"""Compare native package contents without generated build timestamps."""

import gzip
import hashlib
import io
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
    files, bodies = tar_contents(subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(path)]))
    control, controls = tar_contents(subprocess.check_output(['dpkg-deb', '--ctrl-tarfile', str(path)]))
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
    return metadata


def equivalent(left, right, kind):
    reader = {'deb': deb_contents, 'rpm': rpm_contents}[kind]
    return reader(left) == reader(right)
