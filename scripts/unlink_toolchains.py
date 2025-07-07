import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge.utils import *

arch_prefix_list = [
    "i686-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
    "arm-unknown-linux-gnueabi",
    "aarch64-unknown-linux-gnu",
    "mipsel-unknown-linux-gnu",
    "mips64el-unknown-linux-gnu",
    "mips-unknown-linux-gnu",
    "mips64-unknown-linux-gnu",
    "powerpc-unknown-linux-gnu",
    "powerpc64-unknown-linux-gnu",
    "powerpc64le-unknown-linux-gnu",
]

def unlink_toolchains() -> None:
    for arch_prefix in arch_prefix_list:
        print(arch_prefix)
        command = f"sudo update-alternatives --remove-all {arch_prefix}-gcc"
        execute_command(command=command, DEBUG_LEVEL=0)
    # find /usr -xtype l
    # sudo find /usr -xtype l -delete

if __name__ == "__main__":
    unlink_toolchains()

# EOF
