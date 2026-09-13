"""Contact access follows delivered invitations, never a model-authored score."""
import os
from pathlib import Path


def preview_configured(root, environment=None):
    """Do not offer an unavailable contact feature in a default installation."""
    environment = os.environ if environment is None else environment
    configured = environment.get('OLIVIA_PERSONAL_CHAT_CONFIG')
    path = Path(configured) if configured else Path(root) / 'personal-chat/config.json' if root else None
    return path is not None and path.is_absolute() and path.is_file()


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
        selected = None
        for row in completed:
            if row.get("contact_invitation_id") == invitation["letter_id"]:
                candidate = validate_choice(row.get("contact_choice"), row.get("content", ""))
                if candidate:
                    selected = candidate["choice"]
        return {"state": selected or "invited", "invitation_id": invitation["letter_id"],
                "channels": ["qq", "wechat"] if selected == "both" else [selected] if selected in {"qq", "wechat"} else []}
    observed = [r for r in completed if "contact_qualification" in r and r.get("origin") != "proactive"]
    eligible = high_count(snapshot) >= 3 and len(observed) >= 2 and all(r["contact_qualification"] is True for r in observed[-2:])
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
            "not_before": source.get("created_at", now) + 1800,
            "expires_at": source.get("created_at", now) + 7 * 86400}
