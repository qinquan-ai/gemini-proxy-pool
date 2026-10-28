import json
import uuid

import httpx

from app.core.responses_adapter import response_envelope, sse_event
from app.execution.streaming import stream_with_retry


async def responses_stream_with_retry(
    client: httpx.AsyncClient,
    payload: dict,
    spec,
    response_id: str,
    created_at: int,
    response_request: dict,
    chat_id: str,
):
    model = spec.model
    sequence = 0
    initial_response = response_envelope(
        response_id,
        model,
        response_request,
        status="in_progress",
        output=[],
        created_at=created_at,
    )
    yield sse_event("response.created", sequence, response=initial_response)
    sequence += 1
    yield sse_event("response.in_progress", sequence, response=initial_response)
    sequence += 1

    message_item = None
    text_chunks = []
    function_items = []

    async for chunk in stream_with_retry(client, payload, spec, chat_id, created_at):
        for line in chunk.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chat_chunk = json.loads(line[6:])
            except ValueError:
                continue
            choice = (chat_chunk.get("choices") or [{}])[0]
            delta = choice.get("delta", {})

            text_delta = delta.get("content")
            if text_delta:
                if message_item is None:
                    message_item = {
                        "id": f"msg_{uuid.uuid4().hex[:24]}",
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    }
                    yield sse_event(
                        "response.output_item.added",
                        sequence,
                        output_index=0,
                        item=message_item,
                    )
                    sequence += 1
                    yield sse_event(
                        "response.content_part.added",
                        sequence,
                        item_id=message_item["id"],
                        output_index=0,
                        content_index=0,
                        part={
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                            "logprobs": [],
                        },
                    )
                    sequence += 1
                text_chunks.append(text_delta)
                yield sse_event(
                    "response.output_text.delta",
                    sequence,
                    item_id=message_item["id"],
                    output_index=0,
                    content_index=0,
                    delta=text_delta,
                    logprobs=[],
                )
                sequence += 1

            for tool_call in delta.get("tool_calls", []) or []:
                function = tool_call.get("function", {})
                function_item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": tool_call.get("id")
                    or f"call_{uuid.uuid4().hex[:16]}",
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "{}"),
                }
                output_index = (1 if message_item else 0) + len(function_items)
                function_items.append(function_item)
                yield sse_event(
                    "response.output_item.added",
                    sequence,
                    output_index=output_index,
                    item=function_item,
                )
                sequence += 1
                yield sse_event(
                    "response.function_call_arguments.delta",
                    sequence,
                    item_id=function_item["id"],
                    output_index=output_index,
                    delta=function_item["arguments"],
                )
                sequence += 1

    output = []
    if message_item is not None:
        text = "".join(text_chunks)
        message_item = {
            **message_item,
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": text,
                    "annotations": [],
                    "logprobs": [],
                }
            ],
        }
        output.append(message_item)
        yield sse_event(
            "response.output_text.done",
            sequence,
            item_id=message_item["id"],
            output_index=0,
            content_index=0,
            text=text,
            logprobs=[],
        )
        sequence += 1
        yield sse_event(
            "response.content_part.done",
            sequence,
            item_id=message_item["id"],
            output_index=0,
            content_index=0,
            part=message_item["content"][0],
        )
        sequence += 1
        yield sse_event(
            "response.output_item.done",
            sequence,
            output_index=0,
            item=message_item,
        )
        sequence += 1

    for index, function_item in enumerate(function_items, start=len(output)):
        function_item = {**function_item, "status": "completed"}
        output.append(function_item)
        yield sse_event(
            "response.function_call_arguments.done",
            sequence,
            item_id=function_item["id"],
            output_index=index,
            arguments=function_item["arguments"],
        )
        sequence += 1
        yield sse_event(
            "response.output_item.done",
            sequence,
            output_index=index,
            item=function_item,
        )
        sequence += 1

    completed_response = response_envelope(
        response_id,
        model,
        response_request,
        status="completed",
        output=output,
        usage={
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        },
        created_at=created_at,
    )
    yield sse_event(
        "response.completed", sequence, response=completed_response
    )
