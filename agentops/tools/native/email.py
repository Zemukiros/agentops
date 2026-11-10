"""send_email (ADR-008): risk `external`, so it is PROPOSAL-ONLY for specialists (ADR-003).

The executor below is real (SMTP to SMTP_HOST:SMTP_PORT; Mailpit in dev, which captures mail
and delivers nothing). Specialists can never reach it: the registry turns every specialist call
into a ProposedAction. Phase 4's execute_actions will call it after human approval, behind the
side_effects table. The idempotency key is passed as the Message-ID (ADR-004).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from pydantic import Field

from agentops.settings import Settings
from agentops.tools.spec import ToolContext, ToolInput, ToolOutput, ToolSpec


class EmailIn(ToolInput):
    # One plain address; no whitespace or newlines anywhere (blocks header injection).
    to: str = Field(
        max_length=254,
        pattern=r"^[^@\s<>,;]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,63}$",
    )
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    body: str = Field(min_length=1, max_length=20_000)


class EmailDisabledError(RuntimeError):
    pass


def _send(settings: Settings, msg: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)


def email_disabled(settings: Settings) -> str | None:
    return settings.email_disabled_reason


async def send_email(
    args: EmailIn, ctx: ToolContext, *, message_id: str | None = None
) -> ToolOutput:
    if reason := ctx.settings.email_disabled_reason:  # never silently dropped (ADR-050)
        raise EmailDisabledError(reason)
    msg = EmailMessage()
    msg["From"] = ctx.settings.smtp_from
    msg["To"] = str(args.to)
    msg["Subject"] = args.subject
    if message_id:
        msg["Message-ID"] = f"<{message_id}@agentops>"
    msg.set_content(args.body)
    await asyncio.to_thread(_send, ctx.settings, msg)
    return ToolOutput(text=f"sent to {args.to}", data={"message_id": msg["Message-ID"]})


EMAIL_TOOLS = [
    ToolSpec(
        name="send_email",
        description=(
            "Send an email. This is never sent during the task: it is recorded as a proposed "
            "action for a human to approve afterwards."
        ),
        input_model=EmailIn,
        fn=send_email,
        allowed_agents=frozenset({"writing"}),
        risk="external",
        scope="outside",
        timeout_s=20,
        rate_per_min=10,
        meta={
            "idempotency_kw": "message_id",  # the side_effects key becomes the Message-ID
            "disabled_reason": email_disabled,  # settings -> reason | None (ADR-050)
        },
    ),
]
