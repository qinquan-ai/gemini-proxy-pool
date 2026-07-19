import json
import re


DATA_URL_PATTERN = re.compile(
    r"^data:(?P<mime>[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
    re.DOTALL,
)

def normalize_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif "text" in item and "type" not in item:
                    text_parts.append(item.get("text", ""))
        return "\n".join(text_parts)
    return str(content)


def _data_url_part(url: str) -> dict | None:
    match = DATA_URL_PATTERN.match(url)
    if not match:
        return None
    return {
        "inlineData": {
            "mimeType": match.group("mime"),
            "data": match.group("data"),
        }
    }


def content_to_parts(content) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content.strip() else []
    if not isinstance(content, list):
        return [{"text": str(content)}]

    parts = []
    for item in content:
        if isinstance(item, str):
            if item.strip():
                parts.append({"text": item})
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type in ("text", "input_text"):
            text = item.get("text", "")
            if text:
                parts.append({"text": text})
            continue

        if item_type in ("image_url", "video_url"):
            media = item.get(item_type, {})
            url = media.get("url", "") if isinstance(media, dict) else str(media)
            inline_part = _data_url_part(url)
            if inline_part:
                parts.append(inline_part)
            elif url.startswith("gs://") or "/v1beta/files/" in url:
                parts.append({"fileData": {"fileUri": url}})
            continue

        if item_type == "input_audio":
            audio = item.get("input_audio", {})
            data = audio.get("data") if isinstance(audio, dict) else None
            audio_format = audio.get("format", "wav") if isinstance(audio, dict) else "wav"
            if data:
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": f"audio/{audio_format}",
                            "data": data,
                        }
                    }
                )
            continue

        if "text" in item and "type" not in item:
            parts.append({"text": item.get("text", "")})
    return parts


def _sanitize_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    ALLOWED_SCHEMA_KEYS = {"type", "description", "properties", "required", "enum", "items", "format", "nullable", "default"}
    cleaned = {}
    for key, value in schema.items():
        if key not in ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: _sanitize_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = _sanitize_schema(value)
        else:
            cleaned[key] = value
    return cleaned


def translate_tools(openai_tools: list) -> list:
    if not openai_tools:
        return []
    gemini_functions = []
    for t in openai_tools:
        if t.get("type") == "function" and "function" in t:
            fn = t["function"]
            cleaned_fn = {"name": fn.get("name", "")}
            if fn.get("description"):
                cleaned_fn["description"] = fn["description"]
            if fn.get("parameters"):
                cleaned_fn["parameters"] = _sanitize_schema(fn["parameters"])
            gemini_functions.append(cleaned_fn)
    return [{"functionDeclarations": gemini_functions}] if gemini_functions else []


def openai_to_gemini(openai_messages: list) -> tuple:
    contents = []
    system_instruction = None

    for msg in openai_messages:
        role = msg.get("role", "user")
        if role == "system":
            system_instruction = {"parts": [{"text": normalize_content(msg.get("content"))}]}
            continue

        raw_content = normalize_content(msg.get("content"))
        parts = content_to_parts(msg.get("content"))

        # Extract Thought if present in content (for Gemini 3 compatibility)
        thought_text = ""
        display_content = raw_content
        if "<!--THOUGHT_BEGIN-->" in raw_content and "<!--THOUGHT_END-->" in raw_content:
            start_tag = "<!--THOUGHT_BEGIN-->"
            end_tag = "<!--THOUGHT_END-->"
            start_idx = raw_content.find(start_tag) + len(start_tag)
            end_idx = raw_content.find(end_tag)
            thought_text = raw_content[start_idx:end_idx]
            # Replace the thought tag in the text part to preserve visibility if needed,
            # but usually we want to keep it separate in Gemini 'thought' part.
            display_content = raw_content[:raw_content.find(start_tag)] + raw_content[end_idx+len(end_tag):]

        if thought_text:
            parts.append({"thought": thought_text})

        if display_content != raw_content:
            parts = [part for part in parts if "text" not in part]
            if display_content.strip():
                parts.insert(0, {"text": display_content.strip()})

        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg.get("tool_calls", []):
                if tc.get("type") == "function":
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
                    except:
                        args = {}
                    parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        if role == "tool":
            tool_name = msg.get("name", "unknown_tool")
            try:
                parsed_content = json.loads(msg.get("content", "")) if isinstance(msg.get("content"), str) else msg.get("content", {})
            except:
                parsed_content = {"result": msg.get("content", "")}
            contents.append({"role": "user", "parts": [{"functionResponse": {"name": tool_name, "response": parsed_content}}]})
            continue

        gemini_role = "model" if role == "assistant" else "user"
        if not parts and display_content:  # Fallback
            parts = [{"text": display_content}]

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    return contents, system_instruction
