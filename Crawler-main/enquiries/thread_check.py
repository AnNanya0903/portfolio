"""Which Gmail thread an automatic email may go into, and what counts as a customer reply.

A thread is the right one only if ALL of these hold (checked before every send, automatic or manual):
  1. the customer's email address (case-insensitive) is a participant: an email was sent To / Cc them, or
     they wrote in it;
  2. the thread's subject equals the quotation / PI email's subject, ignoring leading "Re:", "Fwd:", "FW:"
     and extra spaces;
  3. it holds the quotation / PI email sent from our mailbox: the email recorded when it was sent from the
     portal, or (sent straight from Gmail) one of ours whose subject, body or attachment name has the
     enquiry number, e.g. ENQ-3346.
When several threads pass, the one with the most recent quotation / PI email wins. When none passes,
nothing is sent and no new thread is started.

A customer reply is a message FROM the verified customer address, or from another address at the customer's
own domain that is already on the enquiry. Messages from our own addresses or any @avigiri.com address
(staff on CC answering) never count, nor do auto-replies (out of office, Auto-Submitted) or delivery
failures; a hard bounce stops the sequence instead.
"""
from __future__ import annotations

import re
from email.utils import getaddresses, parseaddr
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

OUR_DOMAIN = "avigiri.com"

_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)+", re.I)
_BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
_BOUNCE_SUBJECTS = re.compile(
    r"(delivery status notification \(failure\)|undeliverable|undelivered mail|mail delivery (failed|failure)|"
    r"returned mail|delivery has failed|failure notice)", re.I)
_AUTO_SUBJECTS = re.compile(r"^\s*(automatic reply|auto(?:matic)?[- ]?reply|out of (the )?office|autoreply|"
                            r"auto:|away from (the )?office)", re.I)


def normalize_subject(subject: Any) -> str:
    """The subject without leading Re: / Fwd: / FW: (any number, any case) and with single spaces."""
    text = " ".join(str(subject or "").split())
    while True:
        stripped = _PREFIX.sub("", text, count=1)
        if stripped == text:
            return text
        text = " ".join(stripped.split())


def _addresses(*values: Any) -> Set[str]:
    return {address.strip().lower() for _, address in getaddresses([str(v or "") for v in values])
            if address and "@" in address}


def sender_of(message: Mapping[str, Any]) -> str:
    return parseaddr(str(message.get("sender") or ""))[1].strip().lower()


def participants(message: Mapping[str, Any]) -> Set[str]:
    return _addresses(message.get("sender"), message.get("reply_to"), message.get("to"), message.get("cc"))


def is_ours(address: str, own: Iterable[str] = ()) -> bool:
    address = (address or "").strip().lower()
    return bool(address) and (address in {a.lower() for a in own} or address.endswith("@" + OUR_DOMAIN))


def _header(message: Mapping[str, Any], name: str) -> str:
    for item in message.get("headers") or []:
        if str((item or {}).get("name") or "").lower() == name.lower():
            return str(item.get("value") or "")
    return ""


def is_bounce(message: Mapping[str, Any]) -> bool:
    sender = sender_of(message)
    return sender.startswith(_BOUNCE_SENDERS) or bool(_BOUNCE_SUBJECTS.search(str(message.get("subject") or "")))


def is_auto_reply(message: Mapping[str, Any]) -> bool:
    auto = _header(message, "Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return True
    if _header(message, "X-Autoreply") or _header(message, "X-Autorespond"):
        return True
    if _header(message, "Precedence").strip().lower() in ("auto_reply", "bulk", "junk"):
        return True
    return bool(_AUTO_SUBJECTS.match(str(message.get("subject") or "")))


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in (address or "") else ""


def customer_addresses(customer_email: str, others: Iterable[str] = ()) -> Set[str]:
    """The verified address plus the other addresses on the enquiry at the customer's own domain."""
    main = (customer_email or "").strip().lower()
    if not main:
        return set()
    domain = _domain(main)
    result = {main}
    for address in others:
        address = (address or "").strip().lower()
        if address and _domain(address) == domain and not is_ours(address):
            result.add(address)
    return result


def replies(messages: Sequence[Mapping[str, Any]], *, customer_email: str, others: Iterable[str] = (),
            own: Iterable[str] = (), after_id: str = "") -> Dict[str, Any]:
    """{"replied": [customer replies], "bounced": bool} for the messages after `after_id` (all when not found)."""
    start = 0
    if after_id:
        for index, message in enumerate(messages):
            if str(message.get("id") or "") == after_id:
                start = index + 1
                break
    customers = customer_addresses(customer_email, others)
    found: List[Mapping[str, Any]] = []
    bounced = False
    for message in messages[start:]:
        sender = sender_of(message)
        if is_bounce(message):
            bounced = True
            continue
        if not sender or is_ours(sender, own) or is_auto_reply(message):
            continue
        if sender in customers:
            found.append(message)
    return {"replied": found, "bounced": bounced}


def enquiry_numbers(enquiry_id: str, enquiry: Optional[Mapping[str, Any]] = None) -> List[str]:
    values = [str(enquiry_id or ""), str((enquiry or {}).get("enq_number") or "")]
    return [v.strip() for v in dict.fromkeys(values) if v.strip()]


def _mentions(message: Mapping[str, Any], numbers: Sequence[str]) -> bool:
    names = " ".join(str((a or {}).get("filename") or (a or {}).get("name") or "")
                     for a in (message.get("attachments") or []))
    text = " ".join([str(message.get("subject") or ""), str(message.get("body_text") or ""),
                     str(message.get("snippet") or ""), names]).upper()
    return any(number.upper() in text for number in numbers)


def quote_message(messages: Sequence[Mapping[str, Any]], *, own: Iterable[str], numbers: Sequence[str],
                  message_id: str = "") -> Optional[Mapping[str, Any]]:
    """The newest quotation / PI email of ours in the thread (the recorded one, or one naming the enquiry)."""
    own = {a.lower() for a in own}
    found = None
    for message in messages:
        if not is_ours(sender_of(message), own):
            continue
        if (message_id and str(message.get("id") or "") == message_id) or _mentions(message, numbers):
            found = message
    return found


def check(messages: Sequence[Mapping[str, Any]], *, customer_email: str, subject: str, own: Iterable[str],
          numbers: Sequence[str], message_id: str = "") -> Tuple[bool, str]:
    """(passes, reason when it does not) for one thread's messages."""
    customer = (customer_email or "").strip().lower()
    if not messages:
        return False, "the thread is empty or was deleted"
    if not customer or "@" not in customer:
        return False, "no customer email on the enquiry"
    if not any(customer in participants(m) for m in messages):
        return False, f"{customer} is not in this thread"
    wanted = normalize_subject(subject)
    if not wanted or normalize_subject(messages[0].get("subject")) != wanted:
        return False, "the thread's subject does not match the quotation / PI email"
    if quote_message(messages, own=own, numbers=numbers, message_id=message_id) is None:
        return False, "the quotation / PI email is not in this thread"
    return True, ""


def find(gmail, *, customer_email: str, numbers: Sequence[str], own: Iterable[str], subject: str = "",
         limit: int = 20) -> Optional[Dict[str, Any]]:
    """Search our sent mail for the enquiry's quotation / PI and return the best passing thread, or None.

    {"thread_id", "messages", "quote" (the quotation / PI message), "subject"}. Without a known subject,
    each thread is checked against its own quotation / PI email's subject.
    """
    customer = (customer_email or "").strip().lower()
    if not customer or not numbers:
        return None
    query = " OR ".join(f'"{n}"' for n in numbers)
    results = gmail.search_messages(f"from:me to:{customer} ({query})", limit=limit) or []
    best = None
    for thread_id in dict.fromkeys(str(m.get("thread_id") or "") for m in results):
        if not thread_id:
            continue
        messages = gmail.get_thread_messages(thread_id, limit=50) or []
        quote = quote_message(messages, own=own, numbers=numbers)
        if quote is None:
            continue
        wanted = subject or quote.get("subject") or ""
        ok, _ = check(messages, customer_email=customer, subject=wanted, own=own, numbers=numbers)
        if not ok:
            continue
        when = quote.get("parsed_date")
        if best is None or (when and (best["when"] is None or when > best["when"])):
            best = {"thread_id": thread_id, "messages": messages, "quote": quote,
                    "subject": normalize_subject(wanted), "when": when}
    return best


def document_of(message: Mapping[str, Any]) -> str:
    """"proforma" when the email is about a proforma invoice, otherwise "quotation"."""
    names = " ".join(str((a or {}).get("filename") or "") for a in (message.get("attachments") or []))
    text = f"{message.get('subject') or ''} {names}"
    return "proforma" if re.search(r"\b(proforma|pro-forma|PI)\b", text, re.I) else "quotation"
