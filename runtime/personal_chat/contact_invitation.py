"""Contact access follows delivered invitations, never a model-authored score."""
import json
import os
from pathlib import Path


def preview_configured(root, environment=None):
    """Allow the invitation before transport setup after explicit proactive opt-in.

    An explicit transport config still wins when supplied. Otherwise a durable
    local installation may offer the invitation once proactive letters are
    enabled; choosing QQ/WeChat can then enter the existing SETUP_REQUIRED flow.
    """
    environment = os.environ if environment is None else environment
    configured = environment.get('OLIVIA_PERSONAL_CHAT_CONFIG')
    if configured:
        path = Path(configured)
        return path.is_absolute() and path.is_file()
    if root is None:
        return False
    root = Path(root)
    if not root.is_absolute():
        return False
    if (root / 'personal-chat/config.json').is_file():
        return True
    try:
        prefs = json.loads((root / 'proactive/settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(prefs, dict) and prefs.get('enabled') is True


def high_count(snapshot):
    return sum(getattr(snapshot, name, 0) >= 70
               for name in ("familiarity", "trust", "comfort", "closeness"))


def observe(row, snapshot, events):
    if row.get("origin") == "proactive" or "contact_qualification" in row:
        return
    delivery = row.get("private_world_delivery_id")
    event = next((event for event in events if event.payload.get("canonical_delivery_id") == delivery
           and event.payload.get("applied") is True
           and type(event.payload.get("contact_qualification")) is bool
           and event.event_type in {"meaningful_exchange", "shared_experience", "support_received",
                                   "boundary_respected", "repair", "conflict"}), None)
    if event:
        row["contact_qualification"] = event.payload["contact_qualification"]
        # Persist only a behavioral projection, never the hidden raw scores. Both
        # proactive letters and IM can then share the same relationship-driven
        # initiative policy without another relationship store.
        from runtime.personal_chat.initiative_profile import profile_from_snapshot
        profile = profile_from_snapshot(snapshot)
        row["initiative_tier"] = profile.tier
        row["initiative_caution"] = profile.caution


def validate_choice(value, text):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"choice", "quote"}
            or value["choice"] not in {"qq", "wechat", "both", "declined", "later"}
            or not isinstance(value["quote"], str) or not value["quote"].strip()
            or len(value["quote"]) > 240 or value["quote"] not in text):
        raise ValueError("DAILY_LIFE_CONTACT_CHOICE_INVALID")
    return dict(value)


def status(rows, snapshot):
    completed = sorted((r for r in rows if r.get("letter_status") == "COMPLETED"),
                       key=lambda r: r.get("published_at", r.get("created_at", 0)))
    invitation = next((r for r in reversed(completed)
                       if r.get("origin") == "proactive" and r.get("proactive_kind") == "contact_invitation"), None)
    if invitation:
        selected = invitation.get("contact_setup_choice")
        if selected not in {"qq", "wechat", "both"}:
            selected = None
        for row in completed:
            if row.get("contact_invitation_id") == invitation["letter_id"]:
                candidate = validate_choice(row.get("contact_choice"), row.get("content", ""))
                if candidate:
                    selected = candidate["choice"]
        return {"state": selected or "invited", "invitation_id": invitation["letter_id"],
                "channels": ["qq", "wechat"] if selected == "both" else [selected] if selected in {"qq", "wechat"} else []}
    inflight = next((r for r in rows
                     if r.get("origin") == "proactive"
                     and r.get("proactive_kind") == "contact_invitation"
                     and r.get("letter_status") in {"PENDING", "PROCESSING"}), None)
    if inflight:
        return {"state": "locked", "channels": []}
    # Three high relationship dimensions are the product gate. Do not require a
    # post-upgrade hidden interaction marker: older users may already have a
    # valid relationship state before contact_qualification existed.
    eligible = high_count(snapshot) >= 3
    return {"state": "eligible" if eligible else "locked", "channels": []}


def candidate(rows, snapshot, now):
    if status(rows, snapshot)["state"] != "eligible":
        return None
    sources = [r for r in rows if r.get("origin") != "proactive" and r.get("content")
               and r.get("letter_status") == "COMPLETED"]
    source = max(sources, key=lambda r: r.get("created_at", 0), default=None)
    if source is None:
        return None
    return {"id": "contact-invitation-v1", "kind": "contact_invitation",
            "source_id": f"reply:{source['letter_id']}:{source.get('reply_revision', 1)}",
            # Current qualification does not expire with the source letter.
            "not_before": now,
            "expires_at": now + 7 * 86400}
