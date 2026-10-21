#!/usr/bin/env python3
"""Build a primary replacement DEX + resources for a compatible Messages APK.

Python 3.10+, Java 17 and APKEditor 1.4.5. No third-party Python modules.
The input APK is read-only. Output is unsigned; use the target app's signing key.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

A = '{http://schemas.android.com/apk/res/android}'
ET.register_namespace('android', A[1:-1])
RESOURCE = re.compile(r'(?<![\w])0x7f[0-9a-fA-F]{6}(?![\w])')
DESCRIPTOR = re.compile(r'L[\w$/-]+;')
MEMBER = re.compile(r'(L[\w$/-]+;)->([^\s,}\"]+)')
DEX_NAME = re.compile(r'classes(?:(\d+))?\.dex$')


class Incompatible(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Incompatible(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def write_xml(path, root):
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space='  ')
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def xml_shape(node):
    """Ignore formatting/attribute order, preserve values and significant text."""
    return [node.tag, sorted(node.attrib.items()), (node.text or '').strip(),
            [xml_shape(c) for c in node], (node.tail or '').strip()]


def xml_hash(path):
    return sha(json.dumps(xml_shape(ET.parse(path).getroot())).encode())


def public_resources(path):
    result = {}
    for item in ET.parse(path).getroot():
        if item.tag != 'public':
            continue
        key = item.get('type') + '/' + item.get('name')
        require(key not in result, 'Duplicate resource name: ' + key)
        result[key] = int(item.get('id'), 16)
    require(len(set(result.values())) == len(result), 'Duplicate resource IDs')
    require(all(r >> 24 == 0x7f for r in result.values()), 'Only package resource ID 0x7f is supported')
    return result


def code_part(line):
    # Protect quoted strings (including # and hex-looking text) and comments.
    quoted = escaped = False
    for i, char in enumerate(line):
        if char == '"' and not escaped:
            quoted = not quoted
        if char == '#' and not quoted:
            return line[:i]
        escaped = char == '\\' and not escaped
    return line


def remap_smali(text, mapping):
    """Relink integer resource operands, arrays and switch payloads, never strings.

    Converting all packed switches to sparse handles noncontiguous/reordered IDs.
    const/high16 must become const when a new ID has nonzero low 16 bits.
    """
    def number(value):
        old = int(value, 16)
        if old >> 24 != 0x7f:
            return old
        # 0x7fffffff (Integer.MAX_VALUE), for example, is not a resource.
        return mapping.get(old, old)

    def payload(match):
        kind, start, body = match.group(1), match.group(2), match.group(3)
        entries = []
        if kind == 'packed':
            key = int(start.strip(), 0)
            for line in body.splitlines():
                line = code_part(line).strip()
                if not line:
                    continue
                require(re.fullmatch(r':[\w$.-]+', line), 'Unsupported packed switch entry: ' + line)
                entries.append((number(hex(key)), line))
                key += 1
        else:
            for line in body.splitlines():
                line = code_part(line).strip()
                if not line:
                    continue
                m = re.fullmatch(r'(-?0x[0-9a-fA-F]+|-?\d+)\s*->\s*(:[\w$.-]+)', line)
                require(m is not None, 'Unsupported sparse switch entry: ' + line)
                entries.append((number(hex(int(m[1], 0))), m[2]))
        require(len({key for key, _ in entries}) == len(entries), 'Resource remap caused duplicate switch keys')
        return '    .sparse-switch\n' + ''.join(
            f'        {hex(key)} -> {label}\n' for key, label in sorted(entries)) + '    .end sparse-switch'

    text = re.sub(r'^\s*\.(packed|sparse)-switch([^\n]*)\n(.*?)^\s*\.end \1-switch',
                  payload, text, flags=re.M | re.S)
    result = []
    in_switch = False
    for raw in text.splitlines():
        line = code_part(raw).rstrip()
        stripped = line.strip()
        if not stripped:
            result.append('')
            continue
        if stripped.startswith('packed-switch '):
            line = line.replace('packed-switch ', 'sparse-switch ', 1)
        if stripped.startswith('.sparse-switch'):
            in_switch = True
        # String values and debug metadata are never resource operands.
        if not in_switch and '"' not in line and not stripped.startswith(('.line ', '.local ', '.param ', '.source ')):
            line = RESOURCE.sub(lambda m: hex(number(m[0])), line)
            high = re.match(r'(\s*)const/high16 (\w+), (0x[0-9a-fA-F]+)$', line)
            if high and int(high[3], 16) & 0xffff:
                line = f'{high[1]}const {high[2]}, {high[3]}'
        result.append(line)
        if stripped.startswith('.end sparse-switch'):
            in_switch = False
    return '\n'.join(result) + '\n'


def canonical_smali(text, mapping):
    text = remap_smali(text, mapping)
    # Disassemblers rename switch/branch labels after resource relinking. Their
    # spelling is not part of the class contract; their targets and order are.
    def labels(method):
        header = method.splitlines()[0]
        parameters = re.search(r'\((.*?)\)', header)[1]
        types = re.findall(r'\[*L[^;]+;|\[*[ZBCSIJFD]', parameters)
        count = (0 if ' static ' in header else 1) + sum(2 if t in ('J', 'D') else 1 for t in types)
        method = re.sub(r'\.registers (\d+)', lambda m: '.locals ' + str(int(m[1]) - count), method)
        cleaned = []
        for line in method.splitlines():
            line = line.strip()
            if not line or line == 'nop' or line.startswith(('.line ', '.source ', '.local ', '.end local ', '.restart local ', '.param ', '.end param')):
                continue
            cleaned.append(line)
        # Several branch/try labels may designate the same instruction. A
        # disassembler can split one label into aliases without changing code.
        lookup, position = {}, 0
        for line in cleaned:
            if re.fullmatch(r':[\w$.-]+', line):
                lookup[line] = ':pc_' + str(position)
            elif not line.startswith('.catch'):
                position += 1
        output, defined = [], set()
        for line in cleaned:
            rewritten = re.sub(r':[\w$.-]+', lambda m: lookup.get(m[0], m[0]), line)
            if line in lookup:
                if rewritten in defined:
                    continue
                defined.add(rewritten)
            output.append(rewritten)
        return '\n'.join(output)
    text = re.sub(r'^\.method .*?^\.end method', lambda m: labels(m[0]), text, flags=re.M | re.S)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line == 'nop' or line.startswith(('.line ', '.source ', '.local ', '.end local ', '.restart local ', '.param ', '.end param')):
            continue
        # const and const/high16 represent the same literal after relinking.
        line = re.sub(r'^const/(?:high16|16|4) ', 'const ', line)
        line = re.sub(r'^goto/(?:16|32) ', 'goto ', line)
        lines.append(line)
    canonical = '\n'.join(lines)
    methods = re.findall(r'^\.method .*?^\.end method', canonical, flags=re.M | re.S)
    header = re.sub(r'^\.method .*?^\.end method', '', canonical, flags=re.M | re.S).strip()
    return header + '\n' + '\n'.join(sorted(methods))


def normalized_smali(text, mapping):
    return sha(canonical_smali(text, mapping).encode())


def class_index(directory):
    result = {}
    for p in sorted(directory.rglob('*.smali')):
        text = p.read_text(encoding='utf-8')
        m = re.search(r'^\.class\s+.*?(L[^;]+;)$', text, re.M)
        require(m is not None, 'Missing class descriptor: ' + str(p))
        require(m[1] not in result, 'Input contains duplicate class: ' + m[1])
        result[m[1]] = (p, text)
    return result


def class_info(text):
    methods, fields = {}, {}
    for line in text.splitlines():
        line = code_part(line).strip()
        if line.startswith('.method '):
            parts = line.split()
            methods[parts[-1]] = parts[1:-1]
        elif line.startswith('.field '):
            parts = line.split(' = ')[0].split()
            fields[parts[-1]] = parts[1:-1]
    parents = re.findall(r'^\.(?:super|implements) (L[^;]+;)', text, re.M)
    return {'methods': methods, 'fields': fields, 'parents': parents}


def resolved_member(classes, owner, signature, seen=None):
    seen = set() if seen is None else seen
    if owner in seen or owner not in classes:
        return None
    seen.add(owner)
    cls = classes[owner]
    collection = cls['methods'] if '(' in signature else cls['fields']
    if signature in collection:
        return owner, collection[signature]
    for parent in cls['parents']:
        found = resolved_member(classes, parent, signature, seen)
        if found:
            return found
    return None


def allocate_resources(target, source, additions):
    result = dict(target)
    type_ids, maxima = {}, {}
    for key, value in target.items():
        typ = key.split('/')[0]
        type_id = (value >> 16) & 255
        require(typ not in type_ids or type_ids[typ] == type_id, 'Resource type has inconsistent IDs: ' + typ)
        type_ids[typ] = type_id
        maxima[typ] = max(maxima.get(typ, -1), value & 0xffff)
    for key in sorted(additions, key=lambda k: source[k]):
        require(key not in target, 'Target already contains an integration resource: ' + key)
        typ = key.split('/')[0]
        if typ not in type_ids:
            type_ids[typ] = max(type_ids.values(), default=0) + 1
            maxima[typ] = -1
        maxima[typ] += 1
        require(type_ids[typ] <= 255 and maxima[typ] <= 65535, 'Resource ID space exhausted')
        result[key] = 0x7f000000 | (type_ids[typ] << 16) | maxima[typ]
    return result


def verify_bundle(bundle, metadata):
    for relative, expected in metadata['files'].items():
        p = bundle / relative
        require(p.is_file() and sha(p.read_bytes()) == expected, 'Bundle file missing or changed: ' + relative)


def analyze(target, bundle, meta):
    manifest = ET.parse(target / 'AndroidManifest.xml').getroot()
    require(manifest.get('package') == 'com.android.mms', 'Expected package com.android.mms')
    sdk = manifest.find('uses-sdk')
    require(sdk is not None and int(sdk.get(A + 'minSdkVersion', '1')) >= 30,
            'This package requires an Android 11+ Messages base (minSdk >= 30)')
    require(sdk.get(A + 'targetSdkVersion') == '30', 'Only targetSdk 30 is validated by this package')
    require(manifest.find('application').get(A + 'name') == 'com.android.mms.MmsApp',
            'Different application class; host adaptation required')
    resources = target / 'resources/package_1/res'
    require(resources.is_dir(), 'Expected one APKEditor resource package')
    require(len(list((target / 'resources').glob('package_*'))) == 1, 'Multiple resource packages are unsupported')
    current = public_resources(resources / 'values/public.xml')
    source = meta['source_resources']
    allocated = allocate_resources(current, source, meta['added_resources'])
    mapping = {value: allocated[key] for key, value in source.items() if key in allocated}
    inverse = {current[key]: source[key] for key in current.keys() & source.keys()}
    classes = class_index(target / 'smali')
    require(not any(c.startswith(('Lcom/tzibbur/', 'Lcom/android/mms/tzib/')) for c in classes),
            'This APK already contains Tzibbur; start with an APK without this integration')
    for name, expected in meta['replaced_classes'].items():
        require(name in classes, 'Missing host class: ' + name)
        actual = normalized_smali(classes[name][1], inverse)
        require(actual == expected, 'Host class differs from the supported baseline: ' + name +
                '. Rebase the replacement class for this variant; no output was changed.')
    for name, expected in meta['replaced_resource_files'].items():
        p = resources / name
        require(p.is_file() and xml_hash(p) == expected, 'Host layout differs: ' + name)
    for name in meta['required_resources']:
        require(name in allocated, 'Missing resource required by the replacement: ' + name)
    for name, expected in meta['required_classes'].items():
        require(name in classes, 'Missing dependency class: ' + name)
        actual = class_info(classes[name][1])
        require(actual['parents'] == expected, 'Dependency inheritance differs: ' + name)
    info = {name: class_info(text) for name, (_, text) in classes.items()}
    for item in meta['required_members']:
        found = resolved_member(info, item['owner'], item['signature'])
        require(found is not None and found[1] == item['flags'],
                'Incompatible dependency member: ' + item['owner'] + '->' + item['signature'])
    overlay = {}
    for p in (bundle / 'payload/smali').rglob('*.smali'):
        overlay[p.relative_to(bundle / 'payload/smali')] = remap_smali(p.read_text(encoding='utf-8'), mapping)
    return manifest, resources, current, allocated, mapping, overlay


def merge_resources(resources, bundle, meta, current, allocated):
    for p in (bundle / 'payload/res').rglob('*'):
        if not p.is_file():
            continue
        relative = p.relative_to(bundle / 'payload/res')
        dest = resources / relative
        if relative.parts[0].startswith('values'):
            if relative.name == 'public.xml':
                continue
            additions = ET.parse(p).getroot()
            tree = ET.parse(dest).getroot() if dest.exists() else ET.Element('resources')
            for item in additions:
                require(not any(e.tag == item.tag and e.get('name') == item.get('name') for e in tree),
                        'Resource value collision: ' + str(relative) + ':' + str(item.get('name')))
                tree.append(copy.deepcopy(item))
            write_xml(dest, tree)
        else:
            require(not dest.exists() or relative.as_posix() in meta['replaced_resource_files'],
                    'Resource file collision: ' + str(relative))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
    public = ET.parse(resources / 'values/public.xml').getroot()
    for key in sorted(allocated.keys() - current.keys(), key=lambda k: allocated[k]):
        typ, name = key.split('/', 1)
        ET.SubElement(public, 'public', {'id': f'0x{allocated[key]:08x}', 'type': typ, 'name': name})
    write_xml(resources / 'values/public.xml', public)


def merge_manifest(manifest, bundle, version_code):
    additions = ET.parse(bundle / 'manifest-additions.xml').getroot()
    for item in additions.findall('uses-permission'):
        if not any(e.get(A + 'name') == item.get(A + 'name') for e in manifest.findall('uses-permission')):
            manifest.insert(0, copy.deepcopy(item))
    app = manifest.find('application')
    for item in additions.find('application'):
        require(not any(e.get(A + 'name') == item.get(A + 'name') for e in app),
                'Manifest component already exists: ' + str(item.get(A + 'name')))
        app.append(copy.deepcopy(item))
    original_code = int(manifest.get(A + 'versionCode'))
    version_code = original_code + 1 if version_code is None else version_code
    require(original_code < version_code <= 2100000000, '--version-code must be greater than the target version and <= 2100000000')
    manifest.set(A + 'versionCode', str(version_code))
    manifest.set(A + 'versionName', manifest.get(A + 'versionName', '') + '-tzibbur18')


def pack_apk(built, original, output):
    """Preserve every original DEX byte and place the replacement DEX first."""
    with zipfile.ZipFile(original) as orig, zipfile.ZipFile(built) as patched, zipfile.ZipFile(output, 'w') as out:
        dexes = []
        for name in orig.namelist():
            match = DEX_NAME.fullmatch(name)
            if match:
                dexes.append((int(match[1] or '1'), name))
        dexes.sort()
        require([n for n, _ in dexes] == list(range(1, len(dexes) + 1)), 'Input DEX numbering has gaps')
        require([n for n in patched.namelist() if DEX_NAME.fullmatch(n)] == ['classes.dex'],
                'The replacement exceeded one DEX; package needs rebuilding')
        for item in patched.infolist():
            if (item.filename.startswith('META-INF/') and
                    (item.filename.upper().endswith(('.SF', '.RSA', '.DSA', '.EC', '/MANIFEST.MF')))):
                continue
            if item.filename.startswith('assets/dexopt/'):
                continue  # An old baseline profile describes the old DEX positions.
            out.writestr(item, patched.read(item))
        for number, name in dexes:
            dest = f'classes{number + 1}.dex'
            out.writestr(dest, orig.read(name), compress_type=zipfile.ZIP_STORED)
    with zipfile.ZipFile(original) as orig, zipfile.ZipFile(output) as result:
        require(len(result.namelist()) == len(set(result.namelist())), 'Duplicate ZIP entries in output')
        for number, name in dexes:
            require(orig.read(name) == result.read(f'classes{number + 1}.dex'), 'Original DEX bytes changed')
        return result.read('classes.dex'), len(dexes)


def run_tool(command, args, log):
    with log.open('ab') as stream:
        proc = subprocess.run(command + args, stdout=stream, stderr=subprocess.STDOUT)
    if proc.returncode:
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
        raise Incompatible('APKEditor failed:\n' + '\n'.join(lines[-25:]))


def verify_decoded(decoded, allocated, overlay, manifest):
    actual = public_resources(decoded / 'resources/package_1/res/values/public.xml')
    require(actual == allocated, 'Rebuilt resource table differs from the requested mapping')
    classes = class_index(decoded / 'smali/classes')
    require(len(classes) == len(overlay), 'Rebuilt primary DEX has the wrong class count')
    identity = {value: value for value in allocated.values()}
    for relative, expected in overlay.items():
        name = 'L' + relative.as_posix().removesuffix('.smali') + ';'
        require(name in classes and normalized_smali(expected, identity) == normalized_smali(classes[name][1], identity),
                'Rebuilt replacement differs from the expected class: ' + name)
    actual_manifest = ET.parse(decoded / 'AndroidManifest.xml').getroot()
    for name in ['package', A + 'versionCode', A + 'versionName']:
        require(actual_manifest.get(name) == manifest.get(name), 'Rebuilt manifest identity differs')
    expected_components = {item.get(A + 'name'): item for item in manifest.find('application')
                           if (item.get(A + 'name') or '').startswith(('com.tzibbur.', 'com.android.mms.tzib.'))}
    actual_components = {item.get(A + 'name'): item for item in actual_manifest.find('application')}
    for name, expected in expected_components.items():
        require(name in actual_components and xml_shape(expected) == xml_shape(actual_components[name]),
                'Rebuilt manifest component differs: ' + name)


def main():
    bundle = Path(__file__).resolve().parent
    bundled_editor = bundle / 'tools/APKEditor-1.4.5.jar'
    default_editor = str(bundled_editor) if bundled_editor.is_file() else 'apkeditor'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('apk', type=Path, help='Original Messages APK; never modified')
    parser.add_argument('--out', type=Path, help='New output directory (must not already exist)')
    parser.add_argument('--check', action='store_true', help='Check compatibility without building')
    parser.add_argument('--apkeditor', default=os.environ.get('APKEDITOR', default_editor),
                        help='APKEditor executable or .jar path (default: bundled 1.4.5)')
    parser.add_argument('--java', default='java', help='Java executable for a .jar tool')
    parser.add_argument('--version-code', type=int, help='Output versionCode (default: input + 1)')
    args = parser.parse_args()
    require(args.apk.is_file(), 'Input APK not found')
    require(args.check or args.out is not None, 'Specify --out or --check')
    require(args.out is None or not args.out.exists(), 'Output directory already exists')
    meta = read_json(bundle / 'bundle.json')
    verify_bundle(bundle, meta)
    editor = [args.java, '-jar', str(Path(args.apkeditor).resolve())] if args.apkeditor.lower().endswith('.jar') else [args.apkeditor]
    with tempfile.TemporaryDirectory(prefix='tzibbur-integrate-') as scratch:
        work = Path(scratch)
        target, log = work / 'target', work / 'build.log'
        print('Decoding target APK...', flush=True)
        run_tool(editor, ['d', '-i', str(args.apk.resolve()), '-o', str(target)], log)
        print('Checking host code, dependencies and resources...', flush=True)
        manifest, resources, current, allocated, mapping, overlay = analyze(target, bundle, meta)
        report = {
            'bundle': meta['version'], 'input_sha256': sha(args.apk.read_bytes()),
            'input_version': manifest.get(A + 'versionName'), 'compatible': True,
            'replacement_classes': sorted(meta['replaced_classes']),
            'total_overlay_classes': len(overlay), 'resources_added': len(meta['added_resources']),
            'resource_ids_changed': sum(a != b for a, b in mapping.items()),
            'manual_device_test': 'Not performed by this tool',
        }
        if args.check:
            print(json.dumps(report, indent=2))
            return
        merge_resources(resources, bundle, meta, current, allocated)
        merge_manifest(manifest, bundle, args.version_code)
        write_xml(target / 'AndroidManifest.xml', manifest)
        # Only our replacement classes are assembled. Original DEX files will be
        # copied verbatim from the input APK after building the resource table.
        shutil.rmtree(target / 'smali')
        for relative, text in overlay.items():
            dest = target / 'smali/classes' / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding='utf-8')
        for name in ['.cache', 'signatures']:
            shutil.rmtree(target / name, ignore_errors=True)
        built = work / 'overlay.apk'
        print('Building resources and the primary replacement DEX...', flush=True)
        run_tool(editor, ['b', '-i', str(target), '-o', str(built), '-no-cache'], log)
        output = args.out.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.' + output.name + '-', dir=output.parent) as temp:
            stage = Path(temp) / 'result'
            stage.mkdir()
            result = stage / 'Messages-tzibbur-unsigned.apk'
            dex, original_count = pack_apk(built, args.apk, result)
            print('Verifying the rebuilt resource table, manifest and all replacement classes...', flush=True)
            decoded = work / 'verify'
            run_tool(editor, ['d', '-i', str(result), '-o', str(decoded)], log)
            verify_decoded(decoded, allocated, overlay, manifest)
            (stage / 'classes.dex').write_bytes(dex)
            report.update({'output_version': manifest.get(A + 'versionName'),
                           'output_version_code': int(manifest.get(A + 'versionCode')),
                           'original_dex_count': original_count, 'original_dex_preserved': True,
                           'rebuilt_resources_verified': True, 'rebuilt_classes_verified': len(overlay),
                           'rebuilt_manifest_verified': True,
                           'output_sha256': sha(result.read_bytes()), 'dex_sha256': sha(dex),
                           'signing': 'Unsigned; zipalign and sign with the receiving app signing key'})
            write_json(stage / 'report.json', report)
            write_json(stage / 'resource-map.json', {key: {
                'source': f'0x{old:08x}', 'target': f'0x{allocated[key]:08x}'}
                for key, old in meta['source_resources'].items() if key in allocated})
            shutil.copy2(log, stage / 'build.log')
            shutil.copy2(target / 'AndroidManifest.xml', stage / 'AndroidManifest.xml')
            require(not output.exists(), 'Output directory appeared while building; refusing to replace it')
            stage.rename(output)
        print('Created: ' + str(output))
        print('Unsigned APK: Messages-tzibbur-unsigned.apk. Align and sign before installing.')


if __name__ == '__main__':
    try:
        main()
    except (Incompatible, OSError, ET.ParseError, zipfile.BadZipFile) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
