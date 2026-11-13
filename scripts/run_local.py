"""Start the local API or worker with a Windows-compatible event loop."""
import argparse
import asyncio
import sys
import signal
from arq.worker import Worker, get_kwargs


class LocalWorker(Worker):
    """Handle shutdown on Windows, where arq's SIGUSR1 fallback is unavailable."""
    async def close(self) -> None:
        """Cancel tasks using a supported signal before closing the arq resources."""
        if sys.platform == "win32" and not self._handle_signals:
            self.handle_sig(signal.SIGTERM)
            self._handle_signals = True
        await super().close()


def main() -> None:
    """Run the requested service using the local .env configuration."""
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=("web", "worker"))
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.service == "web":
        import uvicorn
        server = uvicorn.Server(uvicorn.Config("api.app:app", host="127.0.0.1", port=18700))
        asyncio.run(server.serve())
    else:
        from tasks.worker import WorkerSettings
        worker = LocalWorker(**get_kwargs(WorkerSettings), handle_signals=sys.platform != "win32")
        worker.run()


if __name__ == "__main__":
    main()
