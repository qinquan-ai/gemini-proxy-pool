from fastapi import HTTPException

from app.core.translator import openai_to_gemini, translate_tools


def build_generation_config(openai_request: dict) -> dict:
    mapping = {
        "temperature": "temperature",
        "top_p": "topP",
        "max_tokens": "maxOutputTokens",
        "max_completion_tokens": "maxOutputTokens",
        "stop": "stopSequences",
    }
    config = {}
    for source, target in mapping.items():
        if source in openai_request and openai_request[source] is not None:
            config[target] = openai_request[source]
    if isinstance(config.get("stopSequences"), str):
        config["stopSequences"] = [config["stopSequences"]]
    return config


def build_gemini_payload(openai_request: dict) -> dict:
    contents, system_instruction = openai_to_gemini(
        openai_request.get("messages", [])
    )
    if not contents:
        raise HTTPException(status_code=400, detail="At least one message is required")

    payload = {"contents": contents}
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    tools = translate_tools(openai_request.get("tools", []))
    if tools:
        payload["tools"] = tools

    generation_config = build_generation_config(openai_request)
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload
