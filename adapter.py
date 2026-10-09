"""Omarchy desktop-toast platform adapter for Hermes.

Turns a Hermes message into a native Quickshell notification toast (the
top-right popup cards) on an Omarchy / Hyprland desktop by shelling out to
``omarchy-notification-send``.

Why a platform plugin: cron delivery goes through the platform send path, so
registering a platform is what makes ``deliver: omatoast`` a valid cron target.
There is no cron lifecycle hook in Hermes to subscribe to, so this is the
supported way to catch cron output.

Outbound-only by design. Nothing is ever received from a toast, so
``handle_message`` is never called and no listener is started: ``connect()``
only marks the adapter live.

Targets: ``omatoast`` (home label) or ``omatoast:<label>``. A toast has no
routing, so the label is cosmetic — it just keeps ``deliver`` strings readable.

Settings are declared in ``plugin.yaml`` under ``config_schema`` and resolved at
send time via the plugin context, so editing them applies to the next message
with no restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import uuid
from typing import Any, Callable, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

PLATFORM_NAME = "omatoast"
NOTIFY_BIN = "omarchy-notification-send"
DEFAULT_APP_NAME = "Hermes"
DEFAULT_HOME_LABEL = "desktop"

HEADLINE_MAX = 80
BODY_MAX = 400
TIMEOUT_S = 10

# A toast that says "something is wrong" should look urgent. Matched
# case-insensitively against the whole message.
_CRITICAL_MARKERS = ("alert", "latch", "error", "failed", "failure", "critical")
_URGENCY_RANK = {"low": 0, "normal": 1, "critical": 2}

# Hermes wraps cron deliveries as:
#   Cronjob Response: <job name>
#   (job_id: <id>)
#   -------------
#   <payload>
_CRON_HEADLINE_RE = re.compile(r"^Cronjob Response:\s*(?P<name>.+?)\s*$", re.MULTILINE)
_JOB_ID_RE = re.compile(r"\(job_id:\s*(?P<id>[0-9a-fA-F]{4,})\)")
_JOB_ID_LINE_RE = re.compile(r"(?m)^\s*\(job_id:[^)]*\)\s*$")
_RULE_LINE_RE = re.compile(r"(?m)^\s*[-=_]{3,}\s*$")

# Defaults mirror plugin.yaml's config_schema.
_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "app_name": DEFAULT_APP_NAME,
    "min_urgency": "normal",
    "only_jobs": "",
    "skip_jobs": "",
    "strip_cron_wrapper": True,
}


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def _resolve_settings(getter: Optional[Callable[..., Any]]) -> dict:
    """Read the declared settings, falling back to the schema defaults."""
    out = dict(_SETTING_DEFAULTS)
    if getter is None:
        return out
    for key, default in _SETTING_DEFAULTS.items():
        try:
            value = getter(key, default)
        except Exception:  # a config read must never break delivery
            logger.debug("omatoast: could not read setting %r", key, exc_info=True)
            continue
        if value is not None:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# toast primitives
# --------------------------------------------------------------------------

def _dbus_address() -> str:
    """The session bus address to use.

    Cron scripts run with a reduced environment, so fall back to the per-user
    bus socket rather than assuming DBUS_SESSION_BUS_ADDRESS was inherited.
    """
    addr = (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "").strip()
    if addr:
        return addr
    runtime = (os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}").strip()
    return f"unix:path={runtime}/bus"


def _urgency_for(text: str) -> str:
    low = (text or "").lower()
    if any(marker in low for marker in _CRITICAL_MARKERS):
        return "critical"
    return "normal"


def _job_identity(content: str) -> tuple[str, str]:
    """``(job name, job id)`` extracted from a cron delivery; empty when absent."""
    name_match = _CRON_HEADLINE_RE.search(content or "")
    id_match = _JOB_ID_RE.search(content or "")
    return (
        name_match.group("name").strip() if name_match else "",
        id_match.group("id") if id_match else "",
    )


def _strip_cron_wrapper(content: str) -> str:
    """Rewrite ``Cronjob Response: <job>`` into ``<job>`` as the headline."""
    match = _CRON_HEADLINE_RE.search(content or "")
    if not match:
        return content
    name = match.group("name").strip()
    body = content[match.end():]
    body = _JOB_ID_LINE_RE.sub("", body)
    body = _RULE_LINE_RE.sub("", body).strip()
    return f"{name}\n{body}" if body else name


def _matches_any(needles: str, haystack: str) -> bool:
    """True when any comma-separated needle appears in haystack (casefolded)."""
    low = (haystack or "").lower()
    return any(part.strip().lower() in low for part in (needles or "").split(",") if part.strip())


def _prepare(content: str, settings: dict) -> tuple[bool, str, str]:
    """Apply the user's filters. Returns ``(should_send, reason, content)``."""
    if not (content or "").strip():
        return False, "empty message", content

    if not settings.get("enabled", True):
        return False, "disabled in plugin settings", content

    name, job_id = _job_identity(content)
    # A non-cron message has no job identity, so job filters fall back to its text.
    target = " ".join(part for part in (name, job_id) if part) or content

    if str(settings.get("only_jobs") or "").strip() and not _matches_any(
        str(settings.get("only_jobs")), target
    ):
        reason = f"not in only_jobs ({name or job_id or 'non-cron message'})"
        return False, reason, content

    if str(settings.get("skip_jobs") or "").strip() and _matches_any(
        str(settings.get("skip_jobs")), target
    ):
        return False, f"matched skip_jobs ({name or job_id})", content

    urgency = _urgency_for(content)
    floor = str(settings.get("min_urgency") or "normal").strip().lower()
    if _URGENCY_RANK.get(urgency, 1) < _URGENCY_RANK.get(floor, 1):
        return False, f"urgency '{urgency}' below min_urgency '{floor}'", content

    if settings.get("strip_cron_wrapper", True):
        content = _strip_cron_wrapper(content)

    return True, "", content


def _split_headline_body(content: str) -> tuple[str, str]:
    """A toast is a headline plus an optional body; the message is multi-line.

    First non-empty line becomes the headline, the rest the body. Both are
    truncated — the full text is already delivered to the real channel, so a
    toast only needs to be glanceable.
    """
    lines = [line.strip() for line in (content or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""

    headline = lines[0]
    body = " ".join(lines[1:])
    if len(headline) > HEADLINE_MAX:
        headline = headline[: HEADLINE_MAX - 1].rstrip() + "…"
    if len(body) > BODY_MAX:
        body = body[: BODY_MAX - 1].rstrip() + "…"
    return headline, body


def _raw_toast(content: str, app_name: str, urgency: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Fire one toast, unconditionally. Returns ``(ok, identifier, error)``."""
    binary = shutil.which(NOTIFY_BIN)
    if not binary:
        return False, None, f"{NOTIFY_BIN} not found on PATH"

    headline, body = _split_headline_body(content)
    if not headline:
        return False, None, "refusing to send an empty toast"

    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = _dbus_address()

    argv = [binary, "--app-name", app_name or DEFAULT_APP_NAME, "-u", urgency]
    argv += [headline, body] if body else [headline]

    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return False, None, f"{NOTIFY_BIN} timed out after {TIMEOUT_S}s"
    except OSError as exc:
        return False, None, f"{NOTIFY_BIN} could not run: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else "no output"
        return False, None, f"{NOTIFY_BIN} exited {proc.returncode}: {first}"

    return True, f"toast-{uuid.uuid4().hex[:12]}", None


def _deliver(
    content: str, getter: Optional[Callable[..., Any]], fallback_app: str = ""
) -> tuple[str, Optional[str], Optional[str]]:
    """Apply filters then toast. Returns ``(status, identifier, detail)``.

    ``status`` is ``"sent"``, ``"skipped"`` or ``"error"``.
    """
    settings = _resolve_settings(getter)
    should_send, reason, prepared = _prepare(content, settings)
    if not should_send:
        logger.debug("omatoast: skipped (%s)", reason)
        return "skipped", f"skipped:{reason}", None

    app_name = str(settings.get("app_name") or "").strip() or fallback_app or DEFAULT_APP_NAME
    urgency = _urgency_for(prepared)
    ok, identifier, error = _raw_toast(prepared, app_name, urgency)
    if ok:
        return "sent", identifier, None
    return "error", None, error


def _app_name_from(extra: Any) -> str:
    try:
        name = extra_or_secret(extra, "app_name", "OMATOAST_APP_NAME")
    except Exception:  # never let a config lookup break delivery
        name = ""
    return (name or "").strip() or DEFAULT_APP_NAME


# --------------------------------------------------------------------------
# adapter
# --------------------------------------------------------------------------

class OmarchyToastAdapter(BasePlatformAdapter):
    """Outbound-only adapter: Hermes -> desktop toast."""

    def __init__(self, config: PlatformConfig, settings_getter: Optional[Callable[..., Any]] = None):
        super().__init__(config, Platform(PLATFORM_NAME))
        self._settings_getter = settings_getter
        self._fallback_app = _app_name_from(getattr(config, "extra", None))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # No listener, no handshake: a toast channel is always "connectable".
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def get_chat_info(self, chat_id):
        return {"name": chat_id or DEFAULT_HOME_LABEL, "type": "dm"}

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        status, identifier, error = await asyncio.to_thread(
            _deliver, content, self._settings_getter, self._fallback_app
        )
        if status == "error":
            return SendResult(success=False, error=error)
        # A filtered-out toast is a deliberate no-op, not a delivery failure:
        # reporting it as an error would spam the cron delivery error log.
        return SendResult(success=True, message_id=identifier)


# --------------------------------------------------------------------------
# registration surface
# --------------------------------------------------------------------------

def check_requirements() -> bool:
    """PASSIVE probe — must never install anything (status displays call it)."""
    return shutil.which(NOTIFY_BIN) is not None


def validate_config(config: Any) -> bool:
    return check_requirements()


def _env_enablement() -> Optional[dict]:
    """Auto-enable on any box that can actually fire a toast."""
    if not check_requirements():
        return None
    return seed_extra_from_env(
        (("OMATOAST_APP_NAME", "app_name", None),),
        home_env="OMATOAST_HOME_CHANNEL",
        home_default=DEFAULT_HOME_LABEL,
    )


def _parse_target_ref(raw: str):
    """``omatoast`` / ``omatoast:label`` -> ``(chat_id, thread_id)``.

    Returning ``None`` lets the channel directory / home channel resolve it
    instead, which is what we want for a bare ``omatoast``.
    """
    label = (raw or "").strip()
    if not label or ":" in label or any(ch.isspace() for ch in label):
        return None
    return (label, None)


def _validate_target_ref(address: str):
    address = (address or "").strip()
    if not address:
        return True
    if ":" in address or any(ch.isspace() for ch in address):
        return "omatoast targets are a single label, e.g. 'omatoast' or 'omatoast:desktop'"
    return True


def _make_standalone_send(getter: Optional[Callable[..., Any]]):
    async def _standalone_send(
        pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False
    ):
        """Cron delivery when no live gateway holds the adapter."""
        fallback = _app_name_from(getattr(pconfig, "extra", None))
        status, identifier, error = await asyncio.to_thread(_deliver, message, getter, fallback)
        if status == "error":
            return {"error": error or "toast failed"}
        return {"success": True, "message_id": identifier}

    return _standalone_send


def register(ctx) -> None:
    def _setting(key: str, default: Any = None) -> Any:
        """Read one declared setting from plugins.entries.<id>.settings.<key>."""
        try:
            value = ctx.get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Omarchy Toast",
        adapter_factory=lambda cfg: OmarchyToastAdapter(cfg, _setting),
        check_fn=check_requirements,
        validate_config=validate_config,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="OMATOAST_HOME_CHANNEL",
        parse_target_ref_fn=_parse_target_ref,
        validate_target_ref_fn=_validate_target_ref,
        standalone_sender_fn=_make_standalone_send(_setting),
        max_message_length=0,  # not a chat channel: one toast per message, never chunk
        platform_hint="",
        emoji="🍞",
    )
