"""Follow-up email templates for enquiries, one per sales stage.

Every template greets "Dear Sir/Madam,", names the enquiry reference, asks one clear
question, closes with a line that fits the stage and is signed "Best regards, AVIGIRI Team"
(plus an optional phone number). No personal names are used.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from django.conf import settings

DEFAULT_STAGE = "general"
PAYMENT_STAGE = "payment_pending"  # for accepted orders whose payment is not received yet

# (key, label shown to the employee, main paragraph, the one question to ask)
_STAGES: List[Tuple[str, str, str, str]] = [
    (
        "general",
        "General follow-up",
        "Thank you for contacting AVIGIRI. We are following up regarding your recent enquiry "
        "about {subject} (our reference: {reference}).",
        "Could you please let us know if there are any updates, or if you need any further "
        "information from our side?",
    ),
    (
        "waiting_po",
        "Waiting for order confirmation",
        "Thank you for your interest in our quotation for {subject} (our reference: {reference}).",
        "Could you please confirm the order (purchase order), so that we can plan production accordingly?",
    ),
    (
        "payment_pending",
        "Payment pending",
        "Thank you for confirming your order for {subject} (our reference: {reference}). "
        "As per the proforma invoice, the payment for this order is still pending.",
        "Could you please let us know when we can expect the payment, so that we can schedule "
        "production and dispatch?",
    ),
]

# Later follow-ups (#2 reminder, #3 offer to adjust, #4 final) per stage: (main paragraph, question).
# Follow-up #1 always uses the _STAGES text above.
_LATER: Dict[str, List[Tuple[str, str]]] = {
    "general": [
        ("We wanted to gently remind you about your enquiry about {subject} (our reference: {reference}).",
         "Could you please let us know if you have had a chance to look into this?"),
        ("We are following up on your enquiry about {subject} (our reference: {reference}). "
         "If any further detail would help your decision, we would be happy to share it.",
         "Is there any information we can send to help you move forward?"),
        ("This is our final follow-up regarding your enquiry about {subject} (our reference: {reference}). "
         "We understand plans may have changed, and we do not wish to fill your inbox.",
         "Could you please let us know whether we should keep this enquiry open, or close it for now?"),
    ],
    "waiting_po": [
        ("We wanted to gently remind you about the order confirmation for {subject} (our reference: {reference}), "
         "so that we can reserve production capacity.",
         "Could you please let us know when you expect to confirm the order?"),
        ("We are following up on our quotation for {subject} (our reference: {reference}). If there is any "
         "change in quantity or schedule, we would be happy to update the quotation.",
         "Are there any changes we should make before you place the order?"),
        ("This is our final follow-up regarding the quotation for {subject} (our reference: {reference}). "
         "We understand plans may have changed, and we do not wish to fill your inbox.",
         "Could you please let us know whether we should hold the quotation open, or close it for now?"),
    ],
    "payment_pending": [
        ("We wanted to gently remind you about the pending payment for your order {subject} "
         "(our reference: {reference}).",
         "Could you please let us know when the payment will be made?"),
        ("We are following up on the pending payment for your order {subject} (our reference: {reference}). "
         "If there is any issue with the proforma invoice or the payment details, we would be happy to help.",
         "Could you please share the payment status, or let us know if anything is holding it up?"),
        ("This is our final reminder regarding the pending payment for your order {subject} "
         "(our reference: {reference}). We are unable to schedule production until the payment is received.",
         "Could you please confirm the payment date at the earliest?"),
    ],
}
# Closing line per stage, matched to the purpose of the email: (follow-ups #1-#3, final follow-up #4).
_CLOSINGS: Dict[str, Tuple[str, str]] = {
    "general": (
        "Please feel free to reply to this email if you have any questions about your enquiry.",
        "Whenever you are ready, simply reply to this email and we will be glad to assist.",
    ),
    "waiting_po": (
        "Once we receive your order, we will schedule production right away.",
        "Whenever you are ready to place the order, simply reply to this email and we will be glad to assist.",
    ),
    "payment_pending": (
        "If the payment has already been made, please share the payment reference so that we can update our records.",
        "If you have already made the payment, please share the payment reference and we will update our records immediately.",
    ),
}

STAGE_CHOICES: List[Tuple[str, str]] = [(key, label) for key, label, _, _ in _STAGES]
STAGE_KEYS = {key for key, _ in STAGE_CHOICES}


def sender_signature(user=None) -> str:
    """Closing lines: "Best regards, AVIGIRI Team" and an optional phone (FOLLOW_UP_CONTACT_PHONE setting).

    No personal name: every email is signed by the team. `user` is kept for existing callers.
    """
    phone = (getattr(settings, "FOLLOW_UP_CONTACT_PHONE", "") or "").strip()
    lines = ["Best regards,", "AVIGIRI Team"]
    if phone:
        lines.append(f"Phone: {phone}")
    return "\n".join(lines)


def _payment_terms_line(due_on, credit_days, today) -> str:
    """One sentence about the agreed credit period / due date for payment reminders, or ''."""
    if not due_on:
        return ""
    when = f"{due_on.day} {due_on:%B %Y}"
    verb = "is" if today is None or due_on >= today else "was"
    if credit_days:
        return f"As per the agreed credit period of {credit_days} days, the payment {verb} due on {when}."
    return f"As agreed, the payment {verb} due on {when}."


def build_follow_up_body(stage: str, *, reference: str, subject: str, user=None, attempt: int = 1,
                         due_on=None, credit_days=None, today=None) -> str:
    """Body for follow-up number `attempt` (1 = first; 4 or more = the final email).

    For payment reminders, `due_on` / `credit_days` add a sentence about the agreed credit period.
    """
    key = stage if stage in STAGE_KEYS else DEFAULT_STAGE
    closing, final_closing = _CLOSINGS[key]
    if attempt <= 1:
        _, _, paragraph, question = next(item for item in _STAGES if item[0] == key)
    else:
        later = _LATER[key]
        paragraph, question = later[min(attempt, len(later) + 1) - 2]
        if attempt >= len(later) + 1:
            closing = final_closing
    values = {
        "subject": (subject or "").strip() or "your enquiry",
        "reference": (reference or "").strip() or "-",
    }
    paragraph = paragraph.format(**values)
    if key == PAYMENT_STAGE:
        terms = _payment_terms_line(due_on, credit_days, today)
        if terms:
            paragraph = f"{paragraph} {terms}"
    return "\n\n".join([
        "Dear Sir/Madam,",
        paragraph,
        question,
        closing,
        sender_signature(user),
    ])


def all_stage_bodies(*, reference: str, subject: str, user=None, due_on=None, credit_days=None,
                     today=None) -> Dict[str, str]:
    """Body text for every stage, used by the stage picker on the compose page."""
    return {
        key: build_follow_up_body(key, reference=reference, subject=subject, user=user,
                                  due_on=due_on, credit_days=credit_days, today=today)
        for key, _ in STAGE_CHOICES
    }
