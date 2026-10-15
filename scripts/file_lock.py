"""Cross-platform advisory file locking for HyperOS-AVD."""
import os
import sys

if sys.platform == 'win32':
    import msvcrt

    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_SH = 1
    LOCK_UN = 8

    def flock(fd_or_stream, operation):
        """Emulate POSIX flock semantics using Windows msvcrt.locking."""
        if hasattr(fd_or_stream, 'fileno'):
            fileno = fd_or_stream.fileno()
        elif isinstance(fd_or_stream, int):
            fileno = fd_or_stream
        else:
            raise TypeError('fd_or_stream must be an integer file descriptor or file-like object')

        # Remember and reset current file position for byte-range locking
        try:
            position = os.lseek(fileno, 0, os.SEEK_CUR)
            os.lseek(fileno, 0, os.SEEK_SET)
        except OSError:
            position = 0

        try:
            if operation & LOCK_UN:
                msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
            else:
                mode = msvcrt.LK_NBLCK if (operation & LOCK_NB) else msvcrt.LK_LOCK
                msvcrt.locking(fileno, mode, 1)
        except OSError as exc:
            raise BlockingIOError(f'Resource temporarily unavailable: {exc}') from exc
        finally:
            try:
                os.lseek(fileno, position, os.SEEK_SET)
            except OSError:
                pass

    # Register shim in sys.modules so 'import fcntl' works across existing tests and modules
    if 'fcntl' not in sys.modules:
        import types
        shim = types.ModuleType('fcntl')
        shim.LOCK_EX = LOCK_EX
        shim.LOCK_NB = LOCK_NB
        shim.LOCK_SH = LOCK_SH
        shim.LOCK_UN = LOCK_UN
        shim.flock = flock
        sys.modules['fcntl'] = shim

else:
    import fcntl
    LOCK_EX = fcntl.LOCK_EX
    LOCK_NB = fcntl.LOCK_NB
    LOCK_SH = fcntl.LOCK_SH
    LOCK_UN = fcntl.LOCK_UN
    flock = fcntl.flock
