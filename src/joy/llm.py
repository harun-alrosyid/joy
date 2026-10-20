"""Streaming replies over OpenAI Responses or Chat Completions, without blocking input."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Event, Thread

from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError

from joy.config import Settings
from joy.tools import FileChange, create_tools

MAX_STEPS = 16
MAX_TOOL_CALLS = 8


@dataclass(frozen=True)
class ToolEvent:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ChangeEvent:
    change: FileChange


def _error_message(error: Exception, model: str) -> str:
    # Preserve useful diagnostics from wrapped errors without printing raw messages.
    seen = set()
    while id(error) not in seen:
        seen.add(id(error))
        cause = error.__cause__ or error.__context__
        if cause is None or isinstance(error, (APIStatusError, APIConnectionError)):
            break
        error = cause
    if isinstance(error, APIStatusError):
        hints = {
            400: "The API rejected the request parameters.",
            401: "The API rejected your API key.",
            403: "Your account lacks permission for this request.",
            404: f"Check access to {model} and your API base URL.",
            429: "Rate limit or quota exceeded; check API usage and billing.",
            500: "The provider returned an internal error. Try again.",
            502: "The provider is temporarily unavailable. Try again or pick another model.",
            503: "The provider is unavailable or out of capacity. Try again or pick another model.",
            504: "The provider timed out upstream. Try again or pick another model.",
        }
        details = []
        for key in ("code", "param"):
            value = getattr(error, key, None)
            # Show identifiers only; raw provider messages can contain credentials.
            if isinstance(value, str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9.\[\]]{0,100}", value):
                details.append(f"{key}={value}")
        return f"HTTP {error.status_code}: {hints.get(error.status_code, 'API request failed.')} " + " ".join(details)
    if isinstance(error, APITimeoutError):
        return "The API request timed out. Slow models may need a larger JOY_TIMEOUT."
    if isinstance(error, APIConnectionError):
        return "Could not connect to the API. Check your connection and API base URL."
    if isinstance(error, ValueError) and str(error) in {
        "Empty or incomplete response", "Incomplete tool call",
    }:
        return str(error) + "."
    return f"Local error: {type(error).__name__}. Check the installed dependencies and configuration."


def _tool_output(toolbox, name: str, arguments: str) -> tuple[str, str]:
    try:
        output = toolbox.execute({"function": {"name": name, "arguments": arguments}})[0]
        return output, "finished"
    except InterruptedError as error:
        return str(error), "cancelled"
    except Exception as error:
        return f"Tool error: {error}", "failed"


def _tool_event_detail(output: str, status: str) -> str:
    if status != "failed":
        return ""
    message = output.partition("\n")[0]
    return re.sub(r"[\x00-\x1f\x7f]+", " ", message)[:180]


def _tool_detail(name: str, arguments: str) -> str:
    if name != "bash":
        return ""
    try:
        command = json.loads(arguments).get("command", "").strip().replace("\n", " ")
    except (TypeError, ValueError, AttributeError):
        return ""
    return command[:100] + ("..." if len(command) > 100 else "")


def _run_responses(client: OpenAI, settings: Settings, conversation: list, toolbox,
                   cancelled: Event, result: Queue) -> None:
    for _ in range(MAX_STEPS):
        if cancelled.is_set():
            return
        text = ""
        completed = None
        with client.responses.create(
            model=settings.model, input=conversation, stream=True,
            reasoning={"effort": "medium"}, store=False,
            include=["reasoning.encrypted_content"],
            tools=[{"type": "function", **tool["function"], "strict": False}
                   for tool in toolbox.tools()],
            parallel_tool_calls=False,
        ) as stream:
            for event in stream:
                if cancelled.is_set():
                    return
                if event.type in ("response.output_text.delta", "response.refusal.delta"):
                    text += event.delta
                    result.put(event.delta)
                elif event.type == "response.completed":
                    completed = event.response
                elif event.type in ("response.failed", "response.incomplete", "error"):
                    raise ValueError("Empty or incomplete response")
        if completed is None or completed.status != "completed":
            raise ValueError("Empty or incomplete response")
        calls = [item for item in completed.output if item.type == "function_call"]
        if not calls:
            if not text.strip():
                raise ValueError("Empty or incomplete response")
            return
        if len(calls) > MAX_TOOL_CALLS or any(not call.call_id for call in calls):
            raise ValueError("Incomplete tool call")
        # Replay all output, including encrypted reasoning, before tool results.
        conversation.extend(item.model_dump(exclude_none=True) for item in completed.output)
        for call in calls:
            if cancelled.is_set():
                return
            result.put(ToolEvent(call.name, "running", _tool_detail(call.name, call.arguments)))
            output, status = _tool_output(toolbox, call.name, call.arguments)
            result.put(ToolEvent(call.name, status, _tool_event_detail(output, status)))
            conversation.append({"type": "function_call_output", "call_id": call.call_id,
                                 "output": json.dumps(output)})
        if text:
            result.put("\n\n")
    result.put(RuntimeError(f"Reached the {MAX_STEPS}-step limit. Ask Joy to continue."))


def _run_chat(client: OpenAI, settings: Settings, conversation: list, toolbox,
              cancelled: Event, result: Queue) -> None:
    tools = [{"type": "function", "function": tool["function"]} for tool in toolbox.tools()]
    for _ in range(MAX_STEPS):
        if cancelled.is_set():
            return
        text = ""
        finish = None
        calls: dict[int, dict[str, str]] = {}
        with client.chat.completions.create(
            model=settings.model, messages=conversation, tools=tools,
            stream=True, parallel_tool_calls=False,
        ) as stream:
            for chunk in stream:
                if cancelled.is_set():
                    return
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.delta.content:
                    text += choice.delta.content
                    result.put(choice.delta.content)
                for piece in choice.delta.tool_calls or []:
                    entry = calls.setdefault(piece.index, {"id": "", "name": "", "arguments": ""})
                    if piece.id:
                        entry["id"] = piece.id
                    if piece.function is not None:
                        if piece.function.name:
                            entry["name"] = piece.function.name
                        if piece.function.arguments:
                            entry["arguments"] += piece.function.arguments
                if choice.finish_reason:
                    finish = choice.finish_reason
        if finish == "tool_calls":
            ordered = [calls[index] for index in sorted(calls)]
            if not ordered or len(ordered) > MAX_TOOL_CALLS:
                raise ValueError("Incomplete tool call")
            if any(not call["id"] or not call["name"] for call in ordered):
                raise ValueError("Incomplete tool call")
            conversation.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": call["id"], "type": "function",
                     "function": {"name": call["name"], "arguments": call["arguments"]}}
                    for call in ordered
                ],
            })
            for call in ordered:
                if cancelled.is_set():
                    return
                result.put(ToolEvent(call["name"], "running",
                                     _tool_detail(call["name"], call["arguments"])))
                output, status = _tool_output(toolbox, call["name"], call["arguments"])
                result.put(ToolEvent(call["name"], status, _tool_event_detail(output, status)))
                conversation.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(output)})
            if text:
                result.put("\n\n")
            continue
        if not text.strip():
            raise ValueError("Empty or incomplete response")
        return
    result.put(RuntimeError(f"Reached the {MAX_STEPS}-step limit. Ask Joy to continue."))


def _request(settings: Settings, messages: list, cancelled: Event, result: Queue) -> None:
    client = None
    try:
        client = OpenAI(**settings.client_kwargs())
        toolbox = create_tools(Path.cwd(), cancelled,
                               lambda change: result.put(ChangeEvent(change)))
        conversation = list(messages)
        runner = _run_responses if settings.is_responses else _run_chat
        runner(client, settings, conversation, toolbox, cancelled, result)
    except Exception as error:
        result.put(RuntimeError(
            "The model API could not return a reply. " + _error_message(error, settings.model)
        ))
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()
        result.put(None)


async def stream_reply(messages: list[dict], settings: Settings | None = None) -> AsyncIterator[str | ToolEvent | ChangeEvent]:
    settings = settings or Settings.from_env()
    result: Queue[str | ToolEvent | ChangeEvent | Exception | None] = Queue()
    cancelled = Event()

    # ponytail: cancelling discards the result; the sync request ends at its timeout.
    # Use a cancellable async provider if server-request cancellation is needed.
    Thread(target=_request, args=(settings, messages, cancelled, result), daemon=True).start()
    try:
        while True:
            while result.empty():
                await asyncio.sleep(0.05)
            chunk = result.get_nowait()
            if chunk is None:
                break
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk
    finally:
        cancelled.set()
