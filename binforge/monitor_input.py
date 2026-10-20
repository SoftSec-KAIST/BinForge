"""Terminal input for read-only live monitors."""

import os
import select
import sys
import termios
import time
import tty


class QuitKey:
    """Wait for a refresh interval, returning true when q is pressed."""

    def __init__(self, fd: int | None = None):
        self.fd = fd
        self.saved = None

    def __enter__(self):
        if self.fd is None:
            try:
                self.fd = sys.stdin.fileno()
            except (AttributeError, OSError, ValueError):
                pass
        if self.fd is not None and os.isatty(self.fd):
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def wait(self, seconds: float) -> bool:
        if self.fd is None:
            time.sleep(seconds)
            return False
        ready, _, _ = select.select([self.fd], [], [], seconds)
        if not ready:
            return False
        key = os.read(self.fd, 1)
        if not key:
            self.fd = None
        return key.lower() == b"q"

    def __exit__(self, _type, _value, _traceback):
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
