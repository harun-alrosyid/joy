"""Local coding tools. Bash runs with the current user's permissions."""

import os
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata
from contextlib import suppress
from difflib import unified_diff
from pathlib import Path
from threading import Event, Lock
from dataclasses import dataclass

from aisuite.utils.tools import Tools

OUTPUT_LIMIT = 20_000
MAX_FILE_SIZE = 2 * 1024 * 1024
# ponytail: serialize file mutations globally; use per-file locks if concurrency grows.
MUTATION_LOCK = Lock()


@dataclass(frozen=True)
class FileChange:
    path: str
    operation: str
    additions: int
    deletions: int
    unified_diff: str


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fuzzy(text: str) -> str:
    # Matching rules adapted from Pi's coding-agent/src/core/tools/edit-diff.ts.
    text = "\n".join(line.rstrip() for line in unicodedata.normalize("NFKC", text).split("\n"))
    substitutions = {char: replacement for chars, replacement in (
        ("‘’‚‛", "'"), ("“”„‟", '"'), ("‐‑‒–—―−", "-"),
        ("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " "),
    ) for char in chars}
    return text.translate(str.maketrans(substitutions))


def _replace(original: str, old_text: str, new_text: str) -> str:
    bom = "\ufeff" if original.startswith("\ufeff") else ""
    body = original[len(bom):]
    first_lf = body.find("\n")
    ending = "\r\n" if first_lf > 0 and body[first_lf - 1] == "\r" else "\n"
    body, old_text, new_text = map(_lf, (body, old_text, new_text))
    fuzzy_body, fuzzy_old = _fuzzy(body), _fuzzy(old_text)
    if not old_text or not fuzzy_old:
        raise ValueError("old_text must contain non-whitespace text.")
    if fuzzy_body.count(fuzzy_old) > 1:
        raise ValueError("old_text matches multiple places. Include more surrounding context.")
    if old_text in body:
        updated = body.replace(old_text, new_text, 1)
    else:
        start = fuzzy_body.find(fuzzy_old)
        if start < 0:
            raise ValueError("old_text was not found. Read the file and retry.")
        end = start + len(fuzzy_old)
        # Rebuild only touched lines so normalization cannot alter unrelated code.
        first = fuzzy_body.count("\n", 0, start)
        last = fuzzy_body.count("\n", 0, end - 1)
        line_start = fuzzy_body.rfind("\n", 0, start) + 1
        line_end = fuzzy_body.find("\n", end - 1)
        line_end = len(fuzzy_body) if line_end < 0 else line_end + 1
        lines = body.split("\n")
        prefix = "\n".join(lines[:first]) + ("\n" if first else "")
        suffix = "\n".join(lines[last + 1:])
        updated = prefix + fuzzy_body[line_start:start] + new_text + fuzzy_body[end:line_end] + suffix
    if updated == body:
        raise ValueError("No changes: the replacement is identical to the original.")
    return bom + updated.replace("\n", ending)


def _clip(text: str) -> str:
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + "\n[output truncated]"


def build_file_change(path: str, operation: str, original: str, updated: str) -> FileChange:
    """Build a complete unified diff and count only added/removed body lines."""
    if original == updated:
        raise ValueError("No changes to record.")
    diff = "".join(unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))
    return FileChange(
        path, operation,
        sum(line.startswith("+") and not line.startswith("+++") for line in diff.splitlines()),
        sum(line.startswith("-") and not line.startswith("---") for line in diff.splitlines()),
        diff,
    )


def _save(path: Path, content: str, cancelled: Event | None = None) -> None:
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_SIZE:
        raise ValueError("File content exceeds the 2 MiB limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        if cancelled and cancelled.is_set():
            raise InterruptedError("Cancelled.")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create_tools(workspace: Path, cancelled: Event, on_change=None) -> Tools:
    root = workspace.resolve()

    def resolve(path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string.")
        try:
            target = (root / path).resolve()
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Cannot resolve path: {error}") from error
        if not target.is_relative_to(root):
            raise ValueError("Path must stay inside the workspace.")
        if cancelled.is_set():
            raise InterruptedError("Cancelled.")
        return target

    def read(path: str, start_line: int = 1, end_line: int = 0,
             offset: int = 0, limit: int = 0) -> str:
        """Read a UTF-8 file with line numbers; offset is 1-based, limit is at most 2000."""
        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = 0
        if offset is None:
            offset = 0
        if limit is None:
            limit = 0
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   for value in (start_line, end_line, offset, limit)):
            raise ValueError("Line range values must be integers.")
        if offset < 0 or limit < 0 or limit > 2000:
            raise ValueError("offset must be positive when set; limit must be between 1 and 2000.")
        if offset:
            start_line = offset
        if not end_line and limit:
            end_line = start_line + limit - 1
        if start_line < 1 or (end_line and end_line < start_line):
            raise ValueError("start_line must be positive and end_line must not precede it.")
        target = resolve(path)
        try:
            if not stat.S_ISREG(target.stat().st_mode):
                raise ValueError("Path is not a regular file.")
        except OSError as error:
            raise ValueError(f"Cannot access file: {error.strerror or error}") from error
        try:
            with target.open("rb") as file:
                data = file.read(MAX_FILE_SIZE + 1)
            if len(data) > MAX_FILE_SIZE:
                raise ValueError("File exceeds the 2 MiB limit. Use bash to inspect a section.")
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ValueError("File is not valid UTF-8 text.") from error
        except OSError as error:
            raise ValueError(f"Cannot read file: {error.strerror or error}") from error
        if cancelled.is_set():
            raise InterruptedError("Cancelled.")
        if lines:
            start_line = min(start_line, len(lines) + 1)
            if end_line:
                end_line = min(end_line, len(lines))
        else:
            start_line, end_line = 1, 0
        selected = lines[start_line - 1:] if not end_line else lines[start_line - 1:end_line]
        actual_end = start_line + len(selected) - 1
        heading = f"File: {path}\nLines: {start_line if selected else 0}-{actual_end if selected else 0}\n"
        body = "\n".join(f"{index:>3} | {line}" for index, line in
                          enumerate(selected, start=start_line))
        return _clip(heading + (f"\n{body}" if body else ""))

    def write(path: str, content: str) -> str:
        """Create or overwrite a UTF-8 file, creating parent directories if needed."""
        if not isinstance(content, str):
            raise ValueError("content must be a string.")
        if len(content.encode("utf-8")) > MAX_FILE_SIZE:
            raise ValueError("File content exceeds the 2 MiB limit.")
        with MUTATION_LOCK:
            target = resolve(path)
            if target.exists() and target.is_dir():
                raise ValueError("Path is a directory.")
            existed = target.exists()
            original_bytes = target.read_bytes() if existed else None
            original = original_bytes.decode("utf-8") if original_bytes is not None else ""
            change = build_file_change(path, "overwritten" if existed else "created", original, content)
            if cancelled.is_set():
                raise InterruptedError("Cancelled.")
            _save(target, content, cancelled)
        if on_change and change.additions + change.deletions:
            on_change(change)
        return _clip(f"Wrote {path}.\n{change.unified_diff}")

    def edit(path: str, old_text: str, new_text: str) -> str:
        """Replace unique old_text with new_text. Exact match first, then tolerate trailing
        whitespace and Unicode quote/dash/space differences. Include context for uniqueness.
        Preserves BOM and newline style; returns a unified diff. Read the file first.
        """
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ValueError("old_text and new_text must be strings.")
        if not old_text.strip():
            raise ValueError("old_text must contain non-whitespace text.")
        with MUTATION_LOCK:
            target = resolve(path)
            if not target.is_file():
                raise ValueError("Path is not an existing file.")
            try:
                if target.stat().st_size > MAX_FILE_SIZE:
                    raise ValueError("File exceeds the 2 MiB limit.")
                original_bytes = target.read_bytes()
                original = original_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("File is not valid UTF-8 text.") from error
            except OSError as error:
                raise ValueError(f"Cannot read file: {error.strerror or error}") from error
            if len(original_bytes) > MAX_FILE_SIZE:
                raise ValueError("File exceeds the 2 MiB limit.")
            updated = _replace(original, old_text, new_text)
            if len(updated.encode("utf-8")) > MAX_FILE_SIZE:
                raise ValueError("Updated file exceeds the 2 MiB limit.")
            if cancelled.is_set():
                raise InterruptedError("Cancelled.")
            try:
                if target.read_bytes() != original_bytes:
                    raise ValueError("File changed during editing. Read it and retry.")
            except OSError as error:
                raise ValueError(f"Cannot verify file before editing: {error.strerror or error}") from error
            change = build_file_change(path, "edited", original, updated)
            _save(target, updated, cancelled)
        if on_change:
            on_change(change)
        return _clip(f"Edited {path}.\n{change.unified_diff}")

    def bash(command: str, timeout: int = 30) -> str:
        """Run a noninteractive Bash command in the workspace; timeout is 1–300 seconds."""
        if not isinstance(command, str) or not command.strip() or not 1 <= timeout <= 300:
            raise ValueError("Provide a command and a timeout between 1 and 300 seconds.")
        if cancelled.is_set():
            raise InterruptedError("Cancelled.")
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    ["bash", "-c", command], cwd=root, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                )
            except OSError as error:
                raise ValueError(f"Could not start command: {error.strerror or error}") from error
            reason = "Completed"
            while process.poll() is None:
                if cancelled.wait(0.05):
                    reason = "Cancelled"
                    break
                if time.monotonic() - started >= timeout:
                    reason = "Timed out"
                    break
            if process.poll() is None:
                # Kill the process group, including children holding output files open.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            stdout.seek(0)
            stderr.seek(0)
            out = stdout.read(OUTPUT_LIMIT + 1).decode("utf-8", errors="replace")
            err = stderr.read(OUTPUT_LIMIT + 1).decode("utf-8", errors="replace")
        duration = time.monotonic() - started
        if reason == "Completed" and process.returncode:
            reason = "Failed"
        result = f"{reason}\nexit code {process.returncode}\nDuration: {duration:.1f}s"
        if out:
            result += f"\n\nstdout:\n{_clip(out)}"
        if err:
            result += f"\n\nstderr:\n{_clip(err)}"
        return result

    return Tools([read, write, edit, bash])
