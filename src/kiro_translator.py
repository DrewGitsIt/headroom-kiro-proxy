"""
Kiro ↔ Anthropic message format translator.

Translates between kiro's custom wire format (conversationState.history)
and the standard Anthropic Messages API format that headroom's compression
pipeline expects.

Kiro format:
    history = [
        {"userInputMessage": {"content": "...", "images": [...], "userInputMessageContext": {...}}},
        {"assistantResponseMessage": {"content": "...", "toolUses": [...]}},
        ...
    ]

Anthropic format:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "..."}]},
        {"role": "assistant", "content": [{"type": "text", "text": "..."}, {"type": "tool_use", ...}]},
        {"role": "user", "content": [{"type": "tool_result", ...}]},
        ...
    ]
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("kiro-proxy.translator")


def kiro_to_anthropic(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert kiro conversationState.history to Anthropic messages format.

    Groups consecutive tool-result user messages into the preceding tool-use
    assistant message's follow-up user turn (as Anthropic expects tool_result
    blocks in a user message following the assistant's tool_use).
    """
    messages: list[dict[str, Any]] = []

    for msg in history:
        if "userInputMessage" in msg:
            um = msg["userInputMessage"]
            content_blocks: list[dict[str, Any]] = []

            # Tool results go first (they're the response to the previous assistant's tool_use)
            ctx = um.get("userInputMessageContext", {})
            tool_results = ctx.get("toolResults", [])
            for tr in tool_results:
                result_content: list[dict[str, Any]] = []
                for part in tr.get("content", []):
                    if "text" in part:
                        result_content.append({"type": "text", "text": part["text"]})
                    elif "json" in part:
                        # Some tool results have JSON content
                        import json
                        result_content.append({"type": "text", "text": json.dumps(part["json"])})

                content_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tr.get("toolUseId", ""),
                    "content": result_content,
                    "is_error": tr.get("status") == "error",
                })

            # User text content
            text = um.get("content", "")
            if text:
                content_blocks.append({"type": "text", "text": text})

            # Images
            images = um.get("images", [])
            for img in images:
                # Handle both kiro image formats:
                # Format 1: {"data": "base64...", "format": "image/png"}
                # Format 2: {"source": {"bytes": "base64..."}}
                if "data" in img:
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.get("format", "image/png"),
                            "data": img["data"],
                        },
                    })
                elif "source" in img and "bytes" in img.get("source", {}):
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img["source"]["bytes"],
                        },
                    })

            if content_blocks:
                messages.append({"role": "user", "content": content_blocks})

        elif "assistantResponseMessage" in msg:
            arm = msg["assistantResponseMessage"]
            content_blocks = []

            # Text content
            text = arm.get("content", "")
            if text:
                content_blocks.append({"type": "text", "text": text})

            # Tool uses
            tool_uses = arm.get("toolUses", [])
            for tu in tool_uses:
                content_blocks.append({
                    "type": "tool_use",
                    "id": tu.get("toolUseId", ""),
                    "name": tu.get("name", ""),
                    "input": tu.get("input", {}),
                })

            if content_blocks:
                messages.append({"role": "assistant", "content": content_blocks})

    return messages


def anthropic_to_kiro(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic messages format back to kiro conversationState.history.

    Reverse of kiro_to_anthropic. Reconstructs kiro's nested structure from
    flat Anthropic message blocks.
    """
    history: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", [])

        # Handle string content (simple case)
        if isinstance(content, str):
            if role == "user":
                history.append({"userInputMessage": {"content": content}})
            elif role == "assistant":
                history.append({"assistantResponseMessage": {"content": content}})
            continue

        if role == "user":
            # Separate tool_results from text and images
            tool_results: list[dict[str, Any]] = []
            text_parts: list[str] = []
            images: list[dict[str, Any]] = []

            for block in content:
                block_type = block.get("type")

                if block_type == "tool_result":
                    tr_content: list[dict[str, Any]] = []
                    for part in block.get("content", []):
                        if part.get("type") == "text":
                            tr_content.append({"text": part["text"]})
                    tool_results.append({
                        "toolUseId": block.get("tool_use_id", ""),
                        "content": tr_content,
                        "status": "error" if block.get("is_error") else "success",
                    })

                elif block_type == "text":
                    text_parts.append(block.get("text", ""))

                elif block_type == "image":
                    source = block.get("source", {})
                    # Reconstruct kiro image format
                    images.append({
                        "data": source.get("data", ""),
                        "format": source.get("media_type", "image/png"),
                    })

            # Build the kiro userInputMessage
            um: dict[str, Any] = {}
            um["content"] = "\n".join(text_parts) if text_parts else ""

            if tool_results:
                um["userInputMessageContext"] = {"toolResults": tool_results}

            if images:
                um["images"] = images

            history.append({"userInputMessage": um})

        elif role == "assistant":
            arm: dict[str, Any] = {}
            text_parts = []
            tool_uses: list[dict[str, Any]] = []

            for block in content:
                block_type = block.get("type")

                if block_type == "text":
                    text_parts.append(block.get("text", ""))

                elif block_type == "tool_use":
                    tool_uses.append({
                        "toolUseId": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    })

            arm["content"] = "\n".join(text_parts) if text_parts else ""

            if tool_uses:
                arm["toolUses"] = tool_uses

            history.append({"assistantResponseMessage": arm})

    return history
