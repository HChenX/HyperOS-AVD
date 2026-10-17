"""Prevent ranchu IMapper abort when host uses shared memory slots."""
import hashlib

NATIVE = '/vendor/lib64/hw/mapper.ranchu.so'
BEFORE = '311ccf0653ab04154fa3c6acbfa99b81f0dc904f501228dee4dc8725e0a6b870'
AFTER = 'ff352920af868ed20ef097090c38a1c3783dc7f837fca3aea77102b40606a497'
SITES = (
    (0x4174, bytes.fromhex('e1031f2a'), bytes.fromhex('21008052')),
    (0x4188, bytes.fromhex('a809f837'), bytes.fromhex('1f2003d5')),
    (0x41bc, bytes.fromhex('e00800b5'), bytes.fromhex('1f2003d5')),
)
MANIFEST = {'revision': 1, 'native': NATIVE, 'native_sha256': AFTER,
            'shared_slots': True}


def patch(data):
    """Fix ranchu IMapper abort on modern host goldfish address space driver.

    The stock mapper.ranchu.so hardcodes useSharedSlots=0 when probing
    GoldfishAddressSpaceHostMemoryAllocator in its constructor.
    On modern hosts (QEMU / gfxstream), device_type=5 is rejected in favor of
    device_type=6 (SharedSlotsHostMemoryAllocator). The failed ioctl causes
    an assertion abort, restarting SurfaceFlinger and cascading into an infinite
    Zygote preload-and-crash loop under TCG emulation.

    Replace `mov w1, wzr` at 0x4174 with `mov w1, #1` to probe shared slots,
    and nop the fatal abort branches at 0x4188 and 0x41bc so probing failure
    does not kill the composer service.
    """
    checksum = hashlib.sha256(data).hexdigest()
    if checksum == AFTER:
        return data
    if checksum != BEFORE:
        raise RuntimeError('Unsupported ranchu mapper SHA-256: ' + checksum)
    result = bytearray(data)
    for offset, before, after in SITES:
        if result[offset:offset + len(before)] != before:
            raise RuntimeError(f'Unexpected ranchu mapper instruction at {hex(offset)}.')
        result[offset:offset + len(before)] = after
    if len(result) != len(data) or hashlib.sha256(result).hexdigest() != AFTER:
        raise RuntimeError('Ranchu mapper patch checksum mismatch.')
    return bytes(result)


def build_vendor(source, destination, work):
    """Recompress the patched ELF while retaining all vendor inode metadata."""
    from build_image import erofs
    from erofs_image import build
    path = NATIVE.removeprefix('/vendor/')
    data = patch(erofs(source, '/' + path))
    build(destination, [('', source)], work,
          {path: (data, 0o644, 'u:object_r:same_process_hal_file:s0')},
          preserve_replacement_metadata=True)
    if hashlib.sha256(erofs(destination, '/' + path)).hexdigest() != AFTER:
        raise RuntimeError('Baked ranchu mapper checksum mismatch.')
    return dict(MANIFEST)

