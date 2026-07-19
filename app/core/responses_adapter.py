import json
import time
import uuid


def _response_content_to_chat(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": part.get("text", "")})
        elif part_type == "input_image":
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part.get("image_url", "")},
                }
            )
        elif part_type == "input_file":
            file_url = part.get("file_url") or part.get("file_id") or ""
            parts.append(
                {"type": "video_url", "video_url": {"url": file_url}}
            )
    return parts


def _responses_tools_to_chat(tools: list) -> list:
    translated = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        translated.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object"}),
                },
            }
        )
    return translated


def responses_to_chat_request(request_body: dict) -> dict:
    messages = []
    instructions = request_body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    input_value = request_body.get("input", "")
    if isinstance(input_value, str):
        if input_value:
            messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        call_names = {
            item.get("call_id"): item.get("name", "tool")
            for item in input_value
            if isinstance(item, dict) and item.get("type") == "function_call"
        }
        for item in input_value:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "message")
            if item_type == "message":
                messages.append(
                    {
                        "role": item.get("role", "user"),
                        "content": _response_content_to_chat(item.get("content", [])),
                    }
                )
            elif item_type == "function_call":
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": item.get("call_id") or item.get("id"),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": item.get("arguments", "{}"),
                                },
                            }
                        ],
                    }
                )
            elif item_type == "function_call_output":
                call_id = item.get("call_id", "")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": call_names.get(call_id, item.get("name", "tool")),
                        "content": item.get("output", ""),
                    }
                )

    chat_request = {
        "model": request_body.get("model"),
        "messages": messages,
        "stream": bool(request_body.get("stream", False)),
    }
    if request_body.get("tools"):
        chat_request["tools"] = _responses_tools_to_chat(request_body["tools"])

    field_mapping = {
        "temperature": "temperature",
        "top_p": "top_p",
        "max_output_tokens": "max_completion_tokens",
    }
    for source, target in field_mapping.items():
        if request_body.get(source) is not None:
            chat_request[target] = request_body[source]
    return chat_request


def _usage_from_chat(chat_usage: dict) -> dict:
    input_tokens = int(chat_usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(chat_usage.get("completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": int(
            chat_usage.get("total_tokens", input_tokens + output_tokens) or 0
        ),
    }


def response_envelope(
    response_id: str,
    model: str,
    request_body: dict,
    *,
    status: str,
    output: list,
    usage: dict | None = None,
    created_at: int | None = None,
) -> dict:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": request_body.get("instructions"),
        "max_output_tokens": request_body.get("max_output_tokens"),
        "model": model,
        "output": output,
        "parallel_tool_calls": request_body.get("parallel_tool_calls", True),
        "previous_response_id": request_body.get("previous_response_id"),
        "reasoning": request_body.get("reasoning"),
        "store": request_body.get("store", False),
        "temperature": request_body.get("temperature"),
        "text": request_body.get("text", {"format": {"type": "text"}}),
        "tool_choice": request_body.get("tool_choice", "auto"),
        "tools": request_body.get("tools", []),
        "top_p": request_body.get("top_p"),
        "truncation": request_body.get("truncation", "disabled"),
        "usage": usage,
        "metadata": request_body.get("metadata", {}),
    }


def chat_completion_to_response(
    chat_completion: dict,
    request_body: dict,
    response_id: str,
    created_at: int,
) -> dict:
    model = chat_completion.get("model") or request_body.get("model") or ""
    choice = (chat_completion.get("choices") or [{}])[0]
    message = choice.get("message", {})
    output = []

    content = message.get("content")
    if content:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content,
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        )

    for tool_call in message.get("tool_calls", []) or []:
        function = tool_call.get("function", {})
        output.append(
            {
                "id": f"fc_{uuid.uuid4().hex[:24]}",
                "type": "function_call",
                "status": "completed",
                "call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                "name": function.get("name", ""),
                "arguments": function.get("arguments", "{}"),
            }
        )

    return response_envelope(
        response_id,
        model,
        request_body,
        status="completed",
        output=output,
        usage=_usage_from_chat(chat_completion.get("usage", {})),
        created_at=created_at,
    )


def sse_event(event_type: str, sequence_number: int, **payload) -> str:
    event = {"type": event_type, "sequence_number": sequence_number, **payload}
    return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
