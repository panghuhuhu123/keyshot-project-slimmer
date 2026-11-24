"""Conservative preview deduplication for the observed KeyShot 14 / DAL024 layout."""
from dataclasses import dataclass
import base64
import bisect
import hashlib
import mmap
import os
from pathlib import Path
import re
import contextlib
import struct
import tempfile
import zipfile

import zstandard as zstd

HEADER = b'//---luxion:dal:024---\n'
SCENE_LABEL = b'texture scaleI\x00'
COMPRESSED = b'\x87\x10'
ZSTD = b'\x28\xb5\x2f\xfd'
AFTER_SCENE = b'\xa9\x00\xa7\x16\xff\xff\xff\xffactive_colorway_id'
RAW_END = re.compile(rb'A\x00' + re.escape(AFTER_SCENE) + rb'\xab\x00')
RAW_NODE = re.compile(rb'\?(.)(....)(....)(lux\.[A-Za-z_][A-Za-z_0-9.]*)', re.S)
FILE_SUFFIXES = ('.bip', '.ksp')
STUDIO = re.compile(rb'\?\)....\x16\x00\x00\x00lux\.studios\.studionodestudio_node9\x04(....)', re.S)
STUDIO_ITEM = re.compile(rb'\?"....\x1a\x00\x00\x00lux\.studios\.studioitemnode', re.S)
STUDIO_REF = re.compile(rb'\x1d\x0c\x02\x00\x00\x00itemtype\x1d\x0c(....)studioIdA\x00', re.S)
IMAGE_STYLE = re.compile(rb'\x1d\x10....imageStyleIdA\x00', re.S)
PNG_MARKER = b'MIlQTkcNChoK'
CHUNK = 4 * 1024 * 1024
EMPTY_FRAME_HEADER = b'\x08\x00\x00\x00\x00\x00\x00\x00'
MAX_EXPANDED = 24 * 1024**3


class Unsupported(ValueError):
    """A format or record shape that this tool cannot safely rewrite."""


class Cancelled(Exception):
    pass


def fingerprint(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


@dataclass(frozen=True)
class Scene:
    start: int
    end: int
    compressed: bool

    @property
    def header_at(self):
        return self.start - 18 if self.compressed else self.start


@dataclass(frozen=True)
class Plan:
    source: Path
    original: tuple
    scene: Scene | None
    replacement: Path | None
    after: int
    removed: int
    previews: int
    active: int

    @property
    def before(self):
        return self.original[2]

    @property
    def saved(self):
        return self.before - self.after


def valid_zstd_frame(data, start, end):
    if end - start < 9 or data[start:start+4] != ZSTD:
        return False
    descriptor = data[start+4]
    if descriptor & 0x18:
        return False
    single_segment = bool(descriptor & 0x20)
    checksum_size = 4 if descriptor & 0x04 else 0
    dictionary_size = (0, 1, 2, 4)[descriptor & 3]
    size_length = (int(single_segment), 2, 4, 8)[descriptor >> 6]
    position = start + 5 + int(not single_segment) + dictionary_size + size_length
    while position + 3 <= end - checksum_size:
        header = int.from_bytes(data[position:position+3], 'little')
        block_kind = (header >> 1) & 3
        block_size = header >> 3
        if block_kind == 3 or block_size > 128 * 1024:
            return False
        position += 3 + (1 if block_kind == 1 else block_size)
        if header & 1:
            return position + checksum_size == end
    return False


def raw_scene_start(data, start):
    node = RAW_NODE.match(data, start, min(start + 256, len(data)))
    if node is None:
        return False
    name_length = node[1][0] - 8
    namespace_length = int.from_bytes(node[3], 'little')
    if not 4 < namespace_length <= name_length <= 150 or len(node[4]) < namespace_length:
        return False
    # The record length counts the UTF-8 bytes of the class and its display name.
    name_end = node.start(4) + name_length
    if data[name_end:name_end+2] not in (b'9\x04', b';\x04'):
        return False
    if len(node[4]) >= name_length:
        return True
    name_start = node.start(4) + namespace_length
    try:
        return data[name_start:name_end].decode('utf-8').isprintable()
    except UnicodeDecodeError:
        return False


def find_scene(data):
    if len(data) < 56 or not data[:22].startswith(b'//---luxion:dal:'):
        raise Unsupported('文件头无法识别，请确认文件是完整的 KeyShot BIP。')
    version = struct.unpack_from('<I', data, 40)[0]
    if data[:len(HEADER)] != HEADER or version != 14 or data[23:28] != b'\x20\x02\x00\x00\x00':
        raise Unsupported('此工程版本未验证，请用 KeyShot Studio 2025.1 另存副本后重试。')
    expected_size = struct.unpack_from('<Q', data, 32)[0]
    if expected_size != len(data):
        raise Unsupported(f'文件长度与工程记录不符（实际 {len(data):,} 字节，应为 {expected_size:,} 字节）。')
    candidates = []
    cursor = 0
    reason = '未找到场景起始标记，请在 KeyShot 中打开并另存副本后重试。'
    while (cursor := data.find(SCENE_LABEL, cursor)) != -1:
        cursor += len(SCENE_LABEL)
        if data[cursor:cursor+2] == COMPRESSED:
            reason = '压缩场景的长度或结束标记不完整，请在 KeyShot 中另存副本后重试。'
            if data[cursor+10:cursor+22] != EMPTY_FRAME_HEADER + ZSTD:
                continue
            start = cursor + 18
            end = start + struct.unpack_from('<Q', data, cursor+2)[0]
            trailer = b'\x89\x00' + AFTER_SCENE + b'\xab\x00'
            if data[end:end+len(trailer)] == trailer and valid_zstd_frame(data, start, end):
                candidates.append(Scene(start, end, True))
        elif raw_scene_start(data, cursor):
            reason = '找到场景起点，但缺少可验证的场景结束标记。'
            ending = RAW_END.search(data, cursor)
            if ending:
                if RAW_END.search(data, ending.end()):
                    raise Unsupported('找到多个场景结束标记，无法确定边界。')
                candidates.append(Scene(cursor, ending.start()+2, False))
        else:
            reason = '场景起始记录使用了未验证的布局，请在 KeyShot 中另存副本后重试。'
    if not candidates:
        raise Unsupported(reason)
    if len(candidates) != 1:
        raise Unsupported('找到多个可能的场景区段，无法唯一确定边界。')
    return candidates[0]


def select_duplicates(data, start, end, check):
    nodes = list(STUDIO.finditer(data, start, end))
    offsets = [node.start() for node in nodes]
    ids = [node[1] for node in nodes]
    # Keep the previews of all studios listed in the project's studio hierarchy.
    items = list(STUDIO_ITEM.finditer(data, start, end))
    active = set()
    for item in items:
        ref = STUDIO_REF.search(data, item.end(), min(item.end() + 48, end))
        if ref is not None:
            active.add(ref[1])
        elif not re.search(rb'\x1d\x0c\x01\x00\x00\x00itemtype\x1d\x0c....studioId',
                           data[item.end():min(item.end()+4096, end)], re.S):
            raise Unsupported('遇到无法识别的工作室列表记录')
    if not active.issubset(ids) or len(ids) != len(set(ids)):
        raise Unsupported('遇到无法确认的工作室引用')

    previews = []
    seen_nodes = set()
    pos = start
    while (pos := data.find(PNG_MARKER, pos, end)) >= 0:
        check()
        field_start = pos - 9
        index = bisect.bisect_right(offsets, field_start) - 1
        if index < 0 or field_start < start:
            raise Unsupported('预览图缺少可识别的工作室记录')
        node = nodes[index]
        size = struct.unpack_from('<I', data, pos-4)[0]
        field_end = pos + size + 7
        node_end = offsets[index+1] if index+1 < len(offsets) else end
        if (index in seen_nodes or not 0 < field_start-node.end() < 512
                or data[field_start] != 0x2e
                or not 50 < size < 20_000_000 or size % 4
                or field_end >= node_end
                or struct.unpack_from('<I', data, field_start+1)[0] != size+11
                or data[field_end-7:field_end] != b'pngData'
                or not IMAGE_STYLE.match(data, field_end, node_end)):
            raise Unsupported('发现与样本结构不同的预览记录')
        image = base64.b64decode(data[pos:pos+size], validate=True)
        if not image.startswith(b'0\x89PNG\r\n\x1a\n') or not image.endswith(b'IEND\xaeB`\x82'):
            raise Unsupported('发现无法验证的 PNG 预览')
        previews.append((field_start, field_end, hashlib.sha256(image[1:]).digest(), node[1] in active))
        seen_nodes.add(index)
        pos = field_end
    kept = {digest for _, _, digest, visible in previews if visible}
    removed = []
    for left, right, digest, visible in previews:
        if visible or digest not in kept:
            kept.add(digest)
        else:
            removed.append((left, right))
    return removed, len(previews), len(active)


def build_replacement(data, start, end, removed, output, check):
    expected = hashlib.sha256()
    with output.open('wb') as frame, zstd.ZstdCompressor(level=6, write_content_size=False).stream_writer(frame, closefd=False) as writer:
        cursor = start
        for left, right in [*removed, (end, end)]:
            for pos in range(cursor, left, CHUNK):
                check()
                chunk = data[pos:min(pos+CHUNK, left)]
                writer.write(chunk)
                expected.update(chunk)
            cursor = right
    actual = hashlib.sha256()
    with output.open('rb') as frame, zstd.ZstdDecompressor().stream_reader(frame) as reader:
        while chunk := reader.read(CHUNK):
            check()
            actual.update(chunk)
    if actual.digest() != expected.digest():
        raise ValueError('重打包校验失败')


def analyze_bip(source, workdir, progress, check):
    source = Path(source).resolve(strict=True)
    raw = None
    replacement = None
    try:
        with source.open('rb') as original:
            initial = fingerprint(os.fstat(original.fileno()))
            if initial[2] == 0:
                raise Unsupported('文件为空')
            with mmap.mmap(original.fileno(), 0, access=mmap.ACCESS_READ) as disk:
                progress('定位场景数据')
                scene = find_scene(disk)
                check()
                data, start, end = disk, scene.start, scene.end
                if scene.compressed:
                    progress('解压场景数据')
                    raw = Path(workdir) / 'scene.tmp'
                    view = memoryview(disk)[start:end]
                    try:
                        with raw.open('xb') as expanded, zstd.ZstdDecompressor(max_window_size=64*1024*1024).stream_reader(view) as reader:
                            total = 0
                            while chunk := reader.read(CHUNK):
                                check()
                                total += len(chunk)
                                if total > MAX_EXPANDED:
                                    raise Unsupported('解压后的场景超过本工具的处理上限')
                                expanded.write(chunk)
                    finally:
                        view.release()
                with contextlib.ExitStack() as stack:
                    if raw is not None:
                        opened = stack.enter_context(raw.open('rb'))
                        data = stack.enter_context(mmap.mmap(opened.fileno(), 0, access=mmap.ACCESS_READ))
                        start, end = 0, len(data)
                    progress('识别重复预览图')
                    removed, previews, active = select_duplicates(data, start, end, check)
                    after = initial[2]
                    if removed:
                        if data[end-2:end] != b'A\x00':
                            raise Unsupported('解压场景结束标记与样本不符')
                        progress('计算瘦身后体积')
                        with tempfile.NamedTemporaryFile(dir=workdir, suffix='.zst', delete=False) as tmp:
                            replacement = Path(tmp.name)
                        build_replacement(data, start, end, removed, replacement, check)
                        after += (18 + replacement.stat().st_size + 2) - (scene.end - scene.header_at)
                        if scene.compressed:
                            after -= 2  # The input suffix already contains the compressed section terminator.
                if fingerprint(source.stat()) != initial:
                    raise ValueError('源文件已变化，请重新分析')
                return Plan(source, initial, scene, replacement, after, len(removed), previews, active)
    except BaseException:
        if replacement is not None:
            replacement.unlink(missing_ok=True)
        raise
    finally:
        if raw is not None:
            raw.unlink(missing_ok=True)


def copy_bytes(source, destination, length, check):
    while length:
        check()
        chunk = source.read(min(CHUNK, length))
        if not chunk:
            raise OSError('读取源文件时意外结束')
        destination.write(chunk)
        length -= len(chunk)


def write_bip(plan, out, check):
    with plan.source.open('rb') as source:
        if fingerprint(os.fstat(source.fileno())) != plan.original:
            raise ValueError('源文件已变化，请重新分析')
        header = bytearray(source.read(56))
        struct.pack_into('<Q', header, 32, plan.after)
        out.write(header)
        copy_bytes(source, out, plan.scene.header_at-len(header), check)
        with plan.replacement.open('rb') as frame:
            length = os.fstat(frame.fileno()).st_size
            out.write(COMPRESSED + struct.pack('<Q', length) + EMPTY_FRAME_HEADER)
            copy_bytes(frame, out, length, check)
        if not plan.scene.compressed:
            out.write(b'\x89\x00')
        source.seek(plan.scene.end)
        copy_bytes(source, out, plan.before-plan.scene.end, check)
        if out.tell() != plan.after or fingerprint(plan.source.stat()) != plan.original:
            raise ValueError('输出长度校验失败或源文件已发生变化')


class PackageEntry(zipfile.ZipInfo):
    def _encodeFilenameFlags(self):
        # KeyShot also writes UTF-8 name bytes without the ZIP UTF-8 flag.
        return self.filename.encode(self.name_encoding), self.flag_bits | self.name_flag


def package_entry(original, size):
    entry = PackageEntry(original.filename, original.date_time)
    for attr in ('compress_type', 'comment', 'internal_attr', 'external_attr',
                 'create_system', 'create_version', 'extract_version'):
        setattr(entry, attr, getattr(original, attr))
    # ZIP64 sizes/offsets are recalculated by the writer. Keep other extra fields.
    entry.extra = strip_zip64(original.extra)
    entry.name_flag = original.flag_bits & 0x800
    entry.name_encoding = 'utf-8' if entry.name_flag else 'cp437'
    entry.file_size = size
    return entry


def strip_zip64(extra):
    offset = 0
    kept = bytearray()
    while offset + 4 <= len(extra):
        tag, size = struct.unpack_from('<HH', extra, offset)
        end = offset + 4 + size
        if end > len(extra):
            raise Unsupported('KSP 资源索引不完整。')
        if tag != 1:
            kept.extend(extra[offset:end])
        offset = end
    return bytes(kept) + extra[offset:]


def analyze_ksp(source, workdir, progress, check):
    initial = fingerprint(source.stat())
    progress('读取 KSP 工程包')
    inner_path = Path(workdir) / 'package-scene.bip'
    packed = Path(workdir) / 'slim.ksp'
    try:
        with zipfile.ZipFile(source) as original:
            entries = original.infolist()
            scenes = [entry for entry in entries if not entry.is_dir() and entry.filename.lower().endswith('.bip')]
            if len(scenes) != 1:
                raise Unsupported(f'KSP 中找到 {len(scenes)} 个 BIP，无法唯一确定主工程。')
            if any(entry.flag_bits & 1 for entry in entries):
                raise Unsupported('KSP 包内有加密文件，请在 KeyShot 中另存一个可读取的工程包。')
            scene = scenes[0]
            with original.open(scene) as src, inner_path.open('xb') as dst:
                copy_bytes(src, dst, scene.file_size, check)
                if src.read(1):
                    raise ValueError('KSP 内的 BIP 长度校验失败。')
            plan = analyze_bip(inner_path, workdir, progress, check)
            try:
                if plan.saved > 0:
                    progress('打包瘦身工程及原有资源')
                    with tempfile.TemporaryFile(dir=workdir) as rewritten:
                        write_bip(plan, rewritten, check)
                        rewritten.seek(0)
                        # Use a non-seekable ZIP writer to match KeyShot's data descriptors.
                        with packed.open('xb') as archive_file, zipfile.ZipFile(PackageStream(archive_file), 'w', allowZip64=True) as output:
                            output.comment = original.comment
                            for entry in entries:
                                check()
                                size = plan.after if entry is scene else entry.file_size
                                target = package_entry(entry, size)
                                with output.open(target, 'w', force_zip64=size >= zipfile.ZIP64_LIMIT) as dst:
                                    if entry is scene:
                                        copy_bytes(rewritten, dst, size, check)
                                    else:
                                        with original.open(entry) as src:
                                            copy_bytes(src, dst, size, check)
                                            if src.read(1):
                                                raise ValueError('KSP 资源长度校验失败。')
                    with zipfile.ZipFile(packed) as output:
                        expected = [(e.filename, e.file_size, e.CRC) for e in entries if e is not scene]
                        actual = [(e.filename, e.file_size, e.CRC) for e in output.infolist() if not e.filename.lower().endswith('.bip') or e.is_dir()]
                        if actual != expected:
                            raise ValueError('KSP 资源校验失败。')
            finally:
                if plan.replacement is not None:
                    plan.replacement.unlink(missing_ok=True)
        if fingerprint(source.stat()) != initial:
            raise ValueError('源文件已变化，请重新分析')
        after = packed.stat().st_size if packed.exists() else initial[2]
        if after >= initial[2]:
            packed.unlink(missing_ok=True)
            return Plan(source, initial, None, None, initial[2], plan.removed, plan.previews, plan.active)
        return Plan(source, initial, None, packed, after, plan.removed, plan.previews, plan.active)
    except zipfile.BadZipFile as error:
        packed.unlink(missing_ok=True)
        raise Unsupported(f'KSP 工程包损坏或读取不完整 · {error}') from error
    except (NotImplementedError, RuntimeError) as error:
        packed.unlink(missing_ok=True)
        raise Unsupported(f'KSP 包内资源采用了无法读取的压缩方式 · {error}') from error
    except BaseException:
        packed.unlink(missing_ok=True)
        raise
    finally:
        inner_path.unlink(missing_ok=True)


class PackageStream:
    def __init__(self, file):
        self.file = file

    def write(self, data):
        return self.file.write(data)

    def flush(self):
        self.file.flush()


def analyze(source, workdir, progress=lambda text: None, check=lambda: None):
    source = Path(source).resolve(strict=True)
    suffix = source.suffix.lower()
    if suffix == '.ksp':
        return analyze_ksp(source, workdir, progress, check)
    if suffix == '.bip':
        return analyze_bip(source, workdir, progress, check)
    raise Unsupported('请选择 .bip 或 .ksp 文件。')


def generate(plan, output_dir=None, check=lambda: None):
    if plan.replacement is None or plan.saved <= 0:
        raise ValueError('未发现可缩小体积的重复预览图')
    folder = Path(output_dir) if output_dir else plan.source.parent
    folder = folder.resolve(strict=True)
    tmp_path = None
    try:
        if fingerprint(plan.source.stat()) != plan.original:
            raise ValueError('源文件已变化，请重新分析')
        with tempfile.NamedTemporaryFile(prefix='.keyshot-', suffix='.tmp', dir=folder, delete=False) as out:
            tmp_path = Path(out.name)
            if plan.scene is None:
                with plan.replacement.open('rb') as packed:
                    copy_bytes(packed, out, plan.after, check)
            else:
                write_bip(plan, out, check)
            if out.tell() != plan.after or fingerprint(plan.source.stat()) != plan.original:
                raise ValueError('输出长度校验失败或源文件已发生变化')
            out.flush()
            os.fsync(out.fileno())
        check()
        index = 1
        while True:
            suffix = '' if index == 1 else f' ({index})'
            target = folder / f'{plan.source.stem}_瘦身{suffix}{plan.source.suffix.lower()}'
            try:
                if os.name == 'nt':
                    os.rename(tmp_path, target)  # Windows rename fails if the destination exists.
                else:
                    os.link(tmp_path, target)
                return target
            except FileExistsError:
                index += 1
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
