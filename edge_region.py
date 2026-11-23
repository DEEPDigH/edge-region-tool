"""Local Edge country-record repair. No registry, ACL, service or scheduled-task changes."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
try:
    import zstandard as zstd
except ImportError as exc:
    raise SystemExit('无法加载 Zstandard 库。请使用 Python 3.12，或执行：\n'
                     'python -m pip install --upgrade --target "' + str(HERE / 'lib') + '" zstandard') from exc

TOOL_VERSION = '2.0'
SUPPORTED_MAJORS = {157, 158}
SAFE_SEED_SENTINEL = b'safe_seed_content'
RULE = 'EdgeWritingBlockedRegionsMitigation20261103'
FEATURE = 'msEdgeComposeNext'
LIMIT = 32 * 1024 * 1024
BLOCKED = set('at be bg by cn cu cy cz de dk ee es fi fr gr hr hu ie ir it kp lt lu lv mt nl pl pt ro ru sd se si sk sy ua ve'.upper().split())

class RepairError(Exception):
    pass

def country(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch('[A-Z]{2}', value):
        raise RepairError('地区必须是两位字母，例如 US、JP、HK、GB。')
    return value

def edge_running() -> bool:
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq msedge.exe', '/FO', 'CSV', '/NH'],
                            capture_output=True, check=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return b'msedge.exe' in result.stdout.lower()

def require_closed() -> None:
    if edge_running():
        raise RepairError('Edge 仍在运行。请保存未提交的网页内容并完全退出 Edge 后重试。未修改文件。')

def encode_varint(number: int) -> bytes:
    out = bytearray()
    while number > 127:
        out.append((number & 127) | 128)
        number >>= 7
    out.append(number)
    return bytes(out)

@dataclass
class Field:
    number: int
    wire: int
    value: bytes | int
    raw: bytes

def parse_fields(data: bytes) -> list[Field]:
    def read(index):
        number = shift = 0
        for _ in range(10):
            if index >= len(data):
                raise RepairError('Protobuf 数据截断。')
            byte = data[index]
            index += 1
            number |= (byte & 127) << shift
            if byte < 128:
                return number, index
            shift += 7
        raise RepairError('Protobuf varint 长度异常。')
    fields, index = [], 0
    while index < len(data):
        start = index
        tag, index = read(index)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise RepairError('Protobuf 字段编号异常。')
        if wire == 0:
            value, index = read(index)
        elif wire == 2:
            size, index = read(index)
            end = index + size
            if end > len(data):
                raise RepairError('Protobuf 字段长度越界。')
            value, index = data[index:end], end
        elif wire in (1, 5):
            end = index + (8 if wire == 1 else 4)
            if end > len(data):
                raise RepairError('Protobuf 固定长度字段截断。')
            value, index = data[index:end], end
        else:
            raise RepairError('发现未支持的 Protobuf 格式，停止修改。')
        fields.append(Field(number, wire, value, data[start:index]))
    return fields

def unique_field(fields: list[Field], number: int, wire: int) -> Field:
    found = [f for f in fields if f.number == number]
    if len(found) != 1 or found[0].wire != wire:
        raise RepairError(f'国家记录结构变化：字段 {number} 不符合已验证格式。')
    return found[0]

def top_span(text: str, wanted: str) -> tuple[int, int]:
    """Find one top-level JSON value without rewriting unrelated formatting."""
    decoder = json.JSONDecoder()
    def space(i):
        while i < len(text) and text[i].isspace():
            i += 1
        return i
    i, hits = space(0), []
    if i >= len(text) or text[i] != '{':
        raise RepairError('JSON 根节点不是对象。')
    i += 1
    while True:
        i = space(i)
        if text[i] == '}':
            break
        key, end = decoder.raw_decode(text, i)
        i = space(end)
        if not isinstance(key, str) or text[i] != ':':
            raise RepairError('JSON 键格式异常。')
        start = space(i + 1)
        _, end = decoder.raw_decode(text, start)
        if key == wanted:
            hits.append((start, end))
        i = space(end)
        if text[i] == '}':
            break
        if text[i] != ',':
            raise RepairError('JSON 分隔符异常。')
        i += 1
    if len(hits) != 1:
        raise RepairError(f'未找到唯一的 {wanted}，停止修改。')
    return hits[0]

def replace_top(text: str, key: str, value) -> str:
    start, end = top_span(text, key)
    if json.loads(text[start:end]) == value:
        return text
    return text[:start] + json.dumps(value, ensure_ascii=False, separators=(',', ':')) + text[end:]

def replace_permanent_country(text: str, destination: str) -> str:
    start, end = top_span(text, 'variations_permanent_consistency_country')
    array = text[start:end]
    decoder = json.JSONDecoder()
    i = 1
    while array[i].isspace():
        i += 1
    _, i = decoder.raw_decode(array, i)
    while array[i].isspace():
        i += 1
    if array[i] != ',':
        raise RepairError('永久一致性国家缺少分隔符。')
    i += 1
    while array[i].isspace():
        i += 1
    old, stop = decoder.raw_decode(array, i)
    if old == destination:
        return text
    changed = array[:i] + json.dumps(destination) + array[stop:]
    return text[:start] + changed + text[end:]

def json_country(value) -> str:
    if not isinstance(value, str):
        raise RepairError('国家记录不是字符串。')
    return country(value)

def decode_seed(blob: bytes):
    if len(blob) > LIMIT:
        raise RepairError('配置文件异常过大，停止修改。')
    data = zstd.ZstdDecompressor().decompress(blob, max_output_size=LIMIT)
    fields = parse_fields(data)
    payload = unique_field(fields, 1, 2).value
    if payload == SAFE_SEED_SENTINEL:
        document = None
    else:
        try:
            document = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise RepairError('下发文件的数据不是可识别的 JSON 或备用引用标记，未修改。') from exc
        if not isinstance(document, dict) or not isinstance(document.get('Headers'), dict):
            raise RepairError('下发文件缺少已识别的 Headers 对象，未修改。')
    for number in (6, 7):
        json_country(unique_field(fields, number, 2).value.decode('ascii'))
    versions = [f for f in fields if f.number == 8]
    if len(versions) > 1 or (versions and versions[0].wire != 2):
        raise RepairError('下发文件永久国家版本字段格式异常。')
    version = versions[0].value.decode('ascii') if versions else ''
    if version and not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
        raise RepairError('下发文件永久国家版本信息异常。')
    # Chromium StoredSeedInfo: download milestone (3) and permanent-country
    # version (8) are independent; safe seeds can omit the latter entirely.
    milestone = unique_field(fields, 3, 0).value
    if not isinstance(milestone, int) or milestone < 1:
        raise RepairError('下发文件下载主版本记录异常。')
    if document is not None:
        json_country(document['Headers']['CountryCode'])
    return fields, document, version

def seed_patch(blob: bytes, destination: str, repair_writing=False, allow_new_version=False,
               version_context=None):
    fields, before, version = decode_seed(blob)
    effective_version = version_context or version
    if not effective_version or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', effective_version):
        raise RepairError('缺少可识别的当前浏览器版本，停止写入。')
    if int(effective_version.split('.')[0]) not in SUPPORTED_MAJORS and not allow_new_version:
        raise RepairError(f'当前浏览器版本 {effective_version} 尚未验证。仅检测，不自动写入。请先验证结构；确认后可用 --allow-new-version。')
    if unique_field(fields, 2, 2).value:
        raise RepairError('下发文件存在非空签名。为避免破坏完整性校验，停止修改。')
    payload = unique_field(fields, 1, 2).value
    if before is not None:
        text = payload.decode('utf-8')
        start, end = top_span(text, 'Headers')
        text = text[:start] + replace_top(text[start:end], 'CountryCode', destination) + text[end:]
        after = json.loads(text)
        known = after.get('EdgeConfig', {}).get(RULE)
        if repair_writing and isinstance(known, dict) and FEATURE in known.get('disableFeatures', []):
            config = after['EdgeConfig']
            entry = dict(known)
            entry['disableFeatures'] = [x for x in entry['disableFeatures'] if x != FEATURE]
            if not entry['disableFeatures']:
                del entry['disableFeatures']
            entry['enableFeatures'] = list(dict.fromkeys(entry.get('enableFeatures', []) + [FEATURE]))
            config[RULE] = entry
            text = replace_top(text, 'EdgeConfig', config)
        result = json.loads(text)
        expected = json.loads(json.dumps(before))
        expected['Headers']['CountryCode'] = destination
        if repair_writing and isinstance(known, dict):
            expected.setdefault('EdgeConfig', {})[RULE] = result['EdgeConfig'][RULE]
        if expected != result:
            raise RepairError('检测到无关配置变化，停止写入。')
        payload = text.encode('utf-8')
    parts = []
    for field in fields:
        if field.number in (1, 6, 7):
            value = payload if field.number == 1 else destination.encode('ascii')
            parts.append(encode_varint(field.number * 8 + 2) + encode_varint(len(value)) + value)
        else:
            parts.append(field.raw)
    rebuilt = b''.join(parts)
    if rebuilt == zstd.ZstdDecompressor().decompress(blob, max_output_size=LIMIT):
        return blob
    params = zstd.get_frame_parameters(blob)
    compressed = zstd.ZstdCompressor(level=3, write_checksum=params.has_checksum).compress(rebuilt)
    if zstd.ZstdDecompressor().decompress(compressed, max_output_size=LIMIT) != rebuilt:
        raise RepairError('压缩回读验证失败。')
    decode_seed(compressed)
    return compressed

def state_patch(blob: bytes, destination: str) -> bytes:
    bom = b'\xef\xbb\xbf' if blob.startswith(b'\xef\xbb\xbf') else b''
    text = blob.decode('utf-8-sig')
    before = json.loads(text)
    json_country(before['variations_country'])
    permanent = before['variations_permanent_consistency_country']
    if not isinstance(permanent, list) or len(permanent) != 2:
        raise RepairError('永久一致性国家格式变化，停止写入。')
    json_country(permanent[1])
    text = replace_top(text, 'variations_country', destination)
    text = replace_permanent_country(text, destination)
    expected = dict(before)
    expected['variations_country'] = destination
    expected['variations_permanent_consistency_country'] = [permanent[0], destination]
    if json.loads(text) != expected:
        raise RepairError('Local State 包含无关修改。')
    return bom + text.encode('utf-8')

def default_root() -> Path:
    return Path(os.environ['LOCALAPPDATA']) / 'Microsoft/Edge/User Data'

def file_pair(root):
    names = ['Local State', 'VariationsSeedV2']
    if (root / 'VariationsSafeSeedV2').is_file():
        names.append('VariationsSafeSeedV2')
    return {name: (root / name).read_bytes() for name in names}

def browser_version(state, seed_version=''):
    value = state.get('variations_last_launch_version')
    if not value:
        permanent = state.get('variations_permanent_consistency_country')
        value = permanent[0] if isinstance(permanent, list) and permanent else seed_version
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', value):
        raise RepairError('无法识别当前浏览器版本。')
    return value

def resolved_document(blobs, document):
    if document is not None:
        return document, 'VariationsSeedV2', None
    if 'VariationsSafeSeedV2' not in blobs:
        raise RepairError('当前配置引用备用文件，但 VariationsSafeSeedV2 不存在。请先正常启动并退出 Edge，或使用完整备份恢复。')
    safe_fields, safe_doc, _ = decode_seed(blobs['VariationsSafeSeedV2'])
    if safe_doc is None:
        raise RepairError('备用配置也包含引用标记，无法解析有效配置，未修改。')
    return safe_doc, 'VariationsSafeSeedV2（最新配置通过 safe_seed_content 引用）', safe_fields

def inspect(root, blobs=None):
    blobs = file_pair(root) if blobs is None else blobs
    state = json.loads(blobs['Local State'].decode('utf-8-sig'))
    fields, doc, version = decode_seed(blobs['VariationsSeedV2'])
    doc, source, safe_fields = resolved_document(blobs, doc)
    value = {6: unique_field(fields, 6, 2).value.decode(), 7: unique_field(fields, 7, 2).value.decode()}
    result = {'工具版本': TOOL_VERSION, '配置版本': browser_version(state, version),
              '有效配置来源': source,
              '配置下载时主版本': unique_field(fields, 3, 0).value,
              '永久国家记录版本': version or '未保存（备用配置允许省略）',
              'variations_country': state.get('variations_country'),
              '永久一致性国家': state.get('variations_permanent_consistency_country', [None, None])[1],
              'Headers.CountryCode': doc['Headers']['CountryCode'],
              '内部字段6_影响Copilot入口': value[6], '内部字段7_写作地区判定': value[7],
              '已知写作禁用项': FEATURE in doc.get('EdgeConfig', {}).get(RULE, {}).get('disableFeatures', [])}
    if safe_fields is not None:
        result['备用文件内部字段6'] = unique_field(safe_fields, 6, 2).value.decode()
        result['备用文件内部字段7'] = unique_field(safe_fields, 7, 2).value.decode()
    return result

def atomic_write(path: Path, value: bytes):
    descriptor, pending = tempfile.mkstemp(prefix='.' + path.name + '.region-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)

def commit(root, original, changed, action):
    require_closed()
    if original == changed:
        print('目标值已经一致，无需修改，也没有产生重复备份。')
        return None
    if file_pair(root) != original:
        raise RepairError('检查期间文件被其他程序修改，未写入。')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = root / 'Codex-Backups/RegionTool' / stamp
    backup.mkdir(parents=True)
    manifest = {'format': 2, 'created': datetime.now().isoformat(), 'action': action,
                'source_root': str(root.resolve()), 'before': inspect(root, original),
                'after': inspect(root, changed), 'files': {}}
    for name, content in original.items():
        (backup / name).write_bytes(content)
        manifest['files'][name] = hashlib.sha256(content).hexdigest()
    (backup / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    require_closed()
    if file_pair(root) != original:
        raise RepairError('备份期间浏览器配置发生变化，未写入。')
    written = []
    try:
        order = sorted(changed, key=lambda n: {'VariationsSafeSeedV2': 0,
                                             'VariationsSeedV2': 1, 'Local State': 2}[n])
        for name in order:
            content = changed[name]
            if content != original[name]:
                require_closed()
                atomic_write(root / name, content)
                written.append(name)
        if file_pair(root) != changed:
            raise RepairError('写入后回读验证失败。')
    except Exception as exc:
        if edge_running():
            raise RepairError(f'操作中断，Edge 在操作期间启动。请关闭 Edge 后使用此备份恢复：{backup}') from exc
        for name in written:
            atomic_write(root / name, original[name])
        raise RepairError(f'操作失败，已回滚。备份：{backup}；原因：{exc}') from exc
    print('修改完成，已回读验证。备份目录：' + str(backup))
    return backup

def set_region(root, destination, repair_writing=False, allow_new_version=False):
    destination = country(destination)
    require_closed()
    original = file_pair(root)
    state = json.loads(original['Local State'].decode('utf-8-sig'))
    version = browser_version(state)
    fields, doc, _ = decode_seed(original['VariationsSeedV2'])
    resolved_document(original, doc)  # Validate referenced payload before writing anything.
    changed = dict(original)
    changed['Local State'] = state_patch(original['Local State'], destination)
    changed['VariationsSeedV2'] = seed_patch(original['VariationsSeedV2'], destination,
                                          repair_writing, allow_new_version, version)
    if doc is None:
        changed['VariationsSafeSeedV2'] = seed_patch(original['VariationsSafeSeedV2'], destination,
                                                  repair_writing, allow_new_version, version)
    commit(root, original, changed, {'operation': 'set', 'country': destination, 'repair_writing': repair_writing})
    show(root)
    if destination in BLOCKED:
        print('提示：此地区在当前已实测版本的写作禁用列表中，写作入口可能消失。')

def show(root):
    print(json.dumps(inspect(root), ensure_ascii=False, indent=2))

def restore(root, source):
    require_closed()
    home = root / 'Codex-Backups/RegionTool'
    if source == 'latest':
        candidates = sorted(p.parent for p in home.glob('*/manifest.json')
                            if json.loads(p.read_text(encoding='utf-8')).get('action', {}).get('operation') != 'restore')
        if not candidates:
            raise RepairError('没有由本工具生成的可恢复备份。')
        backup = candidates[-1]
    else:
        backup = Path(source).expanduser().resolve()
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') not in (1, 2) or manifest.get('source_root') != str(root.resolve()):
        raise RepairError('备份不属于当前用户数据目录，拒绝恢复。')
    allowed = {'Local State', 'VariationsSeedV2', 'VariationsSafeSeedV2'}
    names = set(manifest['files'])
    if not {'Local State', 'VariationsSeedV2'} <= names or not names <= allowed:
        raise RepairError('备份文件清单异常。')
    restored = {name: (backup / name).read_bytes() for name in names}
    for name, content in restored.items():
        if hashlib.sha256(content).hexdigest() != manifest['files'][name]:
            raise RepairError('备份校验失败：' + name)
    original = file_pair(root)
    if decode_seed(restored['VariationsSeedV2'])[1] is None and 'VariationsSafeSeedV2' not in restored:
        raise RepairError('旧备份缺少被引用的备用配置，拒绝不完整恢复。')
    changed = dict(original)
    changed.update(restored)
    if inspect(root, original)['配置版本'] != inspect(root, changed)['配置版本']:
        raise RepairError('配置版本已变化，不整份恢复旧备份。请让 AI 迁移地区记录。')
    commit(root, original, changed, {'operation': 'restore', 'backup': str(backup)})
    show(root)

def launch(root, destination, repair_writing=False, allow_new_version=False):
    set_region(root, destination, repair_writing, allow_new_version)
    options = [Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
               Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Microsoft/Edge/Application/msedge.exe']
    executable = next((p for p in options if p.is_file()), None)
    if executable is None:
        raise RepairError('地区已修复，但未找到 Edge 程序，请手动打开。')
    # No force-feature flags; use the existing last-used profile.
    arguments = [str(executable)]
    if root.resolve() != default_root().resolve():
        arguments.append('--user-data-dir=' + str(root))
    subprocess.Popen(arguments, close_fds=True)
    print('已正常启动 Edge，没有附加测试开关。')

def menu(root):
    print('\nEdge 地区修复工具\n1. 仅查看当前值\n2. 修改地区（默认 US）\n3. 启动前修复并打开 Edge\n4. 恢复最近一次修改前的备份\n0. 退出')
    choice = input('请选择：').strip()
    if choice == '1':
        show(root)
    elif choice in ('2', '3'):
        destination = country(input('地区代码 [US]：').strip() or 'US')
        action = launch if choice == '3' else set_region
        action(root, destination)
    elif choice == '4':
        print('将恢复最近一次修改前的配置文件（包含存在的备用配置）；当前状态也会先备份。')
        if input('确认恢复？输入 RESTORE：').strip() == 'RESTORE':
            restore(root, 'latest')
    elif choice != '0':
        raise RepairError('无效选项。')

def main(argv=None):
    parser = argparse.ArgumentParser(description='Edge 地区记录修复；不修改注册表或权限。')
    parser.add_argument('--user-data-dir', type=Path, default=default_root())
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('status', help='只读检查，可在 Edge 运行时执行')
    for name in ('set', 'launch'):
        p = sub.add_parser(name)
        p.add_argument('country', nargs='?', default='US')
        p.add_argument('--repair-writing', action='store_true', help='另行修复已确认的单条 msEdgeComposeNext 禁用配置')
        p.add_argument('--allow-new-version', action='store_true', help='在 AI 验证新主版本结构后显式允许写入')
    p = sub.add_parser('restore')
    p.add_argument('backup', nargs='?', default='latest')
    args = parser.parse_args(argv)
    root = args.user_data_dir.expanduser().resolve()
    try:
        if args.command is None:
            menu(root)
        elif args.command == 'status':
            show(root)
        elif args.command in ('set', 'launch'):
            (set_region if args.command == 'set' else launch)(root, args.country, args.repair_writing, args.allow_new_version)
        elif args.command == 'restore':
            restore(root, args.backup)
        return 0
    except (RepairError, OSError, ValueError, KeyError, zstd.ZstdError) as exc:
        print('未完成：' + str(exc), file=sys.stderr)
        return 1

if __name__ == '__main__':
    # Keep redirected output readable in PowerShell, other AI tools and log files.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    sys.exit(main())
