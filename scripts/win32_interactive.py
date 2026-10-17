import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('lpReserved', wintypes.LPWSTR),
        ('lpDesktop', wintypes.LPWSTR),
        ('lpTitle', wintypes.LPWSTR),
        ('dwX', wintypes.DWORD),
        ('dwY', wintypes.DWORD),
        ('dwXSize', wintypes.DWORD),
        ('dwYSize', wintypes.DWORD),
        ('dwXCountChars', wintypes.DWORD),
        ('dwYCountChars', wintypes.DWORD),
        ('dwFillAttribute', wintypes.DWORD),
        ('dwFlags', wintypes.DWORD),
        ('wShowWindow', wintypes.WORD),
        ('cbReserved2', wintypes.WORD),
        ('lpReserved2', ctypes.POINTER(ctypes.c_byte)),
        ('hStdInput', wintypes.HANDLE),
        ('hStdOutput', wintypes.HANDLE),
        ('hStdError', wintypes.HANDLE),
    ]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('hProcess', wintypes.HANDLE),
        ('hThread', wintypes.HANDLE),
        ('dwProcessId', wintypes.DWORD),
        ('dwThreadId', wintypes.DWORD),
    ]

class InteractiveProcess:
    def __init__(self, hProcess, pid):
        self._handle = hProcess
        self.pid = pid
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        status = ctypes.windll.kernel32.WaitForSingleObject(self._handle, 0)
        if status == 0:
            code = wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code))
            self.returncode = code.value
            return self.returncode
        return None

    def terminate(self):
        ctypes.windll.kernel32.TerminateProcess(self._handle, 1)

    def kill(self):
        self.terminate()

def spawn_process(command, env=None, log_path=None, desktop='WinSta0\\Default'):
    cmd_str = subprocess.list2cmdline(command)
    si = STARTUPINFO()
    si.cb = ctypes.sizeof(STARTUPINFO)
    si.lpDesktop = desktop

    log_handle = None
    if log_path:
        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ('nLength', wintypes.DWORD),
                ('lpSecurityDescriptor', ctypes.c_void_p),
                ('bInheritHandle', wintypes.BOOL),
            ]
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.bInheritHandle = True

        GENERIC_WRITE = 0x40000000
        CREATE_ALWAYS = 2
        FILE_ATTRIBUTE_NORMAL = 0x80
        log_handle = ctypes.windll.kernel32.CreateFileW(
            str(log_path), GENERIC_WRITE, 1 | 2, ctypes.byref(sa), CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None
        )
        si.dwFlags |= 0x00000100 # STARTF_USESTDHANDLES
        si.hStdOutput = log_handle
        si.hStdError = log_handle
        si.hStdInput = ctypes.windll.kernel32.GetStdHandle(-10)

    env_block = None
    if env is not None:
        env_str = ''.join(f'{k}={v}\0' for k, v in sorted(env.items())) + '\0'
        env_block = ctypes.create_unicode_buffer(env_str)

    pi = PROCESS_INFORMATION()
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    flags = CREATE_NEW_PROCESS_GROUP | (CREATE_UNICODE_ENVIRONMENT if env_block else 0)

    res = ctypes.windll.kernel32.CreateProcessW(
        None, cmd_str, None, None, True if log_handle else False,
        flags, env_block, None, ctypes.byref(si), ctypes.byref(pi)
    )
    if log_handle:
        ctypes.windll.kernel32.CloseHandle(log_handle)

    if not res:
        err = ctypes.GetLastError()
        raise OSError(f"CreateProcessW failed with error {err}")

    ctypes.windll.kernel32.CloseHandle(pi.hThread)
    return InteractiveProcess(pi.hProcess, pi.dwProcessId)

if __name__ == '__main__':
    print("Testing InteractiveProcess helper")
