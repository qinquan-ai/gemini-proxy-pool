import unittest

from app.core.translator import content_to_parts, openai_to_gemini


class TranslatorTests(unittest.TestCase):
    def test_text_and_inline_image_are_preserved(self):
        parts = content_to_parts(
            [
                {"type": "text", "text": "describe"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ]
        )
        self.assertEqual(parts[0], {"text": "describe"})
        self.assertEqual(
            parts[1],
            {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
        )

    def test_system_message_is_kept_separate(self):
        contents, system = openai_to_gemini(
            [
                {"role": "system", "content": "be precise"},
                {"role": "user", "content": "hello"},
            ]
        )
        self.assertEqual(system, {"parts": [{"text": "be precise"}]})
        self.assertEqual(contents[0]["parts"], [{"text": "hello"}])


if __name__ == "__main__":
    unittest.main()
