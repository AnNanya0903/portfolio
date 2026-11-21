"""Follow-up email templates.

Enquiry follow-ups (quotation or proforma invoice sent, enquiry Open): Follow-up 1 to 4, each with the
enquiry's online share link and password, then the closing email (enquiries/status_emails.py), which has
no link. Marketing follow-ups (enquiry rejected): Follow-up 1 to 4 and a closing email, each with its own
subject and the STOP line.

Every email is signed "AVIGIRI team". A sentence whose placeholder has no value is left out, never sent
with an empty gap; the greeting falls back to "Sir/Madam" and the enquiry title to "your enquiry".
"""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Sequence, Tuple

from django.conf import settings

DEFAULT_STAGE = "general"
QUOTATION_STAGE = "waiting_po"  # quotation sent, waiting for the order
PAYMENT_STAGE = "payment_pending"  # proforma invoice sent, waiting for the payment
MARKETING_STAGE = "marketing"  # rejected: the Marketing portal tries to win the customer back

STAGE_CHOICES: List[Tuple[str, str]] = [
    (DEFAULT_STAGE, "General follow-up"),
    (QUOTATION_STAGE, "Quotation sent"),
    (PAYMENT_STAGE, "Proforma invoice sent"),
    (MARKETING_STAGE, "Marketing follow-up (after rejection)"),
]
STAGE_KEYS = {key for key, _ in STAGE_CHOICES}

TEAM_NAME = "AVIGIRI team"
# Last line of every Marketing email. A reply that starts with STOP marks the customer Do not contact.
MARKETING_STOP_LINE = "Reply STOP if you'd prefer not to hear from us."

_LINK_BLOCK = ("You can also view the {document} online and accept or reject it using the link below:\n"
               "{share_link}\nPassword: {share_password}")

# A template is a list of paragraphs; a paragraph is a list of sentences. A sentence is dropped when one of
# its placeholders has no value, and a paragraph with no sentences left is dropped.
Template = Sequence[Sequence[str]]

# Enquiry follow-ups 1-4: (paragraphs, sign-off).
_ENQUIRY_FOLLOW_UPS: List[Tuple[Template, str]] = [
    ((
        ("Just following up on the {document}{document_number_paren} we shared for {enquiry_title}.",
         "Have you had a chance to look through it?"),
        (_LINK_BLOCK,),
        ("If anything needs clarification, we'd be glad to walk you through it.",),
    ), "Best regards,"),
    ((
        ("We wanted to check whether the {document} for {enquiry_title} meets your requirements.",
         "If you'd like changes to the quantity, specifications or terms, let us know and we'll send a "
         "revised version."),
        (_LINK_BLOCK,),
    ), "Kind regards,"),
    ((
        ("Following up again on {enquiry_title}.",
         "To meet your expected timeline of {delivery_timeline}, we'd need your confirmation by "
         "{confirm_by_date}."),
        (_LINK_BLOCK,),
        ("If you'd find it easier to discuss on a call, you can reach us at {phone_number}.",),
    ), "Regards,"),
    ((
        ("This is a gentle reminder about our {document}{document_number_paren} for {enquiry_title}.",
         "We'd appreciate a quick update on where things stand, even if it's just to say the plan has changed."),
        (_LINK_BLOCK,),
    ), "Best regards,"),
]

# The enquiry's closing email (sets the enquiry to Closed). No link.
_ENQUIRY_CLOSING: Tuple[Template, str] = ((
    ("As we haven't heard back, we'll close this enquiry for now.",
     "Thank you for considering AVIGIRI."),
    ("If you'd like to go ahead later or need an updated {document}, just reply to this email and we'll pick "
     "it up right away.",),
), "Warm regards,")

# Marketing follow-ups 1-4 and the closing email: (subject, paragraphs, sign-off).
_MARKETING: List[Tuple[str, Template, str]] = [
    ("Thank you for considering AVIGIRI{customer_name_comma}", (
        ("Thank you for taking the time to review our quotation for {enquiry_title}.",
         "We understand it wasn't the right fit this time, and we appreciate you letting us know."),
        ("If budget, timeline or scope was the concern, we'd be glad to suggest an alternative option that "
         "works better for you.",
         "Just reply to this email and tell us what would help."),
    ), "Warm regards,"),
    ("A different option for {enquiry_title}?", (
        ("Following up on our last note.",
         "Many customers find a phased approach or a revised specification helps them go ahead within their "
         "budget."),
        ("If you share what didn't work in our earlier proposal, we can send a revised quote within "
         "{x} working days, with no obligation.",),
    ), "Best regards,"),
    ("Could you share what didn't work for you?", (
        ("We're always looking to improve, and your feedback on our quotation for {enquiry_title} would really "
         "help us.",),
        ("Was it the price, the timeline, the specifications, or something else?",
         "Even a one-line reply would mean a lot.",
         "If it's something we can fix, we'll gladly send you an updated proposal."),
    ), "Kind regards,"),
    ("Still here when you need us", (
        ("Just checking in once more.",
         "If there's an upcoming requirement we could help with, or you'd like us to revisit the earlier "
         "proposal, a quick reply is all it takes."),
        ("You can also reach us directly at {phone_number}.",),
    ), "Regards,"),
    ("Closing the loop{customer_name_comma}", (
        ("We haven't heard back, so we won't send further follow-ups on this.",
         "Thank you again for considering AVIGIRI."),
        ("Whenever you have a new requirement, just reply to this email and our team will get back to you "
         "promptly.",),
    ), "Wishing you all the best,"),
]

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
# Placeholders that may be empty without dropping their sentence (they carry their own punctuation).
_OPTIONAL = {"document_number_paren", "customer_name_comma"}


def sender_signature(user=None) -> str:
    """Closing lines for the other automatic emails: "Best regards, AVIGIRI Team" and an optional phone
    (FOLLOW_UP_CONTACT_PHONE). No personal name: every email is signed by the team."""
    phone = (getattr(settings, "FOLLOW_UP_CONTACT_PHONE", "") or "").strip()
    lines = ["Best regards,", "AVIGIRI Team"]
    if phone:
        lines.append(f"Phone: {phone}")
    return "\n".join(lines)


def _fill(text: str, values: Dict[str, str]) -> Optional[str]:
    """`text` with its placeholders filled, or None when one of them has no value."""
    for name in _PLACEHOLDER.findall(text):
        if name not in _OPTIONAL and not str(values.get(name) or "").strip():
            return None
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1)) or "").strip()
                            if m.group(1) not in _OPTIONAL else str(values.get(m.group(1)) or ""), text)


def _render(paragraphs: Template, values: Dict[str, str], sign_off: str, extra: Sequence[str] = ()) -> str:
    greeting = " ".join(str(values.get("customer_name") or "").split()) or "Sir/Madam"
    parts = [f"Dear {greeting},"]
    for paragraph in paragraphs:
        sentences = [s for s in (_fill(sentence, values) for sentence in paragraph) if s]
        if sentences:
            parts.append(" ".join(sentences))
    parts.append(f"{sign_off}\n{TEAM_NAME}")
    parts.extend(p for p in extra if p)  # footer, after the sign-off
    return "\n\n".join(parts)


def _values(*, customer_name: str = "", subject: str = "", enquiry_title: str = "", document: str = "",
            document_number: str = "", share_url: str = "", share_password: str = "",
            delivery_timeline: str = "", confirm_by_date: str = "") -> Dict[str, str]:
    name = " ".join(str(customer_name or "").split())
    number = " ".join(str(document_number or "").split())
    return {
        "customer_name": name,
        "customer_name_comma": f", {name}" if name else "",
        "enquiry_title": " ".join(str(enquiry_title or subject or "").split()) or "your enquiry",
        "document": document or "quotation",
        "document_number_paren": f" ({number})" if number else "",
        "share_link": (share_url or "").strip(),
        "share_password": (share_password or "").strip(),
        "delivery_timeline": " ".join(str(delivery_timeline or "").split()),
        "confirm_by_date": " ".join(str(confirm_by_date or "").split()),
        "phone_number": (getattr(settings, "FOLLOW_UP_CONTACT_PHONE", "") or "").strip(),
        "x": str(getattr(settings, "MARKETING_REVISED_QUOTE_DAYS", "") or "").strip(),
    }


def document_for_stage(stage: str) -> str:
    """'proforma invoice' for proforma invoice follow-ups, otherwise 'quotation'."""
    return "proforma invoice" if stage == PAYMENT_STAGE else "quotation"


def build_follow_up_body(stage: str, *, reference: str = "", subject: str = "", user=None, attempt: int = 1,
                         due_on=None, credit_days=None, today=None, share_url: str = "",
                         share_password: str = "", customer_name: str = "", document_number: str = "",
                         enquiry_title: str = "", delivery_timeline: str = "", confirm_by_date: str = "") -> str:
    """Body (plain text) for follow-up number `attempt` of a stage.

    Enquiry stages: Follow-up 1-4 (4 or more = Follow-up 4). Without a share link and password the link
    paragraph is left out; the automatic sender never sends such an email (enquiries/auto_follow_up.py).
    Marketing: Follow-up 1-4, then the closing email (attempt MARKETING_FOLLOW_UP_COUNT + 1 or more).
    `reference`, `user`, `due_on`, `credit_days` and `today` are kept for existing callers.
    """
    key = stage if stage in STAGE_KEYS else DEFAULT_STAGE
    values = _values(customer_name=customer_name, subject=subject, enquiry_title=enquiry_title,
                     document=document_for_stage(key), document_number=document_number, share_url=share_url,
                     share_password=share_password, delivery_timeline=delivery_timeline,
                     confirm_by_date=confirm_by_date)
    if key == MARKETING_STAGE:
        _, paragraphs, sign_off = _marketing_template(attempt)
        return _render(paragraphs, values, sign_off, extra=[MARKETING_STOP_LINE])
    paragraphs, sign_off = _ENQUIRY_FOLLOW_UPS[min(max(attempt, 1), len(_ENQUIRY_FOLLOW_UPS)) - 1]
    return _render(paragraphs, values, sign_off)


def _marketing_template(attempt: int) -> Tuple[str, Template, str]:
    """Follow-up 1-4 for attempts up to MARKETING_FOLLOW_UP_COUNT, the closing email after that."""
    count = follow_up_count(MARKETING_STAGE)
    if attempt > count:
        return _MARKETING[-1]
    return _MARKETING[min(max(attempt, 1), len(_MARKETING) - 1) - 1]


def marketing_subject(attempt: int, *, customer_name: str = "", subject: str = "", enquiry_title: str = "") -> str:
    """Subject of Marketing email number `attempt` (each one opens its own conversation)."""
    values = _values(customer_name=customer_name, subject=subject, enquiry_title=enquiry_title)
    return _fill(_marketing_template(attempt)[0], values) or "AVIGIRI"


def enquiry_closing_body(*, customer_name: str = "", document: str = "quotation") -> str:
    """The enquiry's closing email (no link)."""
    paragraphs, sign_off = _ENQUIRY_CLOSING
    return _render(paragraphs, _values(customer_name=customer_name, document=document), sign_off)


_URL = re.compile(r"https?://[^\s<>\"']+")


def to_html(text: str) -> str:
    """The HTML version of a plain-text email: paragraphs kept, links clickable."""
    def paragraph(block: str) -> str:
        pieces, last = [], 0
        for match in _URL.finditer(block):
            pieces.append(html.escape(block[last:match.start()]))
            url = html.escape(match.group(0), quote=True)
            pieces.append(f'<a href="{url}">{url}</a>')
            last = match.end()
        pieces.append(html.escape(block[last:]))
        return "<p>" + "".join(pieces).replace("\n", "<br>\n") + "</p>"

    return "\n".join(paragraph(block) for block in (text or "").split("\n\n") if block.strip())


def all_stage_bodies(*, reference: str, subject: str, user=None, due_on=None, credit_days=None,
                     today=None, **extra) -> Dict[str, str]:
    """Body text for every stage, used by the stage picker on the compose page."""
    return {
        key: build_follow_up_body(key, reference=reference, subject=subject, user=user,
                                  due_on=due_on, credit_days=credit_days, today=today, **extra)
        for key, _ in STAGE_CHOICES
    }


def follow_up_count(stage: str) -> int:
    """How many follow-ups a stage gets before its closing email (FOLLOW_UP_MAX_COUNT; Marketing has its own)."""
    if stage == MARKETING_STAGE:
        return int(getattr(settings, "MARKETING_FOLLOW_UP_COUNT", 4) or 0)
    return int(getattr(settings, "FOLLOW_UP_MAX_COUNT", 4) or 0)
