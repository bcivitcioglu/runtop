"""Read-only discovery and connectivity diagnostics, without stats or disk scans."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable

from runtop.data.backend import LiveBackend
from runtop.data.engine import EngineClient
from runtop.data.models import Target
from runtop.data.remote import RemoteClient


async def diagnose(backend: LiveBackend | None = None, emit: Callable[[str], object] = print) -> int:
    backend = backend or LiveBackend()
    for name in ("limactl", "colima", "docker"):
        emit(f"{name}: {shutil.which(name) or 'not installed (optional)'}")
    failed = False
    try:
        targets = await backend.discover()
        if backend.discovery_error:
            emit(f"Discovery warning: {backend.discovery_error}")
            failed = True
        if not targets:
            emit("No targets found. Start a Lima/Colima Docker VM or configure a Unix-socket Docker context.")
            return 1

        async def check(t: Target) -> bool:
            mode = "read-only" if t.read_only else "local management"
            emit(f"\n{t.key} · {mode}\n  {t.endpoint}")
            if t.vm and not t.vm.running:
                emit(f"  VM {t.vm.status}; start it to connect.")
                return False
            try:
                if t.is_remote:
                    await RemoteClient(t.name).version()
                else:
                    async with EngineClient(t.socket_path) as engine:
                        if not await engine.ping():
                            raise RuntimeError("daemon did not answer OK")
                emit(f"  {t.key}: connected")
                return False
            except Exception as e:
                emit(f"  {t.key}: connection failed: {e}")
                return True

        # Bounded so a large context list cannot flood SSH with connections.
        sem = asyncio.Semaphore(4)
        async def bounded(t: Target) -> bool:
            async with sem:
                return await check(t)
        results = await asyncio.gather(*(bounded(t) for t in targets))
        return int(failed or any(results))
    except Exception as e:
        emit(f"Discovery failed: {e}")
        return 1
    finally:
        await backend.aclose()
