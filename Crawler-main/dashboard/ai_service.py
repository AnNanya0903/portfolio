"""Utility helpers for generating AI-powered email replies."""

from __future__ import annotations

import logging
import os
import textwrap
from dataclasses import dataclass
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency in dev
    import google.generativeai as genai
except ImportError:  # pragma: no cover - handled gracefully in runtime
    genai = None


class AIServiceError(RuntimeError):
    """Raised when AI generation fails or is unavailable."""


@dataclass
class AIReplyRequest:
    """Encapsulate the information sent to the AI service."""

    thread_summary: str
    tone: Optional[str] = None
    purpose: str = "email_reply"
    # Used when purpose == "follow_up" (manual enquiry follow-up from the enquiries board).
    customer_name: Optional[str] = None
    enquiry_subject: Optional[str] = None


def _tone_key(tone: Optional[str]) -> str:
    """Map a tone description ("friendly and warm", "brief and direct", ...) to one of four styles."""
    hint = (tone or "").lower()
    if any(word in hint for word in ("friendly", "warm", "casual")):
        return "friendly"
    if any(word in hint for word in ("direct", "brief")):
        return "direct"
    if "empath" in hint:
        return "empathetic"
    return "professional"


# Sample drafts used only when Gemini is unavailable, one per tone so the choice is visible.
_SAMPLE_REPLIES = {
    "professional": (
        "Hello,",
        'Thank you for your message regarding "{excerpt}". We have noted the details and will '
        "come back to you shortly with the next steps.",
        "Please let us know if there is anything else you need in the meantime.",
    ),
    "friendly": (
        "Hey there,",
        'Thanks so much for getting in touch about "{excerpt}". We are on it and will get back '
        "to you very soon with an update!",
        "If anything else comes up, just drop us a line. Happy to help!",
    ),
    "direct": (
        "Hi,",
        'Noted: "{excerpt}". We will confirm the next steps shortly.',
        "",
    ),
    "empathetic": (
        "Hello,",
        'Thank you for sharing this, and we understand how important "{excerpt}" is to you. '
        "We are looking into it carefully and will update you as soon as we can.",
        "Please don't hesitate to reach out if there is anything we can do to help in the meantime.",
    ),
}

_SAMPLE_FOLLOW_UPS = {
    "professional": (
        "Dear Sir/Madam,",
        "I hope you are well. We are following up on your recent enquiry regarding {subject}.",
        "Could you please share any updates, or let us know if you need any further information "
        "from our side?",
    ),
    "friendly": (
        "Dear Sir/Madam,",
        "Hope you're doing well! Just checking in on your enquiry about {subject}.",
        "Whenever you get a moment, we'd love to hear how things are going, and if there's "
        "anything else you need from us, just say the word.",
    ),
    "direct": (
        "Dear Sir/Madam,",
        "Following up on your enquiry about {subject}.",
        "Could you confirm the current status or your next step?",
    ),
    "empathetic": (
        "Dear Sir/Madam,",
        "We know things can get busy, so we just wanted to gently follow up on your enquiry "
        "about {subject}.",
        "There's no rush. Whenever you're ready, please let us know if you have any questions "
        "or need anything further from us. We're happy to help.",
    ),
}


class AIReplyService:
    """Wrapper around Google Gemini API with local fallbacks for dev."""

    def __init__(self):
        self._model_name = os.getenv('GEMINI_MODEL') or getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash')
        self._allow_stub = getattr(settings, 'AI_REPLY_ALLOW_STUB', bool(getattr(settings, 'DEBUG', False)))
        self._stub_signature = getattr(settings, 'AI_REPLY_STUB_SIGNATURE', "Thanks,\nMagicWand Team")
        api_key = os.environ.get('GEMINI_API_KEY')
        if api_key is None:
            api_key = getattr(settings, 'GEMINI_API_KEY', '')
        self._using_stub = False
        # True when the last generate_reply() returned a sample draft instead of a Gemini answer.
        self.last_reply_was_sample = False

        if api_key and genai is not None:
            genai.configure(api_key=api_key)
            self.client = genai.GenerativeModel(self._model_name)
            return

        if not self._allow_stub:
            raise AIServiceError(
                "Gemini is not configured. Set GEMINI_API_KEY and install the google-generativeai package."
            )

        self._using_stub = True
        self.client = None
        logger.info(
            "Gemini API is unavailable; using local stub responses. Set GEMINI_API_KEY to enable real replies."
        )

    def generate_reply(self, request: AIReplyRequest) -> str:
        """Generate a suggested reply using the conversation summary."""
        self.last_reply_was_sample = False
        if self._using_stub:
            self.last_reply_was_sample = True
            return self._build_stub_reply(request)

        prompt = self._build_prompt(request)
        try:
            response = self.client.generate_content(prompt)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - depends on network
            logger.error("AI reply generation failed: %s", exc)
            if self._allow_stub:
                logger.info("Falling back to local AI stub after Gemini failure.")
                self._using_stub = True
                self.last_reply_was_sample = True
                return self._build_stub_reply(request)
            raise AIServiceError("AI reply generation failed") from exc

        try:
            text = response.text
        except Exception as exc:  # pragma: no cover - structure may vary
            logger.error("Unexpected AI response structure: %s", exc)
            raise AIServiceError("AI reply generation produced no content") from exc

        return text.strip()

    def _build_prompt(self, request: AIReplyRequest) -> str:
        tone_hint = request.tone or 'professional and concise'
        purpose = (request.purpose or "email_reply").strip().lower()
        if purpose in {"raw", "prompt"}:
            return (request.thread_summary or "").strip()
        if purpose in {"assistant", "analysis", "copilot", "deskera"}:
            return (
                "You are a helpful operations copilot. Follow the instructions and context below.\n"
                "Do not use email-style greetings/signatures unless explicitly requested.\n"
                "Prefer Markdown tables when listing records.\n\n"
                f"{(request.thread_summary or '').strip()}\n\n"
                "Answer:"
            )
        if purpose == "follow_up":
            subject = (request.enquiry_subject or "").strip() or "their recent enquiry"
            return (
                "You are writing a follow-up email on behalf of AVIGIRI to a customer who has "
                "not replied recently.\n"
                f"Enquiry subject: {subject}\n"
                f"Tone: {tone_hint}. Make the wording clearly reflect this tone.\n"
                "Politely ask whether they have any updates or need more information from us. "
                "Use the conversation below for context only. Do not invent prices, dates, "
                "quantities or commitments. Do not address anyone by name. Start with exactly "
                "\"Dear Sir/Madam,\" and end with:\nBest regards,\nAVIGIRI Team\n"
                "Return only the email body, without a subject line.\n\n"
                "Conversation History:\n"
                f"{request.thread_summary}\n\n"
                "Follow-up email:"
            )
        return (
            "You are an email assistant. Read the conversation below and craft a "
            f"{tone_hint} reply addressing the latest message. The reply should be "
            "polite, actionable where needed, and keep formatting suitable for email.\n\n"
            "Conversation History:\n"
            f"{request.thread_summary}\n\n"
            "Reply:"
        )

    def _build_stub_reply(self, request: AIReplyRequest) -> str:
        """Return a lightweight reply when Gemini is not configured."""
        purpose = (request.purpose or "email_reply").strip().lower()
        tone = _tone_key(request.tone)

        if purpose == "follow_up":
            subject = (request.enquiry_subject or "").strip() or "your recent enquiry"
            greeting, *paragraphs = _SAMPLE_FOLLOW_UPS[tone]
            lines = [greeting]
            for paragraph in paragraphs:
                lines += ["", paragraph.format(subject=subject)]
            lines += ["", "Best regards,", "AVIGIRI Team"]
            return "\n".join(lines)

        if purpose not in {"email_reply", "email"}:
            return (
                "AI stub is enabled (Gemini not configured). "
                "Set `GEMINI_API_KEY` to generate Copilot answers."
            )

        excerpt = self._extract_latest_excerpt(request.thread_summary)
        excerpt = textwrap.shorten(excerpt or "the details you shared", width=120, placeholder="…")

        greeting, *paragraphs = _SAMPLE_REPLIES[tone]
        lines = [greeting]
        for paragraph in paragraphs:
            if paragraph:
                lines += ["", paragraph.format(excerpt=excerpt)]
        lines += ["", self._stub_signature]
        return "\n".join(line.rstrip() for line in lines).strip()

    @staticmethod
    def _extract_latest_excerpt(summary: str) -> str:
        if not summary:
            return ""

        segments = [segment for segment in summary.split("From:") if segment.strip()]
        latest = segments[-1]
        if '---' in latest:
            latest = latest.split('---', 1)[-1]
        latest = latest.strip()
        latest = " ".join(latest.split())
        return latest
