import json
import unittest

from app.core.responses_adapter import (
    chat_completion_to_response,
    responses_to_chat_request,
    sse_event,
)


class ResponsesAdapterTests(unittest.TestCase):
    def test_text_instructions_and_tools_are_translated(self):
        translated = responses_to_chat_request(
            {
                "model": "gemini-2.5-flash",
                "instructions": "Be precise",
                "input": "Hello",
                "max_output_tokens": 128,
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "description": "Lookup a value",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )
        self.assertEqual(translated["messages"][0]["role"], "system")
        self.assertEqual(translated["messages"][1]["content"], "Hello")
        self.assertEqual(translated["max_completion_tokens"], 128)
        self.assertEqual(translated["tools"][0]["function"]["name"], "lookup")

    def test_function_call_and_output_preserve_name(self):
        translated = responses_to_chat_request(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": '{"id":1}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": '{"value":"ok"}',
                    },
                ]
            }
        )
        self.assertEqual(translated["messages"][0]["role"], "assistant")
        self.assertEqual(translated["messages"][1]["name"], "lookup")

    def test_chat_completion_becomes_response(self):
        response = chat_completion_to_response(
            {
                "model": "gemini-2.5-flash",
                "choices": [
                    {"message": {"role": "assistant", "content": "Hi"}}
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
            {"model": "gemini-2.5-flash"},
            "resp_test",
            123,
        )
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["output"][0]["content"][0]["text"], "Hi")
        self.assertEqual(response["usage"]["total_tokens"], 5)

    def test_sse_event_has_named_event_and_json_payload(self):
        event = sse_event("response.created", 0, response={"id": "resp_1"})
        lines = event.strip().splitlines()
        self.assertEqual(lines[0], "event: response.created")
        payload = json.loads(lines[1].removeprefix("data: "))
        self.assertEqual(payload["sequence_number"], 0)


if __name__ == "__main__":
    unittest.main()
