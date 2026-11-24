import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from dashboard.ai_service import AIReplyRequest, AIReplyService, AIServiceError


class AIReplyServiceTests(SimpleTestCase):
    summary = (
        "From: client@example.com\n"
        "To: ops@example.com\n"
        "Date: 2024-06-01 10:00\n"
        "---\n"
        "Could you confirm the updated production timeline and let us know if any blockers remain?"
    )

    @override_settings(AI_REPLY_ALLOW_STUB=True, DEBUG=True)
    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_stub_reply_used_when_gemini_missing(self):
        service = AIReplyService()
        reply = service.generate_reply(AIReplyRequest(thread_summary=self.summary, tone="friendly"))

        self.assertIn("Hey there", reply)
        self.assertIn("production timeline", reply)
        self.assertNotIn("Gemini is not configured", reply)

    @override_settings(AI_REPLY_ALLOW_STUB=False, DEBUG=False)
    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_error_when_stub_disabled_and_gemini_missing(self):
        with self.assertRaises(AIServiceError):
            AIReplyService()

    @override_settings(AI_REPLY_ALLOW_STUB=False, DEBUG=False)
    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False)
    def test_service_uses_gemini_when_available(self):
        with mock.patch('dashboard.ai_service.genai') as mock_genai:
            instance = mock.Mock()
            mock_genai.GenerativeModel.return_value = instance
            service = AIReplyService()
            mock_genai.configure.assert_called_once_with(api_key="test-key")
            mock_genai.GenerativeModel.assert_called_once_with(service._model_name)
            self.assertIs(service.client, instance)

            request = AIReplyRequest(thread_summary=self.summary, tone="professional")
            mock_response = mock.Mock()
            mock_response.text = "Done"
            instance.generate_content.return_value = mock_response

            reply = service.generate_reply(request)
            self.assertEqual(reply, "Done")
            instance.generate_content.assert_called_once_with(service._build_prompt(request))


@override_settings(AI_REPLY_ALLOW_STUB=True, DEBUG=True)
@mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
class SampleDraftToneTests(SimpleTestCase):
    """Without Gemini, each tone on the compose page must give a visibly different draft."""

    summary = AIReplyServiceTests.summary
    tones = ["professional and concise", "friendly and warm", "brief and direct", "empathetic"]

    def _drafts(self, **extra):
        service = AIReplyService()
        drafts = [service.generate_reply(AIReplyRequest(thread_summary=self.summary, tone=t, **extra))
                  for t in self.tones]
        self.assertTrue(service.last_reply_was_sample)
        return drafts

    def test_four_tones_give_four_different_replies(self):
        drafts = self._drafts()
        self.assertEqual(len(set(drafts)), 4)
        for draft in drafts:
            self.assertIn("production timeline", draft)

    def test_follow_up_drafts_differ_and_use_sir_madam_and_subject(self):
        drafts = self._drafts(purpose="follow_up", customer_name="Ravi", enquiry_subject="EPDM gaskets")
        self.assertEqual(len(set(drafts)), 4)
        for draft in drafts:
            self.assertTrue(draft.startswith("Dear Sir/Madam,"))
            self.assertNotIn("Ravi", draft)
            self.assertIn("EPDM gaskets", draft)
            self.assertTrue(draft.endswith("Best regards,\nAVIGIRI Team"))

    def test_follow_up_prompt_for_gemini_uses_sir_madam_tone_and_subject(self):
        service = AIReplyService()
        prompt = service._build_prompt(AIReplyRequest(
            thread_summary=self.summary, tone="empathetic", purpose="follow_up",
            customer_name="Ravi", enquiry_subject="EPDM gaskets"))
        self.assertIn("Tone: empathetic", prompt)
        self.assertNotIn("Ravi", prompt)
        self.assertIn('"Dear Sir/Madam,"', prompt)
        self.assertIn("Best regards,\nAVIGIRI Team", prompt)
        self.assertIn("Enquiry subject: EPDM gaskets", prompt)
        self.assertIn("Do not invent prices", prompt)

    def test_other_purposes_unchanged(self):
        service = AIReplyService()
        reply = service.generate_reply(AIReplyRequest(thread_summary="x", tone="operations analyst", purpose="raw"))
        self.assertIn("AI stub is enabled", reply)


class GeminiFlagTests(SimpleTestCase):
    @override_settings(AI_REPLY_ALLOW_STUB=True, DEBUG=False)
    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False)
    def test_gemini_failure_falls_back_and_is_flagged(self):
        with mock.patch('dashboard.ai_service.genai') as mock_genai:
            mock_genai.GenerativeModel.return_value.generate_content.side_effect = RuntimeError("quota")
            service = AIReplyService()
            reply = service.generate_reply(AIReplyRequest(thread_summary="hi", tone="friendly"))
        self.assertTrue(service.last_reply_was_sample)
        self.assertIn("Hey there", reply)

    @override_settings(AI_REPLY_ALLOW_STUB=False, DEBUG=False)
    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False)
    def test_real_gemini_reply_not_flagged(self):
        with mock.patch('dashboard.ai_service.genai') as mock_genai:
            mock_genai.GenerativeModel.return_value.generate_content.return_value = mock.Mock(text="Real draft")
            service = AIReplyService()
            self.assertEqual(service.generate_reply(AIReplyRequest(thread_summary="hi")), "Real draft")
        self.assertFalse(service.last_reply_was_sample)
