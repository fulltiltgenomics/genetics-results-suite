"""Slack webhook helper for posting block-formatted messages."""

import logging

import requests

logger = logging.getLogger(__name__)


def send_slack_message(webhook_url: str, blocks: list[dict]) -> bool:
    """POST a blocks payload to a Slack incoming webhook.

    Returns True on success (2xx response), False otherwise.
    """
    try:
        resp = requests.post(
            webhook_url,
            json={"blocks": blocks},
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
