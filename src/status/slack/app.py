"""Slack Bolt app entrypoint (Socket Mode)."""

from __future__ import annotations

import logging

from status.config import get_settings
from status.slack.handlers import register_handlers

log = logging.getLogger(__name__)


class SlackAppError(RuntimeError):
    pass


def run_socket_mode() -> None:
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise SlackAppError(
            "slack-bolt is not installed. Run: pip install -e '.[slack]'"
        ) from exc

    settings = get_settings()
    if not settings.slack_bot_token or not settings.slack_app_token:
        raise SlackAppError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set")

    app = App(token=settings.slack_bot_token)
    register_handlers(app, bot_token=settings.slack_bot_token)
    log.info("starting Slack socket mode handler")
    SocketModeHandler(app, settings.slack_app_token).start()
