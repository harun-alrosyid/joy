"""Terminal UI for Joy's streaming coding agent."""

import asyncio
import re
import sys
import time
from contextlib import aclosing
from datetime import datetime

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.filters import Always, Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, Dimension, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from joy.config import Settings
from joy.llm import ChangeEvent, ToolEvent, stream_reply


class DiffViewer:
    def __init__(self, changes: list) -> None:
        self.changes = changes
        self.focus = 0
        self.expanded = False
        self.scroll = 0
        self._cache = None

    def add_change(self, change) -> None:
        self.changes.append(change)
        self.invalidate()

    def invalidate(self) -> None:
        self._cache = None

    def focus_change(self, amount: int) -> None:
        if self.changes:
            self.focus = max(0, min(len(self.changes) - 1, self.focus + amount))
            self.scroll = 0
            self.invalidate()

    def toggle(self) -> None:
        if not self.changes:
            return
        self.toggle_all()

    def toggle_all(self) -> None:
        self.expanded = not self.expanded
        self.scroll = 0
        self.invalidate()

    def expand(self) -> None:
        if self.changes:
            self.expanded = True
            self.invalidate()

    def collapse(self) -> None:
        self.expanded = False
        self.scroll = 0
        self.invalidate()

    def scroll_by(self, amount: int) -> None:
        self.scroll = max(0, self.scroll + amount)
        self.invalidate()

    def lines(self, width: int, height: int) -> list[tuple[str, str]]:
        return [(style, text) for style, text, _ in self._rows(width, height)]

    def _rows(self, width: int, height: int, include_toggle: bool = True
              ) -> list[tuple[str, str, int | None]]:
        cache_key = (width, height, self.focus, self.expanded, self.scroll, include_toggle,
                     tuple((change.path, change.unified_diff) for change in self.changes))
        if self._cache and self._cache[0] == cache_key:
            return self._cache[1]
        output = [("bold", "File changes", None)]
        for index, change in enumerate(self.changes):
            style = "bold" if index == self.focus else ""
            suffix = f"  +{change.additions}  -{change.deletions}"
            available = max(1, width - len(suffix) - 4)
            operation = change.operation.title()
            path_width = max(1, available - len(operation) - 2)
            path = change.path if len(change.path) <= path_width else "..." + change.path[-path_width + 3:]
            output.append((style, f"• {operation} {path:<{path_width}}{suffix}", None))
            diff_lines = change.unified_diff.splitlines()
            expanded = self.expanded
            visible = diff_lines if expanded else self._preview(diff_lines)
            output.extend((line_style, line_text, None)
                          for line_style, line_text in self._format_diff(visible))
            if not expanded and len(self._diff_body(diff_lines)) > len(visible):
                output.append(("dim", "      ⋮", None))
        additions = sum(change.additions for change in self.changes)
        deletions = sum(change.deletions for change in self.changes)
        files = len(self.changes)
        output.append(("summary", f"{files} file{'s' if files != 1 else ''} changed  "
                                   f"+{additions}  -{deletions}", None))
        if include_toggle:
            output.append(("blue", "  − Hide details" if self.expanded else "  + Show details", -1))
        visible = max(1, height)
        content_height = visible
        self.scroll = max(0, min(self.scroll, max(0, len(output) - content_height)))
        result = output[self.scroll:self.scroll + content_height]
        self._cache = (cache_key, result)
        return result

    @staticmethod
    def _preview(lines: list[str]) -> list[str]:
        hunk = next((index for index, line in enumerate(lines) if line.startswith("@@")), None)
        if hunk is None:
            return []
        section = []
        for line in lines[hunk:]:
            if section and line.startswith("@@"):
                break
            section.append(line)
        preview = [section[0]]
        changes = 0
        for line in section[1:]:
            preview.append(line)
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                changes += 1
            if changes >= 3 and len(preview) >= 5:
                break
        return preview

    @staticmethod
    def _diff_body(lines: list[str]) -> list[str]:
        return [line for line in lines if not line.startswith(("---", "+++", "@@"))]

    @staticmethod
    def _format_diff(lines: list[str]) -> list[tuple[str, str]]:
        old_line = new_line = 0
        formatted = []
        for line in lines:
            if line.startswith("@@"):
                import re
                match = re.search(r"-(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?", line)
                if match:
                    old_line, new_line = map(int, match.groups())
                formatted.append(("cyan", "  " + line))
                continue
            if line.startswith("---") or line.startswith("+++"):
                continue
            if line.startswith("-"):
                formatted.append(("class:diff-remove", f"{old_line:>4}      {line}"))
                old_line += 1
            elif line.startswith("+"):
                formatted.append(("class:diff-add", f"     {new_line:>4} {line}"))
                new_line += 1
            else:
                formatted.append(("class:diff-context", f"{old_line:>4} {new_line:>4} {line}"))
                old_line += 1
                new_line += 1
        return formatted


def _viewer_text(state: dict, width: int, height: int,
                 include_toggle: bool = True) -> list[tuple]:
    viewer = state.get("viewer")
    if not viewer:
        return []
    def mouse_handler(index):
        def handle(mouse_event):
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                viewer.scroll_by(-3)
                get_app().invalidate()
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                viewer.scroll_by(3)
                get_app().invalidate()
                return None
            if index is not None and mouse_event.event_type == MouseEventType.MOUSE_UP:
                if index == -1:
                    viewer.toggle_all()
                else:
                    viewer.focus = index
                    viewer.toggle()
                get_app().invalidate()
                return None
        return handle

    content_height = max(1, height)
    # FormattedTextControl concatenates fragments; terminate each row explicitly.
    formatted = []
    for style, text, index in viewer._rows(width, content_height, include_toggle):
        handler = mouse_handler(index)
        counts = re.fullmatch(r"(.*?)(\s+\+\d+)(\s+-\d+)", text)
        if counts and style in {"bold", "", "summary"}:
            base_style = "bold" if style == "bold" else ""
            formatted.extend(((base_style, counts[1], handler),
                              ("class:diff-count-add", counts[2], handler),
                              ("class:diff-count-remove", counts[3] + "\n", handler)))
        else:
            style = f"class:{style}" if style in {"diff-add", "diff-remove", "diff-context"} else style
            formatted.append((style, text + "\n", handler))
    return formatted


def _show_changes(console: Console, changes: list) -> None:
    """Render compact, collapsed file summaries without consuming input."""
    if not changes:
        return
    console.print("[bold]File changes[/bold]")
    unique = list(dict((change.path, change) for change in changes).values())
    width = max(24, console.width)
    for change in unique:
        suffix = f"  +{change.additions}  -{change.deletions}"
        available = max(1, width - len(suffix) - 7)
        path = change.path if len(change.path) <= available else "..." + change.path[-available + 3:]
        console.print(f"  [dim]›[/dim] {path:<{available}}"
                      f"[green]+{change.additions}[/green]  [red]-{change.deletions}[/red]",
                      soft_wrap=False, overflow="ellipsis")
    additions = sum(change.additions for change in unique)
    deletions = sum(change.deletions for change in unique)
    console.print("[dim]─" * min(width, 40) + "[/dim]")
    files = len(unique)
    console.print(f"[dim]{files} file{'s' if files != 1 else ''} changed[/dim]"
                  f"  [green]+{additions}[/green]  [red]-{deletions}[/red]")
    if sys.stdin.isatty() and sys.stdout.isatty():
        console.print("[dim]Press F4 to view activity and changes.[/dim]")


def _tool_status(name: str, status: str, detail: str = "") -> str:
    labels = {"running": "●", "finished": "✓", "failed": "✗", "cancelled": "－"}
    verbs = {"read": "Reading", "write": "Writing", "edit": "Editing", "bash": "Running"}
    if status == "running":
        context = f" — {detail}" if detail else ""
        return f"{labels[status]} {verbs.get(name, name.title())}{context}"
    context = f" — {detail}" if detail else ""
    return f"{labels.get(status, '•')} {verbs.get(name, name.title())} {status}{context}"


def _work_elapsed(state: dict) -> float:
    elapsed = state.get("elapsed_seconds", 0.0)
    started = state.get("segment_started_at")
    return elapsed + (time.monotonic() - started if started is not None else 0.0)


def _show_keymap(console: Console) -> None:
    console.print("[bold]Keymap[/bold]")
    for key, action in (
        ("F4", "Focus Activity / File Changes"),
        ("Alt+M", "Toggle mouse controls / text selection"),
        ("Esc", "Interrupt turn; leave viewer focus"),
        ("Enter", "Submit; toggle all diff details"),
        ("Space", "Toggle all diff details"),
        ("Up / Down", "Navigate file changes"),
        ("Right / Left", "Expand / collapse all diffs"),
        ("PageUp / PageDown", "Scroll expanded diff"),
        ("Alt+Enter", "Insert newline"),
        ("Ctrl+D", "Exit on empty input"),
        ("/exit", "Exit Joy"),
        ("/keymap", "Show this menu"),
    ):
        console.print(f"  [cyan]{key:<18}[/cyan] {action}")


async def _consume(messages, settings, state, session):
    try:
        async with aclosing(stream_reply(messages, settings)) as stream:
            async for chunk in stream:
                if isinstance(chunk, ChangeEvent):
                    state["changes"].append(chunk.change)
                    state["viewer"].add_change(chunk.change)
                elif isinstance(chunk, ToolEvent):
                    state["tool"] = _tool_status(chunk.name, chunk.status, chunk.detail)
                else:
                    state["answer"] += chunk
                session.app.invalidate()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        state["error"] = str(error)
    finally:
        state["done"] = True
        if state.get("viewer") is not None:
            state["viewer"].invalidate()
        if state.get("work_started_at") is not None:
            elapsed = _work_elapsed(state)
            ended_at = datetime.now().strftime("%H:%M:%S")
            state["finished_status"] = (
                f"－ Work interrupted after {elapsed:.1f}s · {ended_at}"
                if state["cancelled"] else
                f"✗ Work failed after {elapsed:.1f}s · {ended_at}"
                if state["error"] else
                f"✓ Work finished in {elapsed:.1f}s · {ended_at}"
            )
        session.app.invalidate()
        if not state["cancelled"]:
            try:
                session.app.exit(result=session.default_buffer.text)
            except Exception:
                pass


async def _animate(session, state):
    while state["pending"]:
        session.app.invalidate()
        await asyncio.sleep(0.1)


def main() -> None:
    console = Console(color_system="truecolor" if sys.stdout.isatty() else None)
    try:
        settings = Settings.from_env()
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        return
    if not settings.api_key:
        console.print("[red]Set OPENAI_API_KEY (or JOY_API_KEY) before running joy.[/red]")
        return

    history = [{
        "role": "system",
        "content": "You are Joy, a concise coding agent. Use read, write, edit, and bash "
        "to perform the user's task in the working directory. Read files before editing; "
        "report tool results honestly. Do not expose credentials or modify unrelated files.",
    }]
    bindings = KeyBindings()
    state = {"pending": False, "answer": "", "tool": "", "error": "", "done": False,
             "cancelled": False, "changes": [], "viewer": None, "viewer_focus": False,
             "viewer_window": None,
             "work_started_at": None, "segment_started_at": None,
             "elapsed_seconds": 0.0, "finished_status": "", "mouse_enabled": True}
    active: asyncio.Task | None = None

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].toggle()
            event.app.invalidate()
        elif not state["pending"]:
            event.current_buffer.validate_and_handle()

    @bindings.add("space")
    def toggle_change_space(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].toggle()
            event.app.invalidate()
        else:
            event.current_buffer.insert_text(" ")

    @bindings.add("escape", "enter")
    def newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("<bracketed-paste>")
    def paste(event: KeyPressEvent) -> None:
        text = event.data.replace("\r\n", "\n").replace("\r", "\n")
        event.current_buffer.insert_text(text)
        event.current_buffer.cursor_position -= len(text) - len(text.rstrip("\n"))

    @bindings.add("f4")
    def focus_changes(event: KeyPressEvent) -> None:
        if state["changes"]:
            state["viewer_focus"] = True
            event.app.layout.focus(viewer_window)
            event.app.invalidate()

    @bindings.add("escape", "m")
    def toggle_mouse_mode(event: KeyPressEvent) -> None:
        state["mouse_enabled"] = not state["mouse_enabled"]
        event.app.invalidate()

    @bindings.add("escape")
    def leave_changes(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer_focus"] = False
            event.app.layout.focus(input_window)
            event.app.invalidate()
        elif state["pending"]:
            state["cancelled"] = True
            if active:
                active.cancel()
            state["pending"] = False
            event.app.invalidate()

    @bindings.add("q")
    def close_changes(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer_focus"] = False
            event.app.layout.focus(input_window)
            event.app.invalidate()
        else:
            event.current_buffer.insert_text("q")

    @bindings.add("up", filter=Condition(lambda: state["viewer_focus"]))
    def previous_change(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].focus_change(-1)
            event.app.invalidate()

    @bindings.add("down", filter=Condition(lambda: state["viewer_focus"]))
    def next_change(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].focus_change(1)
            event.app.invalidate()

    @bindings.add("right", filter=Condition(lambda: state["viewer_focus"]))
    def expand_change(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].expand()
            event.app.invalidate()

    @bindings.add("left", filter=Condition(lambda: state["viewer_focus"]))
    def collapse_change(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].collapse()
            event.app.invalidate()

    @bindings.add("pageup", filter=Condition(lambda: state["viewer_focus"]))
    def scroll_up(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].scroll = max(0, state["viewer"].scroll - 8)
            event.app.invalidate()

    @bindings.add("pagedown", filter=Condition(lambda: state["viewer_focus"]))
    def scroll_down(event: KeyPressEvent) -> None:
        if state["viewer_focus"]:
            state["viewer"].scroll += 8
            event.app.invalidate()

    session: PromptSession[str] = PromptSession(
        multiline=True, erase_when_done=True,
        placeholder="Ask Joy to do something…", prompt_continuation="  ",
        key_bindings=bindings, color_depth=ColorDepth.TRUE_COLOR,
        mouse_support=Condition(lambda: state["mouse_enabled"]),
        style=Style.from_dict({
            "frame.border": "#393939 bg:#393939",
            "prompt": "#ffffff bg:#393939",
            "placeholder": "#808080 bg:#393939",
            "diff-add": "#c8f7c5 bg:#164b25",
            "diff-remove": "#ffd0d0 bg:#5c2020",
            "diff-context": "#969696",
            "diff-count-add": "#00af00",
            "diff-count-remove": "#ff5f5f",
        }),
    )
    input_window = session.layout.current_window
    input_window.height = Dimension.exact(1)
    input_window.style = "#ffffff bg:#393939"
    input_window.dont_extend_height = Always()

    def status() -> list[tuple[str, str]]:
        if not state["pending"] and state["finished_status"]:
            return [("#808080", state["finished_status"])]
        elapsed = _work_elapsed(state)
        if state["tool"]:
            color = "#ff5555" if "✗" in state["tool"] else "#00afaf"
            display = state["tool"]
            if "●" in state["tool"]:
                tick = int(asyncio.get_running_loop().time() * 10)
                spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                display = spinner[tick % len(spinner)] + state["tool"][1:]
            return [(color, f"{display} · {elapsed:.1f}s  · Esc interrupt")]
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        index = int(asyncio.get_running_loop().time() * 10) % len(frames)
        return [("#808080", f"{frames[index]} Thinking… {elapsed:.1f}s")]

    input_container = Frame(session.layout.container).__pt_container__()
    def viewer_text() -> list[tuple]:
        render_info = state["viewer_window"].render_info
        height = render_info.window_height if render_info else 20
        return _viewer_text(state, session.output.get_size().columns, height,
                            include_toggle=False)

    def viewer_toggle_text() -> list[tuple]:
        viewer = state.get("viewer")
        if viewer is None:
            return []
        label = "  − Hide details" if viewer.expanded else "  + Show details"
        def toggle(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                viewer.toggle_all()
                get_app().invalidate()
        return [("blue", label, toggle)]

    viewer_window = Window(
        FormattedTextControl(viewer_text, focusable=True),
        height=Dimension(min=1, max=19), wrap_lines=True,
        dont_extend_height=True,
    )
    state["viewer_window"] = viewer_window
    viewer_container = HSplit([
        viewer_window,
        Window(FormattedTextControl(viewer_toggle_text),
               height=Dimension.exact(1), dont_extend_height=True),
    ])
    session.layout.container = HSplit([
        ConditionalContainer(
            Window(FormattedTextControl(lambda: [("", state["answer"])]),
                   wrap_lines=True, height=Dimension(max=1),
                   dont_extend_height=True),
            filter=Condition(lambda: state["pending"] and bool(state["answer"])),
        ),
        ConditionalContainer(viewer_container,
                              filter=Condition(lambda: state["done"] and bool(state["changes"])),
        ),
        ConditionalContainer(Window(FormattedTextControl(status), height=Dimension.exact(1)),
                              filter=Condition(lambda: state["pending"] or
                                               bool(state["finished_status"]))),
        input_container,
    ])

    console.print("[bold cyan]Joy[/bold cyan]")
    console.print("[dim]Enter sends · Alt+Enter adds a new line · Esc interrupts[/dim]")
    draft = ""
    while True:
        try:
            message = session.prompt("› ", default=draft) or ""
            draft = ""
        except KeyboardInterrupt:
            draft = ""
            continue
        except EOFError:
            break
        if message.strip() == "/exit":
            break
        if message.strip() == "/keymap":
            _show_keymap(console)
            continue
        if not message.strip():
            continue

        console.print(Padding(Text("› " + message, style="white"), (1, 1),
                              style="white on #393939"))
        state.update(pending=True, answer="", tool="", error="", done=False,
                     cancelled=False, changes=[], viewer_focus=False,
                     work_started_at=None,
                     segment_started_at=None, elapsed_seconds=0.0, finished_status="")
        state["viewer"] = DiffViewer([])
        messages = [*history, {"role": "user", "content": message}]
        session.app.invalidate()

        def start() -> None:
            nonlocal active
            state["work_started_at"] = time.monotonic()
            state["segment_started_at"] = state["work_started_at"]
            active = session.app.create_background_task(_consume(messages, settings, state, session))
            session.app.create_background_task(_animate(session, state))

        try:
            draft = session.prompt("› ", default=draft, pre_run=start) or ""
        except EOFError:
            state["cancelled"] = True
            if active:
                active.cancel()
            break
        except KeyboardInterrupt:
            state["cancelled"] = True
            if active:
                active.cancel()
            state["pending"] = False
            session.app.invalidate()
            console.print("[dim]Response cancelled.[/dim]")
            if state["answer"]:
                console.print(Padding(Markdown(state["answer"]), (0, 0, 0, 1)))
            continue
        finally:
            state["pending"] = False

        if state["answer"]:
            console.print("[bold green]Joy[/bold green]")
            console.print(Padding(Markdown(state["answer"]), (0, 0, 0, 1)))
            if not state["error"]:
                history.extend([{"role": "user", "content": message},
                                {"role": "assistant", "content": state["answer"]}])
        if state["error"]:
            console.print(f"[red]{state['error']}[/red]")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            _show_changes(console, state["changes"])
        if state["finished_status"] and not (sys.stdin.isatty() and sys.stdout.isatty()):
            console.print(f"[dim]{state['finished_status']}[/dim]")


if __name__ == "__main__":
    main()
