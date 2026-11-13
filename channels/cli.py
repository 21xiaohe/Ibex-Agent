"""channels/cli.py —— CLI 渠道：终端里跟 Ibex 对话

直接调本机服务的 HTTP 接口（需先启动 api.app）。
用同一个 --session 多次调用即可续聊（thread_id 由 (cli, session) 稳定派生）。

运行：uv run python -m channels.cli --session mywork "帮我看下订单服务的定价逻辑"
"""
import argparse
import os
import asyncio

import httpx

BASE = "http://localhost:8000"


async def ask(session: str, text: str, base_url: str = BASE, api_key: str | None = None) -> None:
    """Send one request to the configured API endpoint and print the reply."""
    async with httpx.AsyncClient(timeout=300, headers={"X-API-Key": api_key or os.environ.get("IBEX_CLIENT_API_KEY", "")}) as client:
        # 走通用 webhook 的 generic 入口（conversation_id=session 实现 CLI 续聊）
        r = await client.post(f"{base_url.rstrip('/')}/webhook/generic", json={
            "text": text,
            "source": "cli",
            "conversation_id": f"cli:{session}",
        })
        r.raise_for_status()
        data = r.json()
        print("\nIbex:", data.get("reply", data))


def main() -> None:
    """Parse the endpoint, conversation and user prompt."""
    p = argparse.ArgumentParser(description="Ibex CLI")
    p.add_argument("--session", default="default", help="会话名（同名续聊）")
    p.add_argument("--base-url", default=BASE, help="Ibex API URL")
    p.add_argument("text", help="要对 Ibex 说的话")
    args = p.parse_args()
    asyncio.run(ask(args.session, args.text, args.base_url))


if __name__ == "__main__":
    main()
