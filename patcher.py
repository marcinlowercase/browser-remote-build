import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class PatchOp:
    kind: str  # "replace", "overwrite", "create", "append"
    target: str
    old_code: str = ""
    new_code: str = ""


def _strip_fence_newlines(text: str) -> str:
    if text.startswith("\r\n"):
        text = text[2:]
    elif text.startswith("\n"):
        text = text[1:]
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return text


def _parse_plain_text_response(text: str) -> List[PatchOp]:
    ops: List[PatchOp] = []
    lines = text.split("\n")

    change_re = re.compile(
        r"^\s*in\s+`?([^\n,`]+?)`?(?:\s*\([^\n)]*\))?\s*,\s*change\s+[^\n:]*this:\s*$",
        re.IGNORECASE,
    )
    to_this_re = re.compile(r"^\s*to\s+this:\s*$", re.IGNORECASE)
    overwrite_re = re.compile(
        r"^\s*in\s+`?([^\n,`]+?)`?(?:\s*\([^\n)]*\))?\s*,\s*replace(?:\s+the)?\s+file(?:\s+contents)?\s+with:\s*$",
        re.IGNORECASE,
    )
    create_re = re.compile(
        r"^\s*(?:add\s+new\s+file|create(?:\s+new)?\s+file)\s+`?([^\n:`]+?)`?\s*:\s*$",
        re.IGNORECASE,
    )
    append_re = re.compile(
        r"^\s*in\s+`?([^\n,`]+?)`?(?:\s*\([^\n)]*\))?\s*,\s*add\s+at\s+the\s+bottom(?:\s+of\s+the\s+file)?\s*:\s*$",
        re.IGNORECASE,
    )
    commit_re = re.compile(r"^\s*commit\s+message:\s*(.*)$", re.IGNORECASE)

    current_kind: Optional[str] = None
    current_target: str = ""
    old_buf: List[str] = []
    new_buf: List[str] = []
    reading_phase: Optional[str] = None  # "old" or "new"

    def flush_op():
        nonlocal current_kind, current_target, old_buf, new_buf, reading_phase
        if current_kind == "replace" and reading_phase == "new":
            old_text = "\n".join(old_buf).strip("\n")
            new_text = "\n".join(new_buf).strip("\n")
            if old_text:
                ops.append(PatchOp(kind="replace", target=current_target, old_code=old_text, new_code=new_text))
        elif current_kind in ("overwrite", "create", "append") and reading_phase == "new":
            new_text = "\n".join(new_buf).strip("\n")
            if new_text:
                ops.append(PatchOp(kind=current_kind, target=current_target, new_code=new_text))
        current_kind = None
        current_target = ""
        old_buf = []
        new_buf = []
        reading_phase = None

    for line in lines:
        if commit_re.match(line):
            flush_op()
            break

        m = change_re.match(line)
        if m:
            flush_op()
            current_kind = "replace"
            current_target = m.group(1).strip()
            reading_phase = "old"
            continue

        m = overwrite_re.match(line)
        if m:
            flush_op()
            current_kind = "overwrite"
            current_target = m.group(1).strip()
            reading_phase = "new"
            continue

        m = create_re.match(line)
        if m:
            flush_op()
            current_kind = "create"
            current_target = m.group(1).strip()
            reading_phase = "new"
            continue

        m = append_re.match(line)
        if m:
            flush_op()
            current_kind = "append"
            current_target = m.group(1).strip()
            reading_phase = "new"
            continue

        if current_kind == "replace" and reading_phase == "old" and to_this_re.match(line):
            reading_phase = "new"
            continue

        if reading_phase == "old":
            old_buf.append(line)
        elif reading_phase == "new":
            new_buf.append(line)

    flush_op()
    return ops


def parse_ai_response(raw_text: str) -> Tuple[List[PatchOp], str]:
    text = raw_text.replace("\r\n", "\n")
    ops: List[PatchOp] = []

    if "```" in text:
        fence = r"```[a-zA-Z0-9_-]*\n(.*?)\n```"

        # match: in <file> ..., change this: ```...``` to this: ```...```
        change_pattern = re.compile(
            rf"(?:^|\n)\s*in\s+`?([^\n,`]+?)`?(?:\s*\([^\n)]*\))?\s*,\s*change\s+[^\n:]*this:\s*\n\s*{fence}\s*\n\s*to\s+this:\s*\n\s*{fence}",
            re.DOTALL | re.IGNORECASE,
        )

        # match: in <file>, replace [the] file [contents] with: ```...```
        replace_file_pattern = re.compile(
            rf"(?:^|\n)\s*in\s+`?([^\n,`]+?)`?(?:\s*\([^\n)]*\))?\s*,\s*replace(?:\s+the)?\s+file(?:\s+contents)?\s+with:\s*\n\s*{fence}",
            re.DOTALL | re.IGNORECASE,
        )

        # match: add new file <path>: ```...``` or create [new] file <path>: ```...```
        create_file_pattern = re.compile(
            rf"(?:^|\n)\s*(?:add\s+new\s+file|create(?:\s+new)?\s+file)\s+`?([^\n:`]+?)`?\s*:\s*\n\s*{fence}",
            re.DOTALL | re.IGNORECASE,
        )

        # match: in <file>, add at the bottom [of the file]: ```...```
        append_file_pattern = re.compile(
            rf"(?:^|\n)\s*in\s+`?([^\n,`]+?)`?(?:\s*\([^\n)]*\))?\s*,\s*add\s+at\s+the\s+bottom(?:\s+of\s+the\s+file)?\s*:\s*\n\s*{fence}",
            re.DOTALL | re.IGNORECASE,
        )

        matches: List[Tuple[int, PatchOp]] = []

        for m in change_pattern.finditer(text):
            matches.append((
                m.start(),
                PatchOp(
                    kind="replace",
                    target=m.group(1).strip(),
                    old_code=_strip_fence_newlines(m.group(2)),
                    new_code=_strip_fence_newlines(m.group(3)),
                ),
            ))

        for m in replace_file_pattern.finditer(text):
            matches.append((
                m.start(),
                PatchOp(
                    kind="overwrite",
                    target=m.group(1).strip(),
                    new_code=_strip_fence_newlines(m.group(2)),
                ),
            ))

        for m in create_file_pattern.finditer(text):
            matches.append((
                m.start(),
                PatchOp(
                    kind="create",
                    target=m.group(1).strip(),
                    new_code=_strip_fence_newlines(m.group(2)),
                ),
            ))

        for m in append_file_pattern.finditer(text):
            matches.append((
                m.start(),
                PatchOp(
                    kind="append",
                    target=m.group(1).strip(),
                    new_code=_strip_fence_newlines(m.group(2)),
                ),
            ))

        matches.sort(key=lambda item: item[0])
        ops = [op for _, op in matches]

    if not ops:
        ops = _parse_plain_text_response(text)

    commit_match = re.search(
        r"(?:^|\n)\s*commit\s+message:\s*(?:`([^`\n]+)`|([^\n]+)|\n\s*(?:`([^`\n]+)`|([^\n]+)))",
        text,
        re.IGNORECASE,
    )
    commit_msg = ""
    if commit_match:
        commit_msg = (
            commit_match.group(1)
            or commit_match.group(2)
            or commit_match.group(3)
            or commit_match.group(4)
            or ""
        ).strip()
    if not commit_msg:
        commit_msg = "chore: apply remote ai patch"

    return ops, commit_msg


def resolve_repo_file(repo_dir: str, target: str) -> Tuple[Optional[str], Optional[str]]:
    clean = target.strip().lstrip("./")
    direct = os.path.join(repo_dir, clean)
    if os.path.isfile(direct):
        return direct, None

    skip_dirs = {".git", "build", ".gradle", ".idea"}
    candidates: List[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, repo_dir).replace(os.sep, "/")
            if "/" in clean:
                if rel == clean or rel.endswith("/" + clean):
                    candidates.append(full)
            elif fname == clean:
                candidates.append(full)

    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) == 0:
        return None, f"file not found in repo: {target}"

    # disambiguate common files like build.gradle.kts in app/
    app_candidates = [c for c in candidates if "/app/" in c.replace(os.sep, "/")]
    if len(app_candidates) == 1:
        return app_candidates[0], None

    rels = [os.path.relpath(c, repo_dir) for c in candidates]
    return None, f"ambiguous filename '{target}', matched: {', '.join(rels)}"


def _replace_normalized_lines(content: str, old_code: str, new_code: str) -> Optional[str]:
    file_lines = content.splitlines(keepends=True)
    old_lines = old_code.splitlines()
    if not old_lines:
        return None

    # try right-stripped line match first, then full stripped line match
    for mode in ("rstrip", "strip"):
        norm_fn = (lambda s: s.rstrip()) if mode == "rstrip" else (lambda s: s.strip())
        target_norm = [norm_fn(line) for line in old_lines]
        file_norm = [norm_fn(line.rstrip("\r\n")) for line in file_lines]

        found_indices: List[int] = []
        window = len(target_norm)
        for idx in range(0, len(file_norm) - window + 1):
            if file_norm[idx : idx + window] == target_norm:
                found_indices.append(idx)

        if len(found_indices) == 1:
            start = found_indices[0]
            end = start + window
            replacement = new_code + ("\n" if new_code and file_lines[end - 1].endswith("\n") else "")
            return "".join(file_lines[:start]) + replacement + "".join(file_lines[end:])

    return None


def _find_closest_snippet(content: str, old_code: str) -> str:
    old_lines = [l.strip() for l in old_code.splitlines() if l.strip()]
    if not old_lines:
        return ""
    anchor = old_lines[0]
    file_lines = content.splitlines()
    for i, line in enumerate(file_lines):
        if anchor in line:
            lo = max(0, i - 2)
            hi = min(len(file_lines), i + len(old_lines) + 4)
            numbered = [f"{n + 1}: {file_lines[n]}" for n in range(lo, hi)]
            return "\n".join(numbered)
    return ""


def apply_ai_response(repo_dir: str, raw_text: str) -> Tuple[bool, str, str, List[str]]:
    ops, commit_msg = parse_ai_response(raw_text)
    if not ops:
        return (
            False,
            "no valid patch blocks found.\nmake sure blocks use:\nin `FileName.kt`, change this:\n```kotlin\n...\n```\nto this:\n```kotlin\n...\n```",
            commit_msg,
            [],
        )

    buffers = {}
    touched_files: List[str] = []

    for idx, op in enumerate(ops, start=1):
        if op.kind == "create":
            clean = op.target.strip().lstrip("./")
            if "/" not in clean:
                return (
                    False,
                    f"block #{idx}: 'add new file' needs a path (got '{op.target}')",
                    commit_msg,
                    [],
                )
            full_path = os.path.join(repo_dir, clean)
            buffers[full_path] = op.new_code + "\n"
            if full_path not in touched_files:
                touched_files.append(full_path)
            continue

        full_path, err = resolve_repo_file(repo_dir, op.target)
        if err or full_path is None:
            return False, f"block #{idx} ({op.target}): {err}", commit_msg, []

        if full_path not in buffers:
            with open(full_path, "r", encoding="utf-8") as f:
                buffers[full_path] = f.read()

        current = buffers[full_path]

        if op.kind == "overwrite":
            buffers[full_path] = op.new_code + "\n"
        elif op.kind == "append":
            sep = "" if (not current or current.endswith("\n")) else "\n"
            buffers[full_path] = current + sep + "\n" + op.new_code + "\n"
        elif op.kind == "replace":
            count = current.count(op.old_code)
            if count == 1:
                buffers[full_path] = current.replace(op.old_code, op.new_code, 1)
            elif count > 1:
                return (
                    False,
                    f"block #{idx} in {os.path.relpath(full_path, repo_dir)} matched {count} times; provide more surrounding context lines.",
                    commit_msg,
                    [],
                )
            else:
                fallback = _replace_normalized_lines(current, op.old_code, op.new_code)
                if fallback is None:
                    hint = _find_closest_snippet(current, op.old_code)
                    rel = os.path.relpath(full_path, repo_dir)
                    msg = f"patch block #{idx} failed to match in {rel}.\n\nexpected old block:\n```\n{op.old_code}\n```"
                    if hint:
                        msg += f"\n\ncurrent file around anchor:\n```\n{hint}\n```"
                    return False, msg, commit_msg, []
                buffers[full_path] = fallback

        if full_path not in touched_files:
            touched_files.append(full_path)

    # all blocks matched cleanly: flush changes to disk atomically
    for path, new_content in buffers.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

    rel_touched = [os.path.relpath(p, repo_dir) for p in touched_files]
    return True, f"applied {len(ops)} block(s) across {len(rel_touched)} file(s)", commit_msg, rel_touched
