#!/usr/bin/env python3
# 把 serve 以完全脱离的方式拉起来：新会话 + 关掉继承的 fd + 输出进 serve.log。
# 这样调用方（看门狗脚本 / 终端）可以立刻退出，不会被子进程拖住。
import os
import subprocess

BRIDGE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    log = open(os.path.join(BRIDGE, "serve.log"), "ab", buffering=0)
    p = subprocess.Popen(
        [os.path.join(BRIDGE, ".venv", "bin", "python"), "bridge.py", "serve"],
        cwd=BRIDGE,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    print(p.pid)


if __name__ == "__main__":
    main()
