"""Slack webhook helper for posting block-formatted messages."""

import logging

import requests

logger = logging.getLogger(__name__)


def send_slack_message(webhook_url: str, blocks: list[dict]) -> bool:
    """POST a blocks payload to a Slack incoming webhook.

    Returns True on success (2xx response), False otherwise.
    Splits into multiple messages if blocks exceed Slack's 50-block limit.
    """
    # slack limit: 50 blocks per message
    chunks = [blocks[i:i + 48] for i in range(0, len(blocks), 48)]

    success = True
    for chunk in chunks:
        ok = _post_blocks(webhook_url, chunk)
        success = success and ok
    return success


def _post_blocks(webhook_url: str, blocks: list[dict]) -> bool:
    try:
        resp = requests.post(
            webhook_url,
            json={"text": "Monitor report", "blocks": blocks},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code < 300:
            return True
        logger.warning(
            "slack webhook returned %d: %s", resp.status_code, resp.text[:200]
        )
        return False
    except Exception as exc:
        logger.error("failed to send slack message: %s", exc)
        return False
