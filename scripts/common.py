"""Shared host discovery and isolation rules for HyperOS-AVD."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import urllib.request
import file_lock
from https_transport import secure_urlretrieve

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get('HYPEROS_AVD_WORKSPACE', REPO_ROOT)).expanduser().resolve()
DEFAULT_NAME = 'HyperOS_3_API_36'
DEFAULT_PORT = 5566
OS4_NAME = 'HyperOS_4_Official_API_37'
OS4_PORT = 5574
OS4_SOURCE = 'official-hongkong-ota'
KSU_VERSION = 'v3.3.0'
KSU_ASSETS = {
    'ksud-aarch64-apple-darwin': '40ca97a2fb61284129909abac5325dcae790736d9b88901f4f31cc7ec6d9a705',
    'ksud-aarch64-linux-android': '8614de6cdc2233c71fd0d1c64381ea10fbe6658651bae9b5a8dab4fe08e6344b',
    'lkm-aarch64-android15-6.6_kernelsu.ko': 'c31d994aaf285e7bf4cf1ec38c2bbf2d7f303d1a4a7d616405bcd9f850d684e5',
    'lkm-aarch64-android16-6.12_kernelsu.ko': '877286f81d500c4ec546c96e9718c186b7379573c97ba5d5a35dd9a91465d076',
    'KernelSU_v3.3.0_32601-release.apk': 'c197060ecb89702e7d54a4c95e29cf5e8d97369bbbb436979ab7fd6bcde7b077',
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def host_check():
    sys_name = platform.system()
    machine = platform.machine().lower()
    if sys_name == 'Darwin' and machine in ('arm64', 'aarch64'):
        return
    if sys_name == 'Windows' and machine in ('amd64', 'x86_64'):
        return
    raise RuntimeError(
        f'Unsupported platform: {platform.system()} {platform.machine()}. '
        'This release supports Apple Silicon macOS and Windows 11 x64.')


def emulator_binary(sdk=None):
    path = Path(sdk or sdk_path())
    if sys.platform == 'win32':
        qemu_arm64 = path / 'emulator/qemu/windows-x86_64/qemu-system-aarch64.exe'
        if qemu_arm64.is_file():
            return qemu_arm64
        candidate = path / 'emulator/emulator.exe'
        return candidate if candidate.is_file() else (path / 'emulator/emulator')
    candidate = path / 'emulator/emulator'
    return candidate if candidate.is_file() else (path / 'emulator/emulator.exe')


def emulator_env(sdk=None, base_env=None):
    env = dict(os.environ if base_env is None else base_env)
    if sys.platform == 'win32':
        sdk_emu = Path(sdk or sdk_path()) / 'emulator'
        extra_paths = [
            str(sdk_emu),
            str(sdk_emu / 'lib64'),
            str(sdk_emu / 'lib64/qt/lib'),
            str(sdk_emu / 'lib64/gles_swiftshader'),
            str(sdk_emu / 'lib64/vulkan'),
            str(sdk_emu / 'qemu/windows-x86_64'),
        ]
        existing = env.get('PATH', '')
        env['PATH'] = ';'.join(extra_paths) + (';' + existing if existing else '')
        env['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(sdk_emu / 'lib64/qt/plugins')
        env['QT_PLUGIN_PATH'] = str(sdk_emu / 'lib64/qt/plugins')
    env.setdefault('ANDROID_EMU_NO_HANG_DETECTOR', '1')
    env.setdefault('ANDROID_EMU_DISABLE_CRASHHANDLER', '1')
    return env


def adb_binary(sdk=None):
    path = Path(sdk or sdk_path())
    candidate = path / 'platform-tools/adb.exe'
    return candidate if candidate.is_file() else (path / 'platform-tools/adb')


def sdk_path(explicit=None):
    candidates = [
        explicit,
        os.environ.get('ANDROID_SDK_ROOT'),
        os.environ.get('ANDROID_HOME'),
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Android/Sdk' if sys.platform == 'win32' else None,
        Path.home() / 'Library/Android/sdk' if sys.platform == 'darwin' else None,
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser().resolve()
            has_emulator = (path / 'emulator/emulator').is_file() or (path / 'emulator/emulator.exe').is_file()
            has_adb = (path / 'platform-tools/adb').is_file() or (path / 'platform-tools/adb.exe').is_file()
            if has_emulator and has_adb:
                return path
    raise RuntimeError('Install Android Studio Emulator and Platform-Tools, or set ANDROID_HOME / ANDROID_SDK_ROOT.')


def avd_home():
    if os.environ.get('ANDROID_AVD_HOME'):
        return Path(os.environ['ANDROID_AVD_HOME']).expanduser().resolve()
    if os.environ.get('ANDROID_EMULATOR_HOME'):
        return (Path(os.environ['ANDROID_EMULATOR_HOME']).expanduser() / 'avd').resolve()
    if os.environ.get('ANDROID_SDK_HOME'):
        sdk_home = Path(os.environ['ANDROID_SDK_HOME']).expanduser().resolve()
        if (sdk_home / '.android/avd').is_dir():
            return sdk_home / '.android/avd'
        if (sdk_home / 'avd').is_dir():
            return sdk_home / 'avd'
        return sdk_home / '.android/avd'
    if os.environ.get('ANDROID_USER_HOME'):
        return (Path(os.environ['ANDROID_USER_HOME']).expanduser() / 'avd').resolve()
    return (Path.home() / '.android/avd').resolve()


def tool(name, formula):
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == 'win32':
        exe_name = name if name.endswith('.exe') else (name + '.exe')
        try:
            sdk = sdk_path()
            for sub in ('emulator/bin64', 'platform-tools', 'emulator'):
                candidate = sdk / sub / exe_name
                if candidate.is_file():
                    return str(candidate)
        except RuntimeError:
            pass
    for prefix in (Path('/opt/homebrew'), Path('/usr/local')):
        for directory in ('bin', 'sbin'):
            path = prefix / 'opt' / formula / directory / name
            if path.is_file():
                return str(path)
    raise RuntimeError(f'Missing {name}. Install it or make it available in PATH / Android SDK.')


def port_free(port):
    # Check console and ADB endpoints, without contacting any guest.
    for endpoint in (port, port + 1):
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1', endpoint)) == 0:
                raise RuntimeError(f'Port {endpoint} is occupied. No emulator was stopped.')


def firmware_idle(fallback_port=DEFAULT_PORT):
    ports = set()
    for name in ('runtime.json', 'instances.json'):
        path = ROOT / 'local' / name
        if path.exists():
            data = json.loads(path.read_text())
            ports.update([data['port']] if name == 'runtime.json' else data.values())
    for port in ports or {fallback_port}:
        port_free(port)


def fetch_ksu(names):
    cache = ROOT / 'tools'
    cache.mkdir(exist_ok=True)
    for name in names:
        path = cache / name
        if not path.exists():
            url = f'https://github.com/tiann/KernelSU/releases/download/{KSU_VERSION}/{name}'
            temporary = path.with_suffix(path.suffix + '.part')
            print('Downloading official KernelSU asset:', name, flush=True)
            secure_urlretrieve(url, temporary)
            if sha256(temporary) != KSU_ASSETS[name]:
                temporary.unlink()
                raise RuntimeError('KernelSU download checksum mismatch: ' + name)
            temporary.replace(path)
        if sha256(path) != KSU_ASSETS[name]:
            raise RuntimeError('KernelSU checksum mismatch: ' + name)
        if name.startswith('ksud-'):
            path.chmod(0o755)
    return cache


def runtime():
    path = ROOT / 'local/runtime.json'
    if path.exists():
        config = json.loads(path.read_text())
        config['sdk'] = str(sdk_path(config.get('sdk')))
        return config
    return {'sdk': str(sdk_path()), 'name': DEFAULT_NAME, 'port': DEFAULT_PORT}


def adb(config, *arguments, **kwargs):
    # There is deliberately no global server reset or implicit device selection.
    return subprocess.run([str(adb_binary(config['sdk'])),
                           '-s', f"emulator-{config['port']}", *arguments], **kwargs)
