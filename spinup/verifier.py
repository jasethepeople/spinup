from __future__ import annotations

import time
from dataclasses import dataclass

from .sandbox import Sandbox


@dataclass(slots=True)
class Verification:
    ok: bool
    reason: str


def verify(sandbox: Sandbox, port: int, timeout: int = 20) -> Verification:
    deadline = time.monotonic() + timeout
    last = "process or port is not ready"
    while True:
        if not sandbox.process_alive():
            return Verification(False, "start process exited")
        if sandbox.port_answers(port):
            return Verification(True, "process alive and port answered")
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    return Verification(False, last)
