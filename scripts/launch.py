#!/usr/bin/env python3
"""Cold-boot only the project AVD and initialize KernelSU on fresh userdata."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from common import ROOT, adb, emulator_binary, emulator_env, fetch_ksu, host_check, port_free, runtime
from setup import configure

VULKAN_BASE_FEATURES = 'VulkanBatchedDescriptorSetUpdate'


def is_os4():
    build = ROOT / 'local/build.json'
    return build.is_file() and json.loads(build.read_text()).get('source') in (
        'official-hongkong-ota', 'official-yingtian-ota')


def vulkan_features(build):
    """Select audited descriptor, submission and presentation workarounds."""
    if build.get('source') == 'official-yingtian-ota':
        return ['-feature', VULKAN_BASE_FEATURES]
    if (build.get('source') == 'official-hongkong-ota'
            and build.get('hyperos') == '4.0.18.0.XFRCNXM'):
        from phone_profile import profile_from_build
        from patch_pad_hwui import PHONE_BEFORE, PHONE_AFTER, PHONE_PROFILE
        profile = profile_from_build(build)
        expected = {'before': PHONE_BEFORE, 'after': PHONE_AFTER, 'profile': PHONE_PROFILE}
        if (profile['pins']['hwui'] == PHONE_BEFORE and build.get('hwui') == expected
                and build.get('hwui_renderer') == 'skiavk'):
            # Keep presentation behavior and avoid the observed gfxstream
            # queue-sequence stall in this verified dual-display workload.
            return ['-feature', VULKAN_BASE_FEATURES +
                    ',-GLAsyncSwap,-VulkanQueueSubmitWithCommands']
    return []


def prepare_userdata(config):
    """Repair disk/filesystem mismatches before the owned Emulator can open them."""
    avd = ROOT / 'avd' / (config['name'] + '.avd')
    properties = dict(line.split('=', 1) for line in (avd / 'config.ini').read_text().splitlines()
                      if '=' in line)
    storage = config.get('hardware', {}).get('disk.dataPartition.size',
                                             properties.get('disk.dataPartition.size'))
    if (not isinstance(storage, str) or not storage.endswith('G') or not storage[:-1].isdigit()
            or not 6 <= int(storage[:-1]) <= 1024):
        raise RuntimeError('Missing or invalid userdata capacity in this AVD configuration.')
    from manage import resize, validate_userdata
    from userdata_resize import OfflineBackupRequiredError, PENDING
    def offline_resize():
        try:
            return resize(Path(config['sdk']), avd, int(storage[:-1]), allow_guest=True)
        except OfflineBackupRequiredError:
            # Only a validated encrypted-growth source can request a backup;
            # unknown layouts and mismatched chains keep their refusal.
            return {'guest_required': True}
    pending = avd / PENDING
    recovered = None
    if pending.exists() or pending.is_symlink():
        # An interrupted activation can temporarily have no active base. Its
        # original chain must be recovered before considering a fresh template.
        recovered = offline_resize()
    userdata = avd / 'userdata-qemu.img'
    if not userdata.exists():
        from userdata_resize import check_dependencies
        check_dependencies(Path(config['sdk']))
        print('Creating fresh userdata from the clean release template.', flush=True)
        shutil.copyfile(ROOT / 'images/userdata.img', userdata)
    validate_userdata(Path(config['sdk']), avd)
    result = recovered if recovered is not None else offline_resize()
    if isinstance(result, dict) and result.get('guest_required'):
        from manage import backup, prepare_storage
        folder = backup(ROOT, config['name'])
        result = prepare_storage(ROOT, config['name'], config['port'],
                                 Path(config['sdk']), int(storage[:-1]), folder)
    return result


def skip_oobe(config):
    command = '\n'.join([
        'set -e',
        'settings put global device_provisioned 1',
        'settings put secure user_setup_complete 1',
        'pm disable-user --user 0 com.android.provision',
        'am force-stop com.android.provision',
        'settings put system screen_brightness_mode 0',
        'settings put system accelerometer_rotation 0',
        'kill -STOP $(pidof com.xiaomi.finddevice) 2>/dev/null || true',
        'kill -STOP $(pidof com.xiaomi.aicr) 2>/dev/null || true',
        'input keyevent 224',
        'wm dismiss-keyguard',
        'am start -W -a android.intent.action.MAIN -c android.intent.category.HOME '
        '-n com.miui.home/com.miui.home.launcher.Launcher',
    ])
    adb(config, 'shell', 'su -W -c ' + shlex.quote(command), check=True, timeout=30)
    state = adb(config, 'shell',
                'settings get global device_provisioned\n'
                'settings get secure user_setup_complete\n'
                'pm list packages -d com.android.provision',
                capture_output=True, text=True, check=True, timeout=15).stdout.splitlines()
    if state != ['1', '1', 'package:com.android.provision']:
        raise RuntimeError('OOBE bypass validation failed: ' + repr(state))
    print('OOBE skipped. Existing userdata was preserved.', flush=True)


def migrate_boot_service_helpers(config):
    """Refresh only authenticated legacy hooks; keep optional failures local."""
    from apply_boot_service_fix import migrate_kernel_helper, migrate_uninstall_hook, migrate_lifecycle_hooks
    results = {}
    for name, migrate in (('kernel helper', migrate_kernel_helper),
                          ('uninstall hook', migrate_uninstall_hook),
                          ('lifecycle hooks', migrate_lifecycle_hooks)):
        try:
            outcome = migrate(config)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            reason = str(error).splitlines()[0][:240] if str(error) else type(error).__name__
            print(f'Legacy boot-service {name} is preserved: {reason}', flush=True)
            results[name] = {'migrated': False, 'preserved': True, 'error': reason}
            continue
        results[name] = outcome
        if outcome.get('migrated'):
            print(f'Legacy boot-service {name} migrated; current user settings are preserved.', flush=True)
        elif outcome.get('unsupported'):
            print(outcome['reason'], flush=True)
        elif outcome.get('pending') or outcome.get('preserved'):
            print(f'Legacy boot-service {name} lifecycle choice is preserved.', flush=True)
    return results


def optional_patch(outcomes, feature, install, *arguments, **options):
    """Skip only an explicitly unsupported, untouched optional workload."""
    from patch_outcome import UnsupportedPatch
    try:
        result = install(*arguments, **options)
    except UnsupportedPatch as error:
        reason = str(error).splitlines()[0][:240]
        outcomes[feature] = {'state': 'unsupported', 'reason': reason}
        print(f'{feature}: original workload preserved; {reason}', flush=True)
        return None
    outcomes[feature] = {'state': 'checked'}
    return result


def initialize(config, bypass_oobe=False, rotate_window=True, process=None, save_snapshot=False):
    timeout_scale = 3 if sys.platform == 'win32' else 1
    adb_timeout = 900 if sys.platform == 'win32' else 300
    deadline = time.monotonic() + adb_timeout
    while time.monotonic() < deadline:
        if process and process.poll() is not None:
            raise RuntimeError(
                f"Emulator process exited prematurely with code {process.returncode}. "
                "Inspect logs/emulator-current.log for details."
            )
        try:
            result = adb(config, 'shell', 'id', capture_output=True,
                         text=True, timeout=30 * timeout_scale)
            if result.returncode == 0 and 'uid=' in result.stdout:
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)
    else:
        raise RuntimeError(
            f'ADB did not become ready within {adb_timeout // 60} minutes. '
            'Inspect logs/emulator-current.log.')
    if is_os4():
        from os4_defaults import apply_sensor_defaults
        try:
            apply_sensor_defaults(config)
        except Exception as error:
            # Sensor setup is optional for boot; a slow cross-architecture getprop
            # must not prevent the watchdog stop and KernelSU initialization below.
            print(f'Warning: sensor defaults were not applied: {error}', flush=True)
    if is_os4() and sys.platform == 'win32':
        try:
            adb(config, 'shell', 'setprop ctl.stop hypsys_system; setprop ctl.stop misight',
                capture_output=True, text=True, timeout=10)
        except Exception:
            pass

    root_ready = False
    for attempt in range(10 if sys.platform == 'win32' else 2):
        try:
            root = adb(config, 'shell', "su -W -c 'id'", capture_output=True, text=True,
                       timeout=20 * timeout_scale)
            if root.returncode == 0 and 'uid=0(' in root.stdout:
                root_ready = True
                break
        except Exception:
            pass
        time.sleep(3)

    if not root_ready:
        binary = ROOT / 'tools/ksud-aarch64-linux-android'
        remote = '/data/local/tmp/hyperos-avd-ksud'
        try:
            adb(config, 'push', str(binary), remote, check=True, capture_output=True, timeout=30 * timeout_scale)
            adb(config, 'shell', f'chmod 755 {remote}', check=True, timeout=15 * timeout_scale)
            result = adb(config, 'shell', remote + ' debug su', input=remote + ' install\nexit\n',
                         text=True, capture_output=True, timeout=30 * timeout_scale)
            if result.returncode:
                print('Warning: KernelSU manual install returned: ' + result.stderr, flush=True)
        except Exception as error:
            print(f'Warning: KernelSU manual install attempt failed: {error}', flush=True)

    result = None
    for attempt in range(8 if sys.platform == 'win32' else 2):
        try:
            result = adb(config, 'shell', "su -W -c 'id; getenforce; /data/adb/ksud feature set selinux_hide 0; /data/adb/ksud feature save'",
                         capture_output=True, text=True, timeout=30 * timeout_scale)
            if 'uid=0(' in result.stdout and 'Enforcing' in result.stdout and result.returncode == 0:
                break
        except Exception:
            pass
        time.sleep(3)
    else:
        detail = (result.stdout + result.stderr) if result else 'timeout'
        if not result or 'uid=0(' not in result.stdout or 'Enforcing' not in result.stdout or result.returncode:
            print('Warning: KernelSU root/enforcing validation incomplete (' + detail + '), continuing boot monitoring...', flush=True)

    if sys.platform == 'win32':
        watchdog_script = (
            "su -W -c 'mkdir -p /data/adb/post-fs-data.d && "
            "if [ ! -f /data/adb/post-fs-data.d/00-watchdog.sh ]; then "
            "printf \"#!/system/bin/sh\\n/data/adb/ksud resetprop ro.hw_timeout_multiplier 20\\n"
            "/data/adb/ksud resetprop persist.sys.crashOnWatchdog 0\\n\" "
            "> /data/adb/post-fs-data.d/00-watchdog.sh && "
            "chmod 755 /data/adb/post-fs-data.d/00-watchdog.sh; fi'"
        )
        try:
            adb(config, 'shell', watchdog_script, capture_output=True, text=True,
                timeout=30 * timeout_scale)
        except subprocess.TimeoutExpired:
            pass
        watchdog_properties = [
            ('ro.hw_timeout_multiplier', '20'),
            ('persist.sys.crashOnWatchdog', '0'),
        ]
        for name, value in watchdog_properties:
            command = (
                "su -W -c 'if [ -x /data/adb/ksu/bin/resetprop ]; then "
                "/data/adb/ksu/bin/resetprop " + name + " " + value + "; "
                "else /data/adb/ksud resetprop " + name + " " + value + "; fi'"
            )
            try:
                adb(config, 'shell', command, capture_output=True, text=True,
                    timeout=30 * timeout_scale)
            except subprocess.TimeoutExpired:
                print(f'Warning: failed to set watchdog property {name}.', flush=True)
        state = adb(config, 'shell',
                    'getprop ro.hw_timeout_multiplier; getprop persist.sys.crashOnWatchdog',
                    capture_output=True, text=True, timeout=15 * timeout_scale)
        parts = state.stdout.split()
        multiplier = parts[0] if parts else ''
        print('Watchdog configuration: ' + ' '.join(parts), flush=True)
        if multiplier != '20':
            zygote = adb(config, 'shell', 'getprop init.svc.zygote',
                         capture_output=True, text=True, timeout=10 * timeout_scale)
            if zygote.stdout.strip() == 'running':
                adb(config, 'shell', "su -W -c 'setprop ctl.restart zygote'",
                    capture_output=True, timeout=15 * timeout_scale)
                print('Restarted zygote to apply watchdog timeout multiplier.', flush=True)
        else:
            print('Watchdog multiplier already active; Zygote restart skipped.', flush=True)

    # In cross-architecture software emulation, cold boot can take longer than Xiaomi's
    # default 20-minute BootMonitor timeout. Stop the watchdog early and verify that init
    # actually observed the request before waiting for boot completion.
    if is_os4() and sys.platform == 'win32':
        stopped = False
        for _ in range(10):
            try:
                adb(config, 'shell',
                    "su -c 'stop hypsys_system; setprop ctl.stop hypsys_system; "
                    "stop misight; setprop ctl.stop misight; "
                    "stop statsd; stop traced; stop traced_probes; stop incidentd; "
                    "stop neuralnetworks_hal_service_aidl_sample_all; "
                    "stop neuralnetworks_hal_service_aidl_sample_limited; "
                    "stop neuralnetworks_hal_service_shim_sample'",
                    capture_output=True, timeout=15 * timeout_scale)
                state = adb(config, 'shell', 'getprop init.svc.hypsys_system',
                            capture_output=True, text=True, timeout=10 * timeout_scale)
                if state.stdout.strip() == 'stopped':
                    stopped = True
                    print('Successfully stopped hypsys_system BootMonitor and idle telemetry services.', flush=True)
                    break
            except Exception:
                pass
            time.sleep(2)
        if not stopped:
            print('Warning: hypsys_system BootMonitor could not be stopped; '
                  'the guest may reboot at the 20-minute boot timeout.', flush=True)
    def apply_early_mapper_fix(config, timeout_scale=1):
        """Ensure ranchu IMapper does not crash HWC3 on host shared memory slots."""
        import hashlib
        from patch_mapper import BEFORE, AFTER, NATIVE
        try:
            state = adb(config, 'get-state', capture_output=True, text=True, timeout=5)
            if state.returncode != 0 or state.stdout.strip() != 'device':
                return False
            res = adb(config, 'shell', f"su -c '/data/adb/ksu/bin/busybox sha256sum {NATIVE} 2>/dev/null || sha256sum {NATIVE}'",
                      capture_output=True, text=True, timeout=10 * timeout_scale)
            if res.returncode != 0:
                return False
            current_hash = res.stdout.strip().split()[0] if res.stdout.strip() else ''
            if current_hash == AFTER:
                return True
            if current_hash != BEFORE:
                return False
            local_patched = ROOT / 'work/mapper.ranchu.patched.so' if (ROOT / 'work/mapper.ranchu.patched.so').is_file() else ROOT / 'mapper.ranchu.patched.so'
            if not local_patched.is_file():
                from patch_mapper import patch
                raw = adb(config, 'exec-out', f"su -c 'cat {NATIVE}'", capture_output=True, timeout=15 * timeout_scale).stdout
                if hashlib.sha256(raw).hexdigest() == BEFORE:
                    local_patched.write_bytes(patch(raw))
            remote_tmp = '/data/local/tmp/mapper.ranchu.so'
            adb(config, 'push', str(local_patched), remote_tmp, capture_output=True, timeout=15 * timeout_scale)
            cmd = (
                f"chown 0:0 {remote_tmp} && chmod 644 {remote_tmp} && "
                f"chcon u:object_r:same_process_hal_file:s0 {remote_tmp} && "
                f"/data/adb/ksu/bin/busybox nsenter -t 1 -m -- /data/adb/ksu/bin/busybox mount -o bind {remote_tmp} {NATIVE} && "
                f"/data/adb/ksu/bin/busybox nsenter -t 1 -m -- /data/adb/ksu/bin/busybox mount -o remount,bind,ro,nosuid,nodev {NATIVE} && "
                f"kill -9 $(pidof android.hardware.graphics.composer3-service.ranchu) 2>/dev/null || true"
            )
            adb(config, 'shell', f"su -c '{cmd}'", capture_output=True, text=True, timeout=15 * timeout_scale)
            print('Applied early ranchu IMapper fix; restarted HWC3 service.', flush=True)
            return True
        except Exception:
            return False

    boot_timeout = 1800 if sys.platform == 'win32' else 300
    deadline = time.monotonic() + boot_timeout
    last_notice = time.monotonic()
    early_services_handled = False
    while time.monotonic() < deadline:
        if process and process.poll() is not None:
            raise RuntimeError(
                f"Emulator process exited prematurely with code {process.returncode}. "
                "Inspect logs/emulator-current.log for details."
            )
        try:
            if not early_services_handled:
                if apply_early_mapper_fix(config, timeout_scale):
                    early_services_handled = True
                    try:
                        adb(config, 'shell',
                            "su -c 'stop hypsys_system; setprop ctl.stop hypsys_system; "
                            "stop misight; setprop ctl.stop misight; "
                            "stop statsd; stop traced; stop traced_probes; stop incidentd; "
                            "stop neuralnetworks_hal_service_aidl_sample_all; "
                            "stop neuralnetworks_hal_service_aidl_sample_limited; "
                            "stop neuralnetworks_hal_service_shim_sample'",
                            capture_output=True, timeout=10 * timeout_scale)
                        print('Successfully stopped hypsys_system BootMonitor and idle telemetry services.', flush=True)
                    except Exception:
                        pass
            completed = adb(config, 'shell', 'getprop sys.boot_completed', capture_output=True,
                            text=True, timeout=15)
            if completed.returncode == 0 and completed.stdout.strip() == '1':
                break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() - last_notice >= 30:
            elapsed = int(time.monotonic() - (deadline - boot_timeout))
            print(f"Waiting for system boot completion ({elapsed}s elapsed)...", flush=True)
            last_notice = time.monotonic()
        time.sleep(3)
    else:
        raise RuntimeError('Boot did not finish within timeout. Inspect logs/emulator-current.log.')
    if save_snapshot:
        print('Saving emulator snapshot default_boot...', flush=True)
        try:
            result = adb(config, 'emu', 'avd', 'snapshot', 'save', 'default_boot',
                         capture_output=True, text=True, timeout=300 * timeout_scale)
            if result.returncode:
                print('Warning: snapshot save failed: ' + result.stderr.strip(), flush=True)
            else:
                print('Snapshot saved: default_boot', flush=True)
        except Exception as error:
            print(f'Warning: snapshot save failed: {error}', flush=True)
    build = ROOT / 'local/build.json'
    metadata = json.loads(build.read_text()) if build.is_file() else {}
    source = metadata.get('source')
    outcomes = {}
    if source == 'official-hongkong-ota':
        try:
            from os4_defaults import apply_runtime
            apply_runtime(config, managed=True)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            reason = str(error).splitlines()[0][:240] if str(error) else type(error).__name__
            print('Warning: phone runtime defaults deferred: ' + reason, flush=True)
            outcomes['phone_runtime'] = {'state': 'deferred', 'error': reason}
    elif source == 'official-yingtian-ota':
        try:
            from os4_pad import apply_runtime, align_window
            apply_runtime(config, managed=True)
            if rotate_window:
                align_window(config)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            reason = str(error).splitlines()[0][:240] if str(error) else type(error).__name__
            print('Warning: Pad runtime defaults deferred: ' + reason, flush=True)
            outcomes['pad_runtime'] = {'state': 'deferred', 'error': reason}
    if is_os4():
        try:
            migrate_boot_service_helpers(config)
            # Both OS4 variants install the same two module artifacts. Guest
            # services select individual recipes by content and capability.
            from apply_native_compat import install as install_native_compat
            from core_context import load as load_core_context
            outcomes['core'] = install_native_compat(config, platform_context=load_core_context(ROOT))
            from apply_app_compat import install_prebuilt as install_apps
            outcomes['apps'] = install_apps(config, workspace=ROOT)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            reason = str(error).splitlines()[0][:240] if str(error) else type(error).__name__
            print('Warning: post-boot compatibility setup deferred: ' + reason, flush=True)
            outcomes['post_boot'] = {'state': 'deferred', 'error': reason}
    if bypass_oobe:
        skip_oobe(config)
    manager = adb(config, 'shell', 'pm path me.weishu.kernelsu', capture_output=True, text=True, timeout=15 * timeout_scale)
    if 'package:' not in manager.stdout:
        apk = ROOT / 'tools/KernelSU_v3.3.0_32601-release.apk'
        # MIUI's fresh setup rejects standard shell installs; use verified KernelSU root.
        remote = '/data/local/tmp/hyperos-avd-ksu.apk'
        adb(config, 'push', str(apk), remote, check=True, capture_output=True, timeout=30 * timeout_scale)
        try:
            adb(config, 'shell', 'su -W -c ' + shlex.quote('pm install -r ' + remote),
                check=True, timeout=60 * timeout_scale)
        finally:
            adb(config, 'shell', 'rm -f ' + remote, timeout=15 * timeout_scale)
    adb(config, 'shell', 'rm -f /data/local/tmp/hyperos-avd-ksud', timeout=15 * timeout_scale)
    (ROOT / 'local').mkdir(exist_ok=True)
    (ROOT / 'local/last-boot.json').write_text(json.dumps(
        {'name': config['name'], 'serial': f"emulator-{config['port']}",
         'validated_at': datetime.now().astimezone().isoformat(), 'root': result.stdout,
         'patch_outcomes': outcomes}, indent=2) + '\n')
    print(f"HyperOS is ready: emulator-{config['port']}, KernelSU root, SELinux Enforcing.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--no-host-color-fix', action='store_true',
                        help='Disable the OS4 macOS sRGB window tag for this launch')
    parser.add_argument('--skip-oobe', action='store_true',
                        help='Skip Xiaomi provisioning and open the native launcher')
    parser.add_argument('--tb-size', type=int, default=512,
                        help='TCG translation cache size in MiB on Windows; 0 uses the QEMU default')
    parser.add_argument('--tb-stats', action='store_true',
                        help='Collect low-overhead TCG lookup/translation statistics for info jit')
    parser.add_argument('--tb-hotness-file', type=Path, default=ROOT / 'local/tb-hotness.txt',
                        help='Persistent guest-PC hotspot index path on Windows')
    parser.add_argument('--tb-hotness-max', type=int, default=65536,
                        help='Maximum persistent hotspot entries (1024-4194304)')
    parser.add_argument('--gpu', choices=['auto', 'host', 'software', 'lavapipe', 'swiftshader', 'swangle', 'guest'],
                        help='Override the emulator GPU backend for this launch')
    parser.add_argument('--cores', type=int,
                        help='Override the guest vCPU count for this launch')
    parser.add_argument('--memory', type=int,
                        help='Override the guest RAM size in MiB for this launch')
    parser.add_argument('--machine', choices=['ranchu', 'virt'], default='ranchu',
                        help='Override the QEMU machine model on Windows')
    parser.add_argument('--cpu',
                        help='Override the QEMU CPU model on Windows')
    parser.add_argument('--qemu-extra', action='append', default=[],
                        help='Append raw QEMU arguments on Windows; repeat for each argument')
    parser.add_argument('--allow-snapshot-save', action='store_true',
                        help='Allow the emulator to save snapshots on exit')
    parser.add_argument('--snapshot', nargs='?', const='default_boot',
                        help='Load an emulator snapshot instead of cold booting')
    args = parser.parse_args()
    if args.tb_size < 0 or args.tb_size > 2048:
        parser.error('--tb-size must be between 0 and 2048 MiB')
    if not 1024 <= args.tb_hotness_max <= 4194304:
        parser.error('--tb-hotness-max must be between 1024 and 4194304')
    if args.cores is not None and not 1 <= args.cores <= 32:
        parser.error('--cores must be between 1 and 32')
    if args.memory is not None and not 1024 <= args.memory <= 65536:
        parser.error('--memory must be between 1024 and 65536 MiB')
    host_check()
    config = runtime()
    port_free(config['port'])
    avd_dir = ROOT / 'avd' / (config['name'] + '.avd')
    for lock_name in ['hardware-qemu.ini.lock', 'multiinstance.lock']:
        lock_path = avd_dir / lock_name
        if lock_path.exists():
            if lock_path.is_dir():
                shutil.rmtree(lock_path, ignore_errors=True)
            else:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
    snapshots_dir = avd_dir / 'snapshots'
    if snapshots_dir.is_dir():
        for snap_dir in snapshots_dir.iterdir():
            if snap_dir.is_dir() and not (snap_dir / 'snapshot.pb').is_file():
                shutil.rmtree(snap_dir, ignore_errors=True)
    if not (ROOT / 'images/userdata.img').is_file():
        raise RuntimeError('Run Setup.command with the release manifest first.')
    # Refresh relocated paths before every cold start; collision checks protect other AVDs.
    configure(Path(config['sdk']), config['name'], config['port'])
    fetch_ksu(['ksud-aarch64-linux-android', 'KernelSU_v3.3.0_32601-release.apk'])
    prepare_userdata(config)
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    log = logs / 'emulator-current.log'
    if log.exists():
        log.rename(logs / ('emulator-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log'))
    avd_config = ROOT / 'avd' / (config['name'] + '.avd') / 'config.ini'
    properties = dict(line.split('=', 1) for line in avd_config.read_text().splitlines() if '=' in line)
    memory = str(args.memory or int(properties.get('hw.ramSize', '2560')))
    build = ROOT / 'local/build.json'
    cores = str(args.cores or int(properties.get('hw.cpu.ncore', '2')))
    accel_mode = 'on' if sys.platform == 'darwin' else 'off'
    command = [str(emulator_binary(config['sdk'])), '-avd', config['name'],
               '-sysdir', str(ROOT / 'images'), '-port', str(config['port']),
               '-accel', accel_mode, '-gpu', args.gpu or 'host',
               '-memory', memory, '-cores', cores, '-show-kernel', '-verbose']
    if args.snapshot:
        command += ['-snapshot', args.snapshot]
    else:
        command += ['-no-snapshot-load']
    if not args.allow_snapshot_save:
        command += ['-no-snapshot-save']
    if args.headless or sys.platform == 'win32':
        command += ['-no-boot-anim']
    if sys.platform == 'win32':
        command += ['-feature', '-WindowsHypervisorPlatform,-HVF,-KVM', '-no-nested-warnings']
    if is_os4():
        command += ['-crash-report-mode', 'never']
    if build.is_file():
        # The guest supports batched updates; gfxstream also masks inline uniform
        # blocks in this mode, avoiding the observed MoltenVK descriptor crash.
        image_build = json.loads(build.read_text())
        command += vulkan_features(image_build)
        from rear_display_config import runtime_options
        command += runtime_options(image_build)
    if args.headless:
        command += ['-no-window']
    if sys.platform == 'darwin':
        from host_color import environment
        launch_environment = environment(ROOT, config['name'],
                                         enabled=not (args.headless or args.no_host_color_fix))
    else:
        print('[Notice] sRGB window calibration is macOS-specific; standard display rendering is active on Windows.', flush=True)
        launch_environment = emulator_env(config['sdk'])
        args.tb_hotness_file.parent.mkdir(parents=True, exist_ok=True)
        launch_environment['QEMU_TB_HOTNESS_FILE'] = str(args.tb_hotness_file)
        launch_environment['QEMU_TB_HOTNESS_MAX'] = str(args.tb_hotness_max)
        if args.tb_stats:
            launch_environment['QEMU_TB_STATS'] = '1'
        if args.gpu in ('swiftshader', 'swangle', 'software', 'lavapipe'):
            icd_name = 'lvp_icd.json' if args.gpu == 'lavapipe' else 'vk_swiftshader_icd.json'
            icd = Path(config['sdk']) / 'emulator/lib64/vulkan' / icd_name
            if icd.is_file():
                launch_environment['VK_ICD_FILENAMES'] = str(icd)
    if sys.platform == 'win32':
        command += ['-qemu', '-machine', 'type=' + args.machine]
        if args.cpu:
            command += ['-cpu', args.cpu]
        if args.tb_size:
            command += ['-tb-size', str(args.tb_size)]
        if args.qemu_extra:
            command += args.qemu_extra
    if sys.platform == 'win32' and not args.headless:
        from win32_interactive import spawn_process
        process = spawn_process(command, env=launch_environment, log_path=log, desktop=r'WinSta0\Default')
        print(f"Starting {config['name']} (PID {process.pid}) on interactive desktop. Log: {log}", flush=True)
        # The emulator stays running if setup fails; evidence and userdata are retained.
        initialize(config, bypass_oobe=args.skip_oobe, rotate_window=not args.headless,
                   process=process, save_snapshot=args.allow_snapshot_save)
    else:
        popen_kwargs = {}
        if sys.platform == 'win32':
            popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs['start_new_session'] = True
        output = log.open('wb')
        try:
            process = subprocess.Popen(command, env=launch_environment, stdout=output,
                                       stderr=subprocess.STDOUT, **popen_kwargs)
            print(f"Starting {config['name']} (PID {process.pid}). Log: {log}", flush=True)
            # The emulator stays running if setup fails; evidence and userdata are retained.
            initialize(config, bypass_oobe=args.skip_oobe, rotate_window=not args.headless,
                       process=process, save_snapshot=args.allow_snapshot_save)
        finally:
            output.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
