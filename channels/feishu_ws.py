"""channels/feishu_ws.py —— 飞书长连接接收端（独立进程运行，纯 producer）

用 lark 长连接客户端监听 im.message.receive_v1。职责只有三件事：
收到消息 → 立即回执（满足 3 秒）→ 投递 arq 队列。

消息处理（Agent 执行）全部在 arq worker 进程完成，完成后由 worker
按 payload.reply_to 把结果发回飞书会话——收发与处理彻底分离，
慢任务获得持久化/失败重试/重启不丢的可靠性。

运行：uv run python -m channels.feishu_ws
（需先 uv add lark-oapi，.env 配好飞书凭证；Redis 与 arq worker 在跑）
"""
import asyncio
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from channels.base import InboundMessage
from channels.feishu import FeishuChannel
from infra.settings import get_settings
from infra.logging import get_logger
from tasks.queue import enqueue_issue, get_arq_redis

logger = get_logger()


def extract_feishu_message(event: P2ImMessageReceiveV1) -> InboundMessage | None:
    """从飞书消息事件解析出归一化 InboundMessage（仅处理文本）。

    长连接版和 Webhook 版都用它解析——避免重复（DRY）。
    飞书事件结构是层层嵌套的：event.event.message 才是消息体、event.event.sender 是发送者。
    """
    msg = event.event.message      # 消息体（含 chat_id、content、message_type 等）
    sender = event.event.sender    # 发送者信息
    chat_id = msg.chat_id          # 会话标识 → 我们用它做 conversation_id（同会话续聊的关键）
    # open_id 是发送者在本应用下的唯一标识；做了空值兜底，拿不到就标 unknown
    open_id = sender.sender_id.open_id if sender and sender.sender_id else "unknown"
    text = ""
    try:
        # ⚠️ 关键易错点：飞书文本消息的 content 不是纯文本，而是 JSON 字符串：'{"text":"你好"}'
        # 所以要先 json.loads 再取 "text" 字段——直接当字符串用会拿到一串带引号的 JSON。
        text = json.loads(msg.content).get("text", "").strip()
    except Exception:
        pass                       # 非文本消息（图片/文件等）content 结构不同，只处理文本
    if not text:
        return None                # 空文本或非文本消息：忽略
    return InboundMessage(channel="feishu", user_id=open_id, text=text,
                          conversation_id=chat_id, raw=None)


async def run():
    """启动长连接，常驻接收。本进程只收消息/回执/入队，处理与回发都在 arq worker。"""
    s = get_settings()
    feishu = FeishuChannel()
    arq_redis = await get_arq_redis()
    loop = asyncio.get_running_loop()

    async def _enqueue(inbound: InboundMessage):
        await feishu.send(inbound.conversation_id, "收到，正在处理…")   # 先回执
        try:
            await enqueue_issue(arq_redis, {
                "channel": inbound.channel,
                "user_id": inbound.user_id,
                "text": inbound.text,
                "conversation_id": inbound.conversation_id,
                "reply_to": {"type": "feishu", "chat_id": inbound.conversation_id},
            })
        except Exception as e:  # noqa: BLE001
            logger.exception("消息入队失败：{}", e)
            await feishu.send(inbound.conversation_id, "处理出错了，请稍后再试。")

    def on_message(event: P2ImMessageReceiveV1) -> None:
        # SDK 回调是同步的，且运行在 SDK 自己的线程——用 run_coroutine_threadsafe
        # 把协程安全地丢回我们的事件循环执行（不是游离的 create_task）。
        inbound = extract_feishu_message(event)
        if inbound is None:
            return
        logger.info("飞书收到消息：chat={} user={}", inbound.conversation_id, inbound.user_id)
        asyncio.run_coroutine_threadsafe(_enqueue(inbound), loop)

    handler = (
        lark.EventDispatcherHandler.builder("", "")   # 长连接模式 encrypt_key/token 可空
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    cli = lark.ws.Client(
        s.feishu_app_id, s.feishu_app_secret.get_secret_value(),
        event_handler=handler, log_level=lark.LogLevel.INFO,
    )
    logger.info("飞书长连接启动…（确保飞书后台已选『长连接接收事件』并发布版本）")
    try:
        # cli.start() 是阻塞的；放到线程里跑，保持本事件循环活着以处理回调协程
        await asyncio.to_thread(cli.start)
    finally:
        await arq_redis.aclose()


if __name__ == "__main__":
    import sys
    # Windows 默认 ProactorEventLoop 与 psycopg 异步不兼容，切到 SelectorEventLoop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())