"""What a suppression comment covers, as CodeQL decides it, and the one way it is written here.

A finding is suppressed in the code, by a comment on the line above it that names its rule and
gives the reason: `// codeql[cs/rule-id] why` in C#, `# codeql[py/rule-id] why` in Python. Lint,
the review of a pull request and the dismissals on `master` all ask the code what such a comment
covers, and all of them need CodeQL's answer rather than one of their own, so it is given once,
here. Run as a script, this is the lint: it refuses every comment that is not the one spelling,
naming the file, the line and what to write instead.

**What CodeQL reads**, which is its alert-suppression query at the pinned version,
`AlertSuppression.qll` in `shared/util`, held to what that version does by
`tests/tools/SuppressionVectors.json`:

- `codeql[...]` anywhere in a comment standing alone on its line covers the next line.
- `lgtm[...]` anywhere in a comment covers the comment's own line, and the next as well when the
  comment stands alone.
- `lgtm` without a bracket, at the start of a comment or after a `;`, does the same for every rule.
- In Python, a comment that is `noqa` and nothing but words after it covers every rule on its own
  line. `noqa:` followed by ruff's codes is not one.
- In C#, a comment is `//`, `///`, or a `/* */` ending on the line it starts on. One spanning lines
  is never read.
- What stands in the bracket is one rule's identifier, with the spaces around it trimmed and its
  case exact: `CS/...` suppresses nothing, and neither does a list of two.

**Read by tree-sitter**, with its C# and Python grammars, so a `//` inside a C# string or a `#`
inside a Python one is never taken for a comment. It reads every branch of an `#if`, which covers
what CodeQL reads across the Debug and the Release build it extracts; a marker in a branch neither
build compiles is one CodeQL never applies, and the review reports it as suppressing nothing. A
grammar older than the code it reads shows as a parse error, and a file holding a marker that does
not parse cleanly is refused rather than read with a guess, so a grammar falling behind fails
here, loudly.

**The one spelling** lint accepts: one or more markers of one rule each, then the reason, in a `//`
or `#` comment of its own directly above the line it covers. Everything else CodeQL reads is
refused, and so is everything that looks like a suppression and is not one.
"""

from __future__ import annotations

import argparse
import itertools
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_c_sharp
import tree_sitter_python
from tree_sitter import Language, Node, Parser

#: The languages CodeQL reads suppressions in, by the extension of the file holding them.
LANGUAGES = {".cs": "csharp", ".py": "python"}

GRAMMARS = {
    "csharp": Language(tree_sitter_c_sharp.language()),
    "python": Language(tree_sitter_python.language()),
}

# CodeQL's own patterns, from `AlertSuppression.qll`, except one: CodeQL writes `(?<=^|;)`, which
# Python's `re` refuses as a look-behind of two widths, so the alternatives sit outside it.
CODEQL = re.compile(r"(?i)\bcodeql\s*\[[^\]]*\]")
LGTM = re.compile(r"(?i)\blgtm\s*\[[^\]]*\]")
BARE_LGTM = re.compile(r"(?i)(?:^|(?<=;))\s*lgtm(?!\B|\s*\[)")
NOQA = re.compile(r"(?i)\s*noqa\s*([^:].*)?")

#: The one way a marker is accepted: a rule as CodeQL names it, a language and lower case words.
CANONICAL = re.compile(r"codeql\[[a-z0-9]+(?:/[a-z0-9][a-z0-9_-]*)+\]")

#: Text that could make a comment a suppression, which decides whether a file is parsed at all.
CANDIDATE = re.compile(r"(?i)codeql|lgtm|noqa")


@dataclass(frozen=True)
class Comment:
    """One comment as the grammar found it, its text without the delimiter, lines counted from 1."""

    line: int
    end: int
    column: int
    delimiter: str
    text: str


@dataclass(frozen=True)
class Source:
    """What the grammar found in a file: its comments, the lines holding code, its parse errors."""

    text: str
    lines: int
    code: frozenset[int]
    comments: tuple[Comment, ...]
    errors: int


@dataclass(frozen=True)
class Marker:
    """One marker in a comment: its kind, its rule or `*` for all, and the lines CodeQL covers."""

    kind: str
    rule: str
    lines: tuple[int, ...]
    written: str


@dataclass(frozen=True)
class Suppression:
    """One comment holding a marker, what CodeQL makes of it, and what lint refuses it for."""

    path: str
    line: int
    column: int
    covered: int
    delimiter: str
    text: str
    reason: str
    markers: tuple[Marker, ...]
    problems: tuple[str, ...]


#: What `scan` gives for each file it read: the path from the root, the source, its suppressions.
Scanned = tuple[str, Source, list[Suppression]]


def language_of(path: Path | str) -> str | None:
    """The language CodeQL reads a file in, of the two that take suppressions, or `None`."""
    return LANGUAGES.get(Path(path).suffix.lower())


def _line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def _delimiter(raw: str) -> tuple[str, str]:
    if raw.startswith("///") and not raw.startswith("////"):
        return "///", raw[3:]
    if raw.startswith("//"):
        return "//", raw[2:]
    if raw.startswith("/*"):
        return "/*", raw[2:-2] if len(raw) >= 4 and raw.endswith("*/") else raw[2:]
    return "#", raw[1:]


def read(text: str, language: str) -> Source:
    """The comment nodes the language's grammar finds, and the lines any other leaf is on."""
    data = text.encode("utf-8")
    tree = Parser(GRAMMARS[language]).parse(data)
    rows = data.split(b"\n")
    comments: list[Comment] = []
    code: set[int] = set()
    errors = 0
    pending: list[Node] = [tree.root_node]
    while pending:
        node = pending.pop()
        if node.is_error or node.is_missing:
            errors += 1
        if node.type == "comment":
            raw = data[node.start_byte : node.end_byte].decode("utf-8").rstrip("\r")
            row, offset = node.start_point
            delimiter, body = _delimiter(raw)
            # The grammar counts columns in bytes, and the text before a comment in characters is
            # what decides whether it stands alone.
            column = len(rows[row][:offset].decode("utf-8", "replace"))
            comments.append(Comment(row + 1, node.end_point[0] + 1, column, delimiter, body))
            continue
        if node.child_count == 0:
            if node.end_byte > node.start_byte:
                first, (last, at) = node.start_point[0], node.end_point
                # A leaf ending at the start of a row ends on the row before it.
                if at == 0 and last > first:
                    last -= 1
                code.update(range(first + 1, last + 2))
            continue
        pending.extend(node.children)
    comments.sort(key=lambda c: (c.line, c.column))
    return Source(text, _line_count(text), frozenset(code), tuple(comments), errors)


def _stands_alone(source: Source, comment: Comment) -> bool:
    return source.text.split("\n")[comment.line - 1][: comment.column].strip() == ""


def _kind_of_line(source: Source, line: int, commented: set[int]) -> str:
    if line > source.lines:
        return "end"
    if line in source.code:
        return "code"
    if line in commented:
        return "comment"
    return "blank"


def _rule_in(written: str) -> str:
    return written[written.index("[") + 1 : -1].strip()


def _marker_problems(codeql: list[re.Match[str]]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for match in codeql:
        written, rule = match.group(0), _rule_in(match.group(0))
        if "," in rule:
            problems.append(f"`{written}` is a list, and a list in one marker suppresses nothing: "
                            "give each rule a marker of its own.")
        elif rule != rule.lower():
            problems.append(f"`{written}` names the rule in another case, and CodeQL matches an "
                            "identifier exactly, so it suppresses nothing: write it in lower case.")
        elif CANONICAL.fullmatch(written) is None:
            problems.append(f"`{written}` is not how a marker is written here: "
                            "`codeql[rule-id]`, with nothing else in the brackets.")
        elif rule in seen:
            problems.append(f"names `{rule}` twice.")
        seen.add(rule)
    return problems


def _placement_problems(source: Source, comment: Comment, read: bool, alone: bool,
                        commented: set[int]) -> list[str]:
    if comment.delimiter == "///":
        problems = ["is a documentation comment: write it with `//`."]
    elif comment.delimiter == "/*" and read:
        problems = ["is a `/* */` comment: write it with `//`."]
    else:
        problems = []

    if not read:
        problems.append("is in a comment of several lines, which CodeQL does not read as a "
                        "suppression.")
    elif not alone:
        problems.append("follows code on its line, which CodeQL does not read as a suppression: "
                        "put it on the line above.")
    else:
        kind = _kind_of_line(source, comment.end + 1, commented)
        if kind == "blank":
            problems.append("has a blank line under it, which is what it covers: put it directly "
                            "above the line it is for.")
        elif kind == "comment":
            problems.append("has a comment under it, which is what it covers: put it directly "
                            "above the code, with any other markers in the same comment.")
        elif kind == "end":
            problems.append("has nothing under it to cover.")
    return problems


def suppressions(source: Source, language: str, path: str = "") -> list[Suppression]:
    """Every comment CodeQL could take for a suppression, with what CodeQL makes of it.

    One for each comment holding a marker, whether or not CodeQL applies it, so that lint can refuse
    the ones it does not and the review can account for every one it does. A marker's `lines` are
    the lines CodeQL covers with it, none where CodeQL does not read the comment at all.
    """
    commented = {n for c in source.comments for n in range(c.line, c.end + 1)}
    found: list[Suppression] = []
    for comment in source.comments:
        text = comment.text
        codeql = list(CODEQL.finditer(text))
        lgtm = list(LGTM.finditer(text))
        bare = list(BARE_LGTM.finditer(text))
        noqa = language == "python" and NOQA.fullmatch(text) is not None
        if not (codeql or lgtm or bare or noqa):
            continue

        read = comment.delimiter != "/*" or comment.end == comment.line
        alone = _stands_alone(source, comment)
        under = comment.end + 1
        own = tuple(range(comment.line, comment.end + 1))
        reach = ((*own, under) if alone else own) if read else ()
        where = "its own line and the next" if alone else "its own line"
        markers: list[Marker] = []
        problems: list[str] = []

        for match in codeql:
            covered = (under,) if read and alone else ()
            markers.append(Marker("codeql", _rule_in(match.group(0)), covered, match.group(0)))
        for match in lgtm:
            written = match.group(0)
            markers.append(Marker("lgtm", _rule_in(written), reach, written))
            problems.append(f"`{written}` is refused, since CodeQL applies it to {where}: "
                            "write `codeql[...]` on the line above instead.")
        for match in bare:
            markers.append(Marker("lgtm", "*", reach, match.group(0).strip()))
            problems.append(f"CodeQL reads this `lgtm` as a suppression of every rule on {where}: "
                            "reword the comment.")
        if noqa:
            markers.append(Marker("noqa", "*", (comment.line,), text.strip()))
            problems.append("CodeQL reads a `noqa` without codes as a suppression of every rule "
                            "on its line: name the ruff codes after `noqa:`.")

        reason = ""
        if codeql:
            reason = text[codeql[-1].end() :].strip()
            if comment.delimiter == "/*":
                reason = reason.removesuffix("*/").strip()
            if not reason:
                problems.append("gives no reason: write after the markers why the finding stays.")
            problems.extend(_marker_problems(codeql))
            if text[: codeql[0].start()].strip():
                problems.append("has text before its markers: begin the comment with them, and "
                                "give the reason after.")
            if any(text[a.end() : b.start()].strip() for a, b in itertools.pairwise(codeql)):
                problems.append("has text between its markers: write the markers together, then "
                                "one reason for them.")
            problems.extend(_placement_problems(source, comment, read, alone, commented))

        found.append(Suppression(path, comment.line, comment.column, under, comment.delimiter,
                                 text, reason, tuple(markers), tuple(problems)))
    return found


def coverage(found: list[Suppression]) -> dict[int, set[str]]:
    """The rules CodeQL suppresses on each line, `*` standing for every rule."""
    lines: dict[int, set[str]] = {}
    for suppression in found:
        for marker in suppression.markers:
            for line in marker.lines:
                lines.setdefault(line, set()).add(marker.rule)
    return lines


def suppressed(lines: dict[int, set[str]], rule: str, line: int) -> bool:
    """Whether CodeQL suppresses a finding of `rule` on `line`, the identifier matched exactly."""
    rules = lines.get(line, set())
    return "*" in rules or rule in rules


def tracked(root: Path) -> list[Path]:
    """The C# and Python files git tracks, asked as `RepositoryFiles.ps1` asks it, in its order."""
    listed = subprocess.run(
        ["git", "-c", "core.quotePath=off", "ls-files", "--cached", "--", "*.cs", "*.py"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [root / name for name in listed.stdout.splitlines() if (root / name).is_file()]


def scan(root: Path, paths: list[Path]) -> list[Scanned]:
    """Every file among `paths` that could hold a marker, read, with the suppressions in it."""
    found: list[Scanned] = []
    for path in paths:
        language = language_of(path)
        if language is None:
            continue
        text = path.read_text(encoding="utf-8-sig")
        if CANDIDATE.search(text) is None:
            continue
        name = path.relative_to(root).as_posix()
        source = read(text, language)
        found.append((name, source, suppressions(source, language, name)))
    return found


def lint(scanned: list[Scanned]) -> list[str]:
    """What lint refuses among the files `scan` read, one line each, naming the file and line."""
    refused: list[str] = []
    for name, source, found in scanned:
        if found and source.errors:
            refused.append(f"{name}: holds a suppression comment and does not parse cleanly, so "
                           "its comments cannot be located with certainty: tree-sitter's grammar "
                           "may be older than the code. Update it, or write the construct it "
                           "stops at another way.")
            continue
        refused.extend(f"{name}:{s.line}: {problem}" for s in found for problem in s.problems)
    return refused


def main(argv: list[str] | None = None) -> int:
    """Lint the repository's suppression comments, and answer 1 when any is refused."""
    parser = argparse.ArgumentParser(description="Lint the suppression comments CodeQL reads.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="the repository root, the parent of `tools` by default")
    arguments = parser.parse_args(argv)
    root: Path = arguments.root.resolve()
    paths = tracked(root)
    scanned = scan(root, paths)
    refused = lint(scanned)
    for line in refused:
        print(f"  {line}")
    if refused:
        print(f"{len(refused)} suppression comment problem(s). The one spelling is under "
              "Conventions in CLAUDE.md.")
        return 1
    count = sum(len(found) for _, _, found in scanned)
    print(f"  {len(paths)} files, {count} suppression comment(s), none refused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
