"""Alerting: macOS notifications for new listings, plus a morning digest.

Detection is worthless if nobody hears about it. A listing is surfaced within
30 minutes of publication, but "apply as early as possible" only happens if
that fact reaches a human — so a genuinely new, good-fit listing triggers a
notification, and everything from the last 24h lands in one digest.

Everything here is best-effort. A failed notification must never fail a run,
and never block one either: `osascript` is invoked with a timeout because a
hung subprocess would stall the whole drafting loop.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Only interrupt for listings that clear this bucket confidence. A notification
# for every listing is a notification for none.
NOTIFY_CONFIDENCE_THRESHOLD = 0.70

# Hard cap so a first sync (hundreds of listings) cannot fire hundreds of
# banners. Beyond this the digest is the right surface.
MAX_NOTIFICATIONS_PER_RUN = 5

_OSASCRIPT_TIMEOUT_SECONDS = 10


def _escape(text: str) -> str:
    """Escape for embedding in an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


async def send_notification(title: str, message: str, subtitle: str = "") -> bool:
    """Post a macOS notification. Returns False rather than raising."""
    osascript = shutil.which("osascript")
    if not osascript:
        return False

    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    if subtitle:
        script += f' subtitle "{_escape(subtitle)}"'

    try:
        process = await asyncio.create_subprocess_exec(
            osascript, "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_OSASCRIPT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            # A hung osascript would otherwise stall the drafting loop.
            process.kill()
            logger.warning("Notification timed out")
            return False
    except Exception as exc:  # noqa: BLE001 - alerting is never critical
        logger.warning("Could not send notification: %s", exc)
        return False

    if process.returncode != 0:
        logger.warning("Notification failed: %s", (stderr or b"").decode()[:200])
        return False
    return True


def select_notifiable(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which new listings are worth interrupting for.

    Deliberately conservative: high confidence, in scope, not dismissed, and
    capped. Work-authorization-sensitive listings are *included* but the caller
    is expected to keep their caveat visible — they are opportunities, just
    unverified ones.
    """
    candidates = [
        row
        for row in listings
        if row.get("bucket")
        and not row.get("dismissed")
        and row.get("active", True)
        and float(row.get("bucket_confidence") or 0.0) >= NOTIFY_CONFIDENCE_THRESHOLD
    ]
    candidates.sort(
        key=lambda r: (
            -float(r.get("bucket_confidence") or 0.0),
            -float(r.get("ats_score") or 0.0),
        )
    )
    return candidates[:MAX_NOTIFICATIONS_PER_RUN]


async def notify_new_listings(listings: list[dict[str, Any]]) -> int:
    """Notify about newly-seen listings. Returns how many banners fired."""
    selected = select_notifiable(listings)
    if not selected:
        return 0

    sent = 0
    for row in selected:
        caveat = " · verify work auth" if row.get("needs_verification") else ""
        ok = await send_notification(
            title=f"New: {row.get('company', 'Unknown')}",
            subtitle=str(row.get("title", ""))[:80],
            message=f"{row.get('bucket', '')}{caveat} — apply early",
        )
        sent += int(ok)

    overflow = len(
        [r for r in listings if float(r.get("bucket_confidence") or 0.0) >= NOTIFY_CONFIDENCE_THRESHOLD]
    ) - len(selected)
    if overflow > 0:
        await send_notification(
            title="Scout",
            message=f"+{overflow} more new listings — see the digest",
        )
    return sent


# --- morning digest --------------------------------------------------------


def _fmt_deadline(row: dict[str, Any]) -> str:
    """Deadlines are almost never published; say so rather than inventing one."""
    if row.get("deadline"):
        return f"{row['deadline']} ({row.get('deadline_source', 'unknown')})"
    return "—"


def render_digest(listings: list[dict[str, Any]], *, generated_at: str) -> str:
    """Render the last 24h as markdown, newest and most confident first."""
    lines = [
        "# Scout digest",
        "",
        f"Generated {generated_at}",
        "",
    ]
    if not listings:
        lines += ["No new listings in the last 24 hours."]
        return "\n".join(lines) + "\n"

    by_bucket: dict[str, list[dict[str, Any]]] = {}
    for row in listings:
        by_bucket.setdefault(row.get("bucket") or "unclassified", []).append(row)

    lines += [f"**{len(listings)} new listing(s)** across {len(by_bucket)} bucket(s).", ""]

    for bucket in sorted(by_bucket):
        rows = sorted(
            by_bucket[bucket],
            key=lambda r: (
                -float(r.get("bucket_confidence") or 0.0),
                -float(r.get("ats_score") or 0.0),
            ),
        )
        lines += [f"## {bucket} ({len(rows)})", ""]
        if any(r.get("needs_verification") for r in rows):
            lines += [
                "> Work-authorization requirements here are **inferred, not verified** —",
                "> no data source publishes them. Confirm with the employer before applying.",
                "",
            ]
        lines += ["| Role | Deadline | Draft | Apply |", "|---|---|---|---|"]
        for r in rows:
            draft = (
                f"[view](/resumes/{r['draft_resume_id']})"
                if r.get("draft_resume_id")
                else f"_{r.get('draft_status', 'none')}_"
            )
            company = str(r.get("company", "")).replace("|", "\\|")
            title = str(r.get("title", "")).replace("|", "\\|")
            lines.append(
                f"| {company} — {title} | {_fmt_deadline(r)} | {draft} "
                f"| [apply]({r.get('apply_url', '')}) |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


async def write_digest(
    listings: list[dict[str, Any]], *, data_dir: Path, notify: bool = True
) -> Path | None:
    """Write today's digest and optionally announce it once. Never raises."""
    try:
        now = datetime.now(timezone.utc)
        target = data_dir / "scout" / "digest"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{now.date().isoformat()}.md"
        path.write_text(
            render_digest(listings, generated_at=now.isoformat()), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 - reporting is never critical
        logger.warning("Could not write digest: %s", exc)
        return None

    if notify:
        await send_notification(
            title="Scout digest",
            message=f"{len(listings)} new listing(s) in the last 24h",
        )
    return path


def listings_since(
    listings: list[dict[str, Any]], *, hours: int = 24
) -> list[dict[str, Any]]:
    """Listings first seen within the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for row in listings:
        raw = row.get("first_seen_at")
        if not raw:
            continue
        try:
            seen = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= cutoff:
            out.append(row)
    return out
