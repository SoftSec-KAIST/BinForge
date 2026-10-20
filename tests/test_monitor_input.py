"""Interactive quit-key behavior shared by the two monitors."""

import os
import pty
import termios
import time
import unittest

from binforge.monitor_input import QuitKey


class QuitKeyTests(unittest.TestCase):
    def test_q_exits_without_enter_and_restores_terminal(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        original = termios.tcgetattr(slave)
        with QuitKey(slave) as keys:
            self.assertNotEqual(termios.tcgetattr(slave), original)
            self.assertTrue(termios.tcgetattr(slave)[3] & termios.ISIG)
            os.write(master, b"x")
            self.assertFalse(keys.wait(0.1))
            start = time.monotonic()
            os.write(master, b"q")
            self.assertTrue(keys.wait(1))
            self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(termios.tcgetattr(slave), original)

    def test_piped_q_and_eof(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, b"q")
        os.close(write_fd)
        with QuitKey(read_fd) as keys:
            self.assertTrue(keys.wait(1))
            self.assertFalse(keys.wait(1))


if __name__ == "__main__":
    unittest.main()
