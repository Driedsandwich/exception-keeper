"""Schema switches and bounded JSON repair, without a jsonschema dependency.

Provider formats remain unverified: https://docs.tokenfactory.nebius.com/ai-models-inference/json.md
"""
import json
import os
import re

STYLES = ("nebius_doc", "openai", "json_object", "none")


def response_format(schema, schema_style=None):
    style = schema_style or os.environ.get("EK_SCHEMA_STYLE", "openai")
    if style not in STYLES:
        raise ValueError("Unknown schema_style")
    if style == "none":
        return None
    if style == "json_object":
        return {"type": "json_object"}
    value = schema if style == "nebius_doc" else {"name": "review_result", "schema": schema, "strict": True}
    return {"type": "json_schema", "json_schema": value}


def parse(content):
    if not isinstance(content, str):
        raise ValueError("Missing text content")
    content = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", content, flags=re.S | re.I)
    # An unfinished reasoning block is not a usable answer.
    content = re.sub(r"<think\b[^>]*>.*\Z", "", content, flags=re.S | re.I)
    decoder = json.JSONDecoder(parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    for index, char in enumerate(content):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(content[index:])
                if isinstance(value, dict):
                    return value
            except (ValueError, RecursionError):
                continue
    raise ValueError("No valid JSON object found")


def validate(value, schema, path="$"):
    errors = []
    if "anyOf" in schema:
        if not any(not validate(value, sub, path) for sub in schema["anyOf"]):
            return [f"{path}: does not match allowed shapes"]
        return []
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    def matches(k):
        return {"object": isinstance(value, dict), "array": isinstance(value, list),
                "string": isinstance(value, str), "integer": type(value) is int,
                "number": type(value) in (int, float), "boolean": type(value) is bool,
                "null": value is None, None: True}.get(k, False)
    if not any(matches(k) for k in kinds):
        return [f"{path}: expected {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: invalid enum value")
    if isinstance(value, str) and len(value) > schema.get("maxLength", float("inf")):
        errors.append(f"{path}: string too long")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors.extend(f"{path}.{key}: required" for key in schema.get("required", []) if key not in value)
        for key, item in value.items():
            if key in props:
                errors.extend(validate(item, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", float("inf")):
            errors.append(f"{path}: too many items")
        for index, item in enumerate(value):
            errors.extend(validate(item, schema.get("items", {}), f"{path}[{index}]"))
    return errors[:30]


def decode(content, schema):
    try:
        value = parse(content)
        errors = validate(value, schema)
        return (None if errors else value), errors
    except (ValueError, RecursionError):
        return None, ["Output must contain a valid JSON object"]


def repair_message(errors):
    return {"role": "user", "content": "Return corrected JSON only. Validation errors: " +
            "; ".join(errors) + ". Do not compute numbers; impact is given."}


def request_json(call, model, messages, schema, *, schema_style=None, max_tokens=1024):
    messages = list(messages)
    for attempt in range(2):
        result = call(model, messages, response_format=response_format(schema, schema_style),
                      max_tokens=max_tokens, temperature=0)
        value, errors = decode(result.content, schema)
        if value is not None:
            return value, []
        if not attempt:
            messages.extend([{"role": "assistant", "content": result.content or ""}, repair_message(errors)])
    return None, errors
