"""Kiro ↔ Anthropic message format translator.

Kiro uses a custom wire format (userInputMessage / assistantResponseMessage)
while headroom's SmartCrusher operates on Anthropic's messages format.
This module handles lossless conversion between the two.

Kiro format:
    [{"userInputMessage": {"content": "...", "images": [...], "userInputMessageContext": {...}}},
     {"assistantResponseMessage": {"content": "...", "toolUses": [...]}}]

Anthropic format:
    [{"role": "user", "content": [{"type": "text", "text": "..."}]},
     {"role": "assistant", "content": [{"type": "text", "text": "..."}]}]
"""

from __future__ import annotations

from typing import Any


def kiro_to_anthropic(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert kiro wire-format history to Anthropic messages format.

    Each kiro message becomes one Anthropic message. Tool uses and tool results
    are mapped to their Anthropic content block equivalents.
    """
    messages: list[dict[str, Any]] = []

    for entry in history:
        if "userInputMessage" in entry:
            messages.append(_translate_user_message(entry["userInputMessage"]))
        elif "assistantResponseMessage" in entry:
            messages.append(_translate_assistant_message(entry["assistantResponseMessage"]))

    return messages


def anthropic_to_kiro(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic messages format back to kiro wire format.

    Inverse of kiro_to_anthropic. Used after headroom compresses the
    Anthropic-format messages to rebuild the kiro request body.
    """
    history: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            history.append(_rebuild_user_message(msg))
        elif role == "assistant":
            history.append(_rebuild_assistant_message(msg))

    return history


# --- Kiro → Anthropic ---


def _translate_user_message(um: dict[str, Any]) -> dict[str, Any]:
    """Translate a kiro userInputMessage to an Anthropic user message."""
    content_blocks: list[dict[str, Any]] = []

    # Check for tool results first (they replace text content)
    ctx = um.get("userInputMessageContext")
    if ctx and isinstance(ctx, dict):
        tool_results = ctx.get("toolResults")
        if tool_results and isinstance(tool_results, list):
            for tr in tool_results:
                block = _translate_tool_result(tr)
                if block:
                    content_blocks.append(block)

    # Images
    images = um.get("images")
    if images and isinstance(images, list):
        for img in images:
            block = _translate_image(img)
            if block:
                content_blocks.append(block)

    # Text content
    text = um.get("content")
    if text and isinstance(text, str) and text.strip():
        content_blocks.append({"type": "text", "text": text})

    # If we have tool results but no other content, that's fine (tool-result-only message)
    # If we have nothing at all, add an empty text block to maintain message structure
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    return {"role": "user", "content": content_blocks}


def _translate_assistant_message(arm: dict[str, Any]) -> dict[str, Any]:
    """Translate a kiro assistantResponseMessage to an Anthropic assistant message."""
    content_blocks: list[dict[str, Any]] = []

    # Text content
    text = arm.get("content")
    if text and isinstance(text, str) and text.strip():
        content_blocks.append({"type": "text", "text": text})

    # Tool uses
    tool_uses = arm.get("toolUses")
    if tool_uses and isinstance(tool_uses, list):
        for tu in tool_uses:
            content_blocks.append({
                "type": "tool_use",
                "id": tu.get("toolUseId", ""),
                "name": tu.get("name", ""),
                "input": tu.get("input", {}),
            })

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    return {"role": "assistant", "content": content_blocks}


def _translate_tool_result(tr: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a kiro tool result to an Anthropic tool_result block."""
    if not isinstance(tr, dict):
        return None

    tool_use_id = tr.get("toolUseId", "")
    status = tr.get("status", "success")
    content_parts = tr.get("content", [])

    # Build anthropic content from kiro content parts
    anthropic_content: list[dict[str, Any]] = []
    for part in content_parts:
        if isinstance(part, dict) and "text" in part:
            anthropic_content.append({"type": "text", "text": part["text"]})

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": status != "success",
        "content": anthropic_content,
    }


def _translate_image(img: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a kiro image to an Anthropic image block."""
    if not isinstance(img, dict):
        return None

    # Kiro images can be in two formats:
    # 1. {"data": "base64...", "format": "image/png"}
    # 2. {"source": {"bytes": "base64..."}}
    data = None
    media_type = "image/png"

    if "data" in img:
        data = img["data"]
        media_type = img.get("format", "image/png")
    elif "source" in img and isinstance(img["source"], dict):
        data = img["source"].get("bytes")

    if not data:
        return None

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


# --- Anthropic → Kiro ---


def _rebuild_user_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a kiro userInputMessage from an Anthropic user message."""
    content_blocks = msg.get("content", [])

    text_parts: list[str] = []
    tool_results: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif block_type == "tool_result":
            tool_results.append(_rebuild_tool_result(block))
        elif block_type == "image":
            images.append(_rebuild_image(block))

    um: dict[str, Any] = {"content": "\n".join(text_parts) if text_parts else ""}

    if tool_results:
        um["userInputMessageContext"] = {"toolResults": tool_results}

    if images:
        um["images"] = images

    return {"userInputMessage": um}


def _rebuild_assistant_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a kiro assistantResponseMessage from an Anthropic assistant message."""
    content_blocks = msg.get("content", [])

    text_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif block_type == "tool_use":
            tool_uses.append({
                "toolUseId": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })

    arm: dict[str, Any] = {"content": "\n".join(text_parts) if text_parts else ""}

    if tool_uses:
        arm["toolUses"] = tool_uses

    return {"assistantResponseMessage": arm}


def _rebuild_tool_result(block: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a kiro tool result from an Anthropic tool_result block."""
    content_parts: list[dict[str, str]] = []
    anthropic_content = block.get("content", [])

    for part in anthropic_content:
        if isinstance(part, dict) and part.get("type") == "text":
            content_parts.append({"text": part.get("text", "")})

    return {
        "toolUseId": block.get("tool_use_id", ""),
        "content": content_parts,
        "status": "error" if block.get("is_error") else "success",
    }


def _rebuild_image(block: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a kiro image from an Anthropic image block."""
    source = block.get("source", {})
    return {
        "data": source.get("data", ""),
        "format": source.get("media_type", "image/png"),
    }
