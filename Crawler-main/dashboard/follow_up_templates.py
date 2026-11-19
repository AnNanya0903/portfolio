"""Follow-up email templates for enquiries, one per sales stage.

Every template greets "Dear Sir/Madam,", names the enquiry reference, asks one clear
question, closes with a line that fits the stage and is signed "Best regards, AVIGIRI Team"
(plus an optional phone number). No personal names are used.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from django.conf import settings

DEFAULT_STAGE = "general"
QUOTATION_STAGE = "waiting_po"  # quotation sent, waiting for the order
PAYMENT_STAGE = "payment_pending"  # proforma invoice sent, waiting for the payment
MARKETING_STAGE = "marketing"  # rejected: the Marketing portal tries to win the customer back

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
        QUOTATION_STAGE,
        "Quotation sent",
        "We are following up on our quotation for {subject} (our reference: {reference}), which we shared "
        "with you recently, to make sure you have received it.",
        "Could you please let us know if you need any clarification on the specification, price or delivery?",
    ),
    (
        PAYMENT_STAGE,
        "Proforma invoice sent",
        "We are following up on the proforma invoice we shared for {subject} (our reference: {reference}). "
        "Kindly confirm the order and arrange the payment as per the agreed terms, so that we can schedule "
        "production.",
        "Could you please let us know when we can expect the payment?",
    ),
    (
        MARKETING_STAGE,
        "Marketing follow-up (after rejection)",
        "Thank you again for considering AVIGIRI for {subject} (our reference: {reference}). If price, "
        "delivery time or specification was the concern, we are open to revising our offer.",
        "Could you please let us know what would work for you?",
    ),
]

# Later follow-ups (#2, #3, #4 final) per stage: (main paragraph, question).
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
    QUOTATION_STAGE: [
        ("Could you please share the status of your evaluation of our quotation for {subject} "
         "(our reference: {reference}), or the expected date for your decision?",
         "If it would help, we are happy to arrange a call with our technical team."),
        ("We have not yet heard back on our quotation for {subject} (our reference: {reference}). If anything "
         "in our offer does not suit your requirement, such as quantity, material, delivery schedule or "
         "payment terms, please let us know.",
         "We will be glad to revise the quotation."),
        ("This is our final reminder for the quotation for {subject} (our reference: {reference}).",
         "To confirm the order, please send your purchase order. If the requirement has changed or is on "
         "hold, a short reply would help us plan."),
    ],
    PAYMENT_STAGE: [
        ("We are following up on the proforma invoice for {subject} (our reference: {reference}).",
         "Could you please let us know when we can expect the payment, and share the transaction reference "
         "once it is made?"),
        ("The payment against the proforma invoice for {subject} (our reference: {reference}) is still "
         "pending. If you need any change to the invoice details or the payment terms, please let us know.",
         "We will be happy to help."),
        ("This is our final reminder for the proforma invoice for {subject} (our reference: {reference}). "
         "We have reserved a production slot for your order.",
         "If we do not receive the payment or an update soon, we may need to release this slot to other "
         "orders."),
    ],
    MARKETING_STAGE: [
        ("We would still like the opportunity to supply {subject} (our reference: {reference}).",
         "If you share your target price or required delivery date, our team will check what we can do and "
         "come back within one working day."),
        ("If this requirement is already closed, we would be glad to help with your next one. We manufacture "
         "gaskets, seals, O-rings and extruded rubber profiles to drawing, and can share samples on request.",
         "Please reply with your drawing or specification whenever you have a new requirement."),
        ("This is our last follow-up on {subject} (our reference: {reference}).",
         "Please feel free to contact us at any time with a drawing or specification for future requirements."),
    ],
}
# Closing line per stage, matched to the purpose of the email: (follow-ups #1-#3, final follow-up #4).
_CLOSINGS: Dict[str, Tuple[str, str]] = {
    "general": (
        "Please feel free to reply to this email if you have any questions about your enquiry.",
        "Whenever you are ready, simply reply to this email and we will be glad to assist.",
    ),
    QUOTATION_STAGE: (
        "Once we receive your order, we will schedule production right away.",
        "Whenever you are ready to place the order, simply reply to this email and we will be glad to assist.",
    ),
    PAYMENT_STAGE: (
        "If the payment has already been made, please share the payment reference so that we can update our records.",
        "If you have already made the payment, please share the payment reference and we will update our records immediately.",
    ),
    MARKETING_STAGE: (
        "Simply reply to this email and we will be glad to help.",
        "Thank you for considering AVIGIRI.",
    ),
}

# Marketing's last email, after its follow-ups: the enquiry is closed, the door stays open.
_MARKETING_CLOSING = (
    "As we have not heard back from you regarding {subject} (our reference: {reference}), we are closing "
    "this enquiry for now.",
    "For any new requirement, please contact the AVIGIRI team. You can simply reply to this email, and we "
    "will be glad to assist.",
    "Thank you for considering AVIGIRI.",
)

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
    """One sentence about the agreed due date for payment reminders, or ''.

    A credit period is not mentioned in the email (the sentence about the credit period was removed).
    """
    if not due_on or credit_days:
        return ""
    when = f"{due_on.day} {due_on:%B %Y}"
    verb = "is" if today is None or due_on >= today else "was"
    return f"As agreed, the payment {verb} due on {when}."


def build_follow_up_body(stage: str, *, reference: str, subject: str, user=None, attempt: int = 1,
                         due_on=None, credit_days=None, today=None) -> str:
    """Body for follow-up number `attempt` (1 = first; 4 or more = the final email).

    For payment reminders, `due_on` adds a sentence about the agreed due date (not for a credit period).
    """
    key = stage if stage in STAGE_KEYS else DEFAULT_STAGE
    closing, final_closing = _CLOSINGS[key]
    if key == MARKETING_STAGE and attempt > len(_LATER[key]) + 1:
        # After the last Marketing follow-up: the closing email.
        paragraph, question, closing = _MARKETING_CLOSING
    elif attempt <= 1:
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


def follow_up_count(stage: str) -> int:
    """How many follow-ups a stage gets before its closing email (FOLLOW_UP_MAX_COUNT; Marketing has its own)."""
    if stage == MARKETING_STAGE:
        return int(getattr(settings, "MARKETING_FOLLOW_UP_COUNT", 4) or 0)
    return int(getattr(settings, "FOLLOW_UP_MAX_COUNT", 4) or 0)
