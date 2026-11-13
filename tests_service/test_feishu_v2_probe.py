"""The live probe must never enqueue unrelated or group messages."""
import json
from types import SimpleNamespace as NS
import pytest
from scripts.verify_feishu_v2 import verification_payload, TOKEN, REPLY


@pytest.mark.parametrize("text,chat_type,event_id,accepted", [
    (TOKEN, "p2p", "event-v2", True),
    ("hello", "p2p", "event-v2", False),
    (TOKEN, "group", "event-v2", False),
    (TOKEN, "p2p", None, False),
])
def test_live_probe_is_scoped(text: str, chat_type: str, event_id: str | None, accepted: bool) -> None:
    """Only the authorized trigger creates a durable job with a same-chat reply."""
    event = NS(header=NS(event_id=event_id), event=NS(
        message=NS(chat_type=chat_type, message_type="text", chat_id="test-chat",
                   content=json.dumps({"text": text}), message_id=None),
        sender=NS(sender_id=NS(open_id="test-user"))))
    result = verification_payload(event, "test-tenant")
    assert (result is not None) is accepted
    if result:
        payload, identifier = result
        assert payload["reply_to"] == {"type": "feishu", "chat_id": "test-chat"}
        assert payload["tenant_id"] == "test-tenant" and REPLY in payload["text"]
        assert identifier == verification_payload(event, "test-tenant")[1]
