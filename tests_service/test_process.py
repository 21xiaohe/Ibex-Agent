"""Subprocess compatibility for psycopg's Windows event loop."""
import asyncio
import sys
from sandbox.docker_manager import _run


def test_subprocess_with_database_compatible_loop() -> None:
    """A selector loop must still execute and capture a child process."""
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        code, output = loop.run_until_complete(_run([sys.executable, "-c", "print('child-ok')"]))
    finally:
        loop.close()
    assert code == 0
    assert "child-ok" in output
