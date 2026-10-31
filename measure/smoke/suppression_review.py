"""The review of every suppression a pull request touches, and the list of what the code suppresses.

A suppression is a comment in the code, so it arrives the way any code does, in a pull request, and
this makes sure nobody merges one unseen. It runs in the CodeQL workflow's last job, after the
analysis, with the files CodeQL wrote:

- **Every suppression a pull request adds, changes or removes gets a review thread**, which the
  ruleset makes a merge wait for, over the comment, the three lines above it and, below it, at
  least three and down to the last line CodeQL flagged, cut to the file and to the hunk GitHub
  shows, so that the code it suppresses is in view. An added or changed one names the rule, its
  title, the severity and the reason, with CodeQL's own message when it adds to the title, and the
  code CodeQL flagged when that runs past the lines in view; a removed one covers the lines it was
  taken from.
- **One comment on the pull request lists what it suppresses**, with the severity of each, beside
  how many findings the code suppresses for each rule, and it is rewritten on every push rather
  than repeated.
- **It warns about what does not add up**: a suppression under which CodeQL found nothing of its
  rule, and a finding CodeQL suppressed that no comment read here accounts for, which would be a
  spelling CodeQL learned after this was written. A warning goes on the line when the pull request
  changed that line, and into the comment when it did not, and is annotated either way.

**What a pull request changes is what GitHub's diff shows**: the files GitHub lists for it, by the
names GitHub gives them, a renamed one with its old name, and GitHub's own hunks, the head against
where its branch left the one it merges into. A suppression only moved, with its file or to another,
is no removal. What CodeQL made of the code is judged on the checkout, the commit merging the two,
and each suppression's comment, a line of the pull request's own, is moved onto the head to be
placed, so a branch behind the one it merges into gets its threads where GitHub shows its lines.

**Where GitHub takes no thread on a line, the whole file gets one**, as for a diff too large for it
to show, and where it takes none at all, the summary names the suppression with GitHub's answer.
`spikes/codeql-suppressions` measured where GitHub takes a thread.

**A thread the review posted whose suppression is no longer in the pull request is deleted**, by
deleting its only comment, since nothing is left in it to read and the merge would wait for it.
Deleted rather than resolved, because the workflow's token may delete what it posted and may not
resolve a thread without `contents: write`, which `spikes/codeql-suppressions` measured. One
somebody answered in, or resolved, is kept, as what a person wrote or decided, and the summary
lists every one with what became of it.

**It fails only when it cannot warn**: a token that may not write to the pull request, which is
what a pull request from a fork is given, or GitHub refusing a comment for good. A finding nothing
suppresses is the verdict's to fail on.

**A push's threads go out as one review, built up while it is pending.** GitHub refuses a review
of more than 80 comments outright, as a secondary rate limit, and holds a pending one to the same,
so the review is opened with one thread and takes the others a second apart before it is submitted
once. GitHub answers 502 to a submission it takes more than about ten seconds over while making it
all the same, so a 5xx is read back rather than believed, and a refusal for rate is waited out as
GitHub's documentation asks before it is taken for good. A pending review an earlier run left
behind is deleted first, since GitHub keeps one a user. `spikes/codeql-suppressions` measured all
of it.

On `master` and on the weekly run there is no pull request, so the list and the warnings go to the
run's summary instead, and the warnings are annotated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import code_scanning
import suppressions

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MARK = "<!-- code-scanning: {} -->"
SUMMARY = MARK.format("summary")
# How far apart the threads of one review go in: GitHub refuses a review of more than 80 comments,
# pending or not, as a secondary rate limit, and took the threads of a pending review added one at
# a time this far apart, 112 of them, with no refusal. Every other write the review makes waits as
# long, which GitHub's documentation asks of writes in a row.
THREAD_GAP = 1.0
# What a refusal worth another try is waited out with when GitHub names no wait: a minute, then
# twice as long each time, which is what its documentation gives for a secondary rate limit. After
# the last, the refusal stands.
RETRY_WAITS = (60.0, 120.0, 240.0)
# How far either side of its line a thread reaches. GitHub's diff carries three unchanged lines
# around every change and takes a thread only inside one of its hunks, so a suppression's own
# comment, a changed line, always has this much in view.
REACH = 3
# The most lines of flagged code a thread quotes, past which the rest is counted rather than shown.
EXCERPT = 15
FENCES = {".cs": "csharp", ".py": "python"}
LINK = re.compile(r"\[([^\]]*)\]\((\d+)\)")
MARKED = re.compile(r"<!-- code-scanning: (.+?) -->")


@dataclass(frozen=True)
class Placed:
    """One `codeql` marker, where it is, what it covers, and the text that identifies it."""

    path: str
    rule: str
    line: int
    covered: int
    comment: str
    target: str
    reason: str


@dataclass(frozen=True)
class Note:
    """One thing the review tells a pull request, on a line when it can, in the comment always."""

    kind: str
    path: str
    line: int
    side: str
    body: str
    key: str
    # The first and last line the thread covers, when the diff gives it more than its own line.
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class Changed:
    """One file a pull request changes, as GitHub lists it: its name, its old name, how it changed.

    `patch` is GitHub's own diff of it, which GitHub leaves out of the list for a file whose diff
    is large and for a rename that changes nothing.
    """

    path: str
    previous: str
    status: str
    patch: str | None = None


@dataclass
class Patch:
    """One file's hunks and the lines it changes, by side: the left removes, the right adds."""

    hunks: dict[str, list[tuple[int, int]]] = field(
        default_factory=lambda: {"LEFT": [], "RIGHT": []})
    changed: dict[str, set[int]] = field(default_factory=lambda: {"LEFT": set(), "RIGHT": set()})


@dataclass(frozen=True)
class View:
    """The pull request as GitHub's diff has it, which is where its threads can go.

    Every file is named as at the head: its patch, and its lines as each side reads them, the head
    on the right and the merge base on the left.
    """

    patches: dict[str, Patch]
    lines: Callable[[str, str], list[str]]


@dataclass
class Review:
    """What the review found: what the pull request suppresses anew, removes, and what is amiss.

    `findings` are what CodeQL found, on the lines the review's suppressions are on: a pull
    request's head's for one, the checkout's otherwise.
    """

    added: list[Placed] = field(default_factory=list)
    removed: list[Placed] = field(default_factory=list)
    stale: list[Placed] = field(default_factory=list)
    unaccounted: list[code_scanning.Finding] = field(default_factory=list)
    totals: Counter[str] = field(default_factory=Counter)
    findings: list[code_scanning.Finding] = field(default_factory=list)


def placed(name: str, source: suppressions.Source,
           found: list[suppressions.Suppression]) -> list[Placed]:
    """Every `codeql` marker in one file, with the comment and the line it covers as written."""
    lines = source.text.split("\n")
    markers: list[Placed] = []
    for suppression in found:
        target = lines[suppression.covered - 1].strip() if suppression.covered <= len(lines) else ""
        for marker in suppression.markers:
            if marker.kind == "codeql":
                markers.append(Placed(name, marker.rule, suppression.line, suppression.covered,
                                      suppression.text.strip(), target, suppression.reason))
    return markers


def read_patch(patch: str) -> Patch:
    """One file's diff, GitHub's own or git's: its hunks and the lines it changes, by side.

    Inside a hunk, its lines are counted off its header, so a line whose text starts as a header
    would is read as the text it is.
    """
    read = Patch()
    old = new = old_left = new_left = 0
    for line in patch.splitlines():
        if old_left == 0 and new_left == 0:
            if (hunk := HUNK.match(line)) is not None:
                old, old_left = int(hunk[1]), int(hunk[2] or "1")
                new, new_left = int(hunk[3]), int(hunk[4] or "1")
                if old_left:
                    read.hunks["LEFT"].append((old, old + old_left - 1))
                if new_left:
                    read.hunks["RIGHT"].append((new, new + new_left - 1))
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("-"):
            read.changed["LEFT"].add(old)
            old, old_left = old + 1, old_left - 1
        elif line.startswith("+"):
            read.changed["RIGHT"].add(new)
            new, new_left = new + 1, new_left - 1
        else:
            old, old_left, new, new_left = old + 1, old_left - 1, new + 1, new_left - 1
    return read


def span(line: int, hunks_of_file: list[tuple[int, int]], length: int,
         through: int = 0) -> tuple[int, int] | None:
    """The lines a thread about `line` covers: `REACH` either side, inside the file and its hunk.

    Below the line it reaches `through` as well, the last line CodeQL flagged, as far as the hunk
    goes. None when the line is not in the file or no hunk holds it, since GitHub takes no thread
    there.
    """
    if not 1 <= line <= length:
        return None
    for first, last in hunks_of_file:
        if first <= line <= last:
            return max(1, line - REACH, first), min(length, max(line + REACH, through), last)
    return None


def changes(diff: str) -> list[tuple[int, int, int, int]]:
    """Every hunk of one file's diff without context.

    Each is where its old lines start and how many there are, and the same for its new ones.
    """
    return [(int(hunk[1]), int(hunk[2] or "1"), int(hunk[3]), int(hunk[4] or "1"))
            for line in diff.splitlines() if (hunk := HUNK.match(line)) is not None]


def moved(line: int, file_changes: list[tuple[int, int, int, int]]) -> int | None:
    """Where the old side's `line` is on the new side, or None when a change replaced it."""
    shift = 0
    for old_start, old_count, _, new_count in file_changes:
        if old_count == 0:
            # An insertion goes after `old_start`, so it moves only the lines below that.
            if line <= old_start:
                break
        elif line < old_start:
            break
        elif line < old_start + old_count:
            return None
        shift += new_count - old_count
    return line + shift


def compare(base: list[Placed], head: list[Placed]) -> tuple[list[Placed], list[Placed]]:
    """The markers the head adds or changes, and the ones it takes away, compared by their text.

    A marker is the same one in both when its rule, its comment and the line it covers read the
    same, wherever it moved. A head marker with no such twin is added or changed. A base marker is
    removed only when nothing in the head keeps its rule with either the same comment or the same
    line under it, since a changed reason or a changed line is a change rather than a removal.
    """
    unmatched = Counter((m.path, m.rule, m.comment, m.target) for m in base)
    added: list[Placed] = []
    for marker in head:
        key = (marker.path, marker.rule, marker.comment, marker.target)
        if unmatched[key] > 0:
            unmatched[key] -= 1
        else:
            added.append(marker)
    kept_comments = {(m.path, m.rule, m.comment) for m in head}
    kept_targets = {(m.path, m.rule, m.target) for m in head}
    removed = [m for m in base
               if unmatched[(m.path, m.rule, m.comment, m.target)] > 0
               and (m.path, m.rule, m.comment) not in kept_comments
               and (m.path, m.rule, m.target) not in kept_targets]
    return added, removed


def _suppressed_at(findings: list[code_scanning.Finding]) -> dict[tuple[str, int], set[str]]:
    at: dict[tuple[str, int], set[str]] = {}
    for finding in findings:
        if finding.suppressed:
            at.setdefault((finding.path, finding.line), set()).add(finding.rule)
    return at


def stale(head: list[Placed], findings: list[code_scanning.Finding]) -> list[Placed]:
    """The markers under which CodeQL suppressed nothing of their rule."""
    at = _suppressed_at(findings)
    return [m for m in head if m.rule not in at.get((m.path, m.covered), set())]


def unaccounted(scanned: list[suppressions.Scanned],
                findings: list[code_scanning.Finding]) -> list[code_scanning.Finding]:
    """The findings CodeQL suppressed that no comment read here covers."""
    covering = {name: suppressions.coverage(found) for name, _, found in scanned}
    return [f for f in findings if f.suppressed
            and not suppressions.suppressed(covering.get(f.path, {}), f.rule, f.line)]


def _finding(findings: list[code_scanning.Finding], marker: Placed) -> code_scanning.Finding | None:
    return next((f for f in findings
                 if (f.path, f.line, f.rule) == (marker.path, marker.covered, marker.rule)), None)


def _severity(findings: list[code_scanning.Finding], marker: Placed) -> str:
    finding = _finding(findings, marker)
    if finding is None:
        return "suppresses nothing"
    return f"security, {finding.severity}" if finding.severity else "quality"


def _quoted(finding: code_scanning.Finding) -> str:
    # CodeQL's message links to places by number; a thread names their lines instead.
    places = {number: (path, line) for number, path, line in finding.related}

    def named(link: re.Match[str]) -> str:
        if (where := places.get(int(link[2]))) is None:
            return link[1]
        path, line = where
        return f"{link[1]} (line {line})" if path == finding.path else f"{link[1]} ({path}:{line})"

    return LINK.sub(named, finding.message)


def _excerpt(path: str, code: list[str], last: int) -> str:
    shown = code[:EXCERPT]
    indent = min((len(line) - len(line.lstrip()) for line in shown if line.strip()), default=0)
    fence = "````" if any("```" in line for line in shown) else "```"
    block = "\n".join(line[indent:] for line in shown)
    text = f"{fence}{FENCES.get(Path(path).suffix, '')}\n{block}\n{fence}"
    if len(code) > EXCERPT:
        text += f"\n\n*{len(code) - EXCERPT} more lines, to line {last}.*"
    return text


def _about(finding: code_scanning.Finding | None, whole: bool, code: list[str]) -> str:
    # CodeQL's message when it adds to the rule's title, and the flagged code unless the thread
    # already shows the whole of it.
    if finding is None:
        return ""
    parts = []
    message = _quoted(finding)
    if message.rstrip(".").casefold() != finding.name.rstrip(".").casefold():
        parts.append(f"CodeQL: {message}")
    end = finding.end_line or finding.line
    if not whole and end <= len(code):
        parts.append(_excerpt(finding.path, code[finding.line - 1:end], end))
    return "".join(f"\n\n{part}" for part in parts)


def _key(kind: str, marker: Placed) -> str:
    digest = hashlib.sha256(f"{marker.comment}\n{marker.target}".encode()).hexdigest()[:12]
    return f"{kind}:{marker.path}:{marker.rule}:{digest}"


def notes(review: Review, view: View) -> list[Note]:
    """What goes on the pull request's lines, each with the key that keeps it from repeating.

    Everything in `review` is as GitHub's diff has it, on the head's lines, and a removed
    suppression on the merge base's. Each note covers the lines around its own that `view` shows,
    and an added one quotes what CodeQL found.
    """

    def around(path: str, line: int, side: str, through: int = 0) -> tuple[int, int] | None:
        patch = view.patches.get(path, Patch())
        return span(line, patch.hunks[side], len(view.lines(path, side)), through)

    def changed(path: str, side: str) -> set[int]:
        return view.patches.get(path, Patch()).changed[side]

    placed_notes: list[Note] = []
    for marker in review.added:
        finding = _finding(review.findings, marker)
        title = f", {finding.name}" if finding is not None and finding.name else ""
        end = (finding.end_line or finding.line) if finding is not None else 0
        covered = around(marker.path, marker.line, "RIGHT", end)
        whole = finding is not None and covered is not None and end <= covered[1]
        body = (f"**Suppresses `{marker.rule}`**{title} ({_severity(review.findings, marker)}): "
                f"{marker.reason}{_about(finding, whole, view.lines(marker.path, 'RIGHT'))}\n\n"
                "Resolve this thread once the suppression is accepted.")
        placed_notes.append(Note("added", marker.path, marker.line, "RIGHT", body,
                                 _key("added", marker), covered))
    for marker in review.removed:
        if marker.line in changed(marker.path, "LEFT"):
            # Removed with the line it covered, as when the file goes, the finding may well be gone.
            body = (f"**The suppression of `{marker.rule}` is removed, with the line it covered.** "
                    "If its finding remains, the verdict fails."
                    if marker.covered in changed(marker.path, "LEFT") else
                    f"**The suppression of `{marker.rule}` is removed.** Its finding has to be "
                    "fixed now, or the verdict fails.")
            placed_notes.append(Note("removed", marker.path, marker.line, "LEFT", body,
                                     _key("removed", marker),
                                     around(marker.path, marker.line, "LEFT")))
    for marker in review.stale:
        if {marker.line, marker.covered} & changed(marker.path, "RIGHT"):
            body = (f"**This suppresses nothing**: CodeQL found no `{marker.rule}` on the line "
                    "under it. Remove it, or move it directly above the finding.")
            placed_notes.append(Note("stale", marker.path, marker.line, "RIGHT", body,
                                     _key("stale", marker),
                                     around(marker.path, marker.line, "RIGHT")))
    for finding in review.unaccounted:
        if finding.line in changed(finding.path, "RIGHT"):
            body = (f"**CodeQL suppressed this `{finding.rule}` finding, and no comment read here "
                    "accounts for it.** Write the suppression as `codeql[rule-id]` above it.")
            key = f"unaccounted:{finding.path}:{finding.rule}:{finding.line}"
            placed_notes.append(Note("unaccounted", finding.path, finding.line, "RIGHT", body, key,
                                     around(finding.path, finding.line, "RIGHT",
                                            finding.end_line or finding.line)))
    return placed_notes


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _fate(posted: Posted, comment: dict[str, Any]) -> str:
    # What became of a thread whose suppression is gone, for the summary.
    number = int(comment["id"])
    if number in posted.deleted:
        return "deleted"
    return "kept: " + _cell(posted.kept.get(number, "not looked at"))


@dataclass
class Posted:
    """What the review put on the pull request, and what it could not.

    `files` are notes on a whole file, where GitHub took no thread on its lines, and `nowhere` are
    notes it took on neither, with what it answered. `gone` are threads the review posted whose
    suppression is no longer in the pull request, by their first comment: `deleted` are the numbers
    of the ones it deleted, and `kept` holds, for each of the others, why it stays.
    """

    lines: int = 0
    files: list[Note] = field(default_factory=list)
    nowhere: list[tuple[Note, str]] = field(default_factory=list)
    gone: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    kept: dict[int, str] = field(default_factory=dict)


def summary(review: Review, findings: list[code_scanning.Finding], pull_request: bool,
            posted: Posted | None = None) -> str:
    """The comment on a pull request, or the run's summary: what is suppressed, what is amiss."""
    parts = ["### Code scanning: suppressions", ""]
    if pull_request:
        security = sum(1 for m in review.added if _severity(findings, m).startswith("security"))
        parts += [f"**Newly suppressed in this pull request: {len(review.added)}**"
                  + (f", {security} of them security findings" if security else ""), ""]
        if review.added:
            parts += ["| Rule | Severity | Where | Reason |", "| --- | --- | --- | --- |"]
            parts += [f"| `{m.rule}` | {_severity(findings, m)} | `{m.path}:{m.covered}` | "
                      f"{_cell(m.reason)} |" for m in review.added]
            parts.append("")
        if review.removed:
            parts += [f"**Suppressions this pull request removes: {len(review.removed)}**", ""]
            parts += [f"- `{m.rule}` at `{m.path}:{m.covered}`" for m in review.removed]
            parts.append("")
        if posted is not None and posted.files:
            parts += [f"**On their whole file, since GitHub took no thread on their lines: "
                      f"{len(posted.files)}**", ""]
            parts += [f"- `{n.path}:{n.line}`, {n.kind}" for n in posted.files]
            parts.append("")
        if posted is not None and posted.nowhere:
            parts += [f"**Where GitHub took no thread at all: {len(posted.nowhere)}**", ""]
            parts += [f"- `{n.path}:{n.line}`, {n.kind}: {_cell(answer)}"
                      for n, answer in posted.nowhere]
            parts.append("")
        if posted is not None and posted.gone:
            parts += [f"**Threads whose suppression is no longer in this pull request: "
                      f"{len(posted.gone)}**", ""]
            parts += [f"- `{c.get('path')}:{c.get('line') or c.get('original_line')}`, "
                      f"{_fate(posted, c)}" for c in posted.gone]
            parts.append("")
    change = Counter(m.rule for m in review.added)
    change.subtract(Counter(m.rule for m in review.removed))
    parts += [f"**Suppressed in total: {sum(review.totals.values())}**", ""]
    if review.totals:
        parts += ["| Rule | Count |", "| --- | --- |"]
        for rule, count in sorted(review.totals.items(), key=lambda item: (-item[1], item[0])):
            delta = f" ({change[rule]:+d})" if pull_request and change[rule] else ""
            parts.append(f"| `{rule}` | {count}{delta} |")
        parts.append("")
    parts += [f"**Suppressions that suppress nothing: {len(review.stale)}**", ""]
    parts += [f"- `{m.rule}` at `{m.path}:{m.line}`" for m in review.stale]
    parts += ["", f"**Findings CodeQL suppressed that no comment accounts for: "
                  f"{len(review.unaccounted)}**", ""]
    parts += [f"- `{f.rule}` at `{f.path}:{f.line}`" for f in review.unaccounted]
    return "\n".join([*parts, "", SUMMARY]).replace("\n\n\n", "\n\n")


def warnings(review: Review) -> list[str]:
    """The workflow commands that annotate every warning, wherever it is."""
    lines = []
    for marker in review.stale:
        where = f"file={code_scanning.escape(marker.path, value=True)},line={marker.line}"
        lines.append(f"::warning {where},title=suppresses nothing::CodeQL found no {marker.rule} "
                     "on the line under this suppression.")
    for finding in review.unaccounted:
        where = f"file={code_scanning.escape(finding.path, value=True)},line={finding.line}"
        lines.append(f"::warning {where},title=unaccounted suppression::CodeQL suppressed this "
                     f"{finding.rule} finding, and no comment read here accounts for it.")
    return lines


class Refused(Exception):
    """A request GitHub did not carry out, with what it gave to go on."""

    def __init__(self, status: int, message: str, request: str | None = None,
                 wait: float | None = None) -> None:
        """GitHub's status and message, the request's identifier for its support, and its wait."""
        super().__init__(f"{status}: {message}" + (f", request {request}" if request else ""))
        self.status = status
        self.message = message
        self.request = request
        self.wait = wait

    @property
    def for_rate(self) -> bool:
        """Whether GitHub refused for rate, which waiting gets past and nothing else does."""
        return self.status in (403, 429) and "rate limit" in self.message.lower()


def refused(answer: urllib.error.HTTPError) -> Refused:
    """What an error from GitHub holds: the status, the message, the request, and the wait it names.

    The wait is `retry-after` when GitHub sends it, and the reset of a spent primary limit when it
    sends that, which is the order its documentation gives.
    """
    text = answer.read().decode(errors="replace")
    try:
        message = str(json.loads(text)["message"])
    except (ValueError, KeyError, TypeError):
        message = text.strip() or str(answer.reason)
    after = answer.headers.get("retry-after", "")
    reset = answer.headers.get("x-ratelimit-reset", "")
    wait: float | None = None
    if after.isdigit():
        wait = float(after)
    elif answer.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
        wait = max(int(reset) - time.time(), 0.0) + 1
    return Refused(answer.code, message, answer.headers.get("x-github-request-id"), wait)


class Repository(Protocol):
    """What the review asks of GitHub, which the tests answer with a stand-in."""

    def review_comments(self, number: int) -> list[dict[str, Any]]:
        """Every review comment on the pull request."""

    def open_review(self, number: int, commit: str, comment: dict[str, Any]) -> tuple[int, str]:
        """A pending review opened with one comment on lines, by its number and its node."""

    def add_thread(self, review: str, thread: dict[str, Any]) -> bool:
        """One thread more in the pending review `review`, and whether GitHub made it."""

    def submit_review(self, review: str) -> None:
        """The pending review `review` submitted, as a comment and not an approval."""

    def review_state(self, number: int, review: int) -> str:
        """Whether a review is still pending, or how it was submitted."""

    def pending_reviews(self, number: int) -> list[tuple[int, str]]:
        """The token's own pending reviews on the pull request, by number and node."""

    def delete_review(self, number: int, review: int) -> None:
        """A pending review deleted, with every thread in it."""

    def review_thread_comments(self, number: int, review: int) -> list[dict[str, Any]]:
        """Every comment in one review."""

    def comment_on_file(self, number: int, commit: str, path: str, body: str) -> None:
        """One comment on a whole file, which GitHub takes where it takes no thread on a line."""

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        """Every file the pull request changes, as GitHub lists it."""

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        """Every comment on the pull request's conversation."""

    def write_comment(self, number: int, existing: int | None, body: str) -> None:
        """A comment on the conversation, written anew or over the one numbered `existing`."""

    def resolved_threads(self, number: int) -> set[int]:
        """The first comment of every resolved thread on the pull request, by its number."""

    def delete_comment(self, comment: int) -> None:
        """One review comment deleted, which takes a thread it is the only comment of with it."""


THREADS = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}"""


ADD_THREAD = """
mutation($review: ID!, $path: String!, $line: Int, $side: DiffSide, $startLine: Int,
         $startSide: DiffSide, $subjectType: PullRequestReviewThreadSubjectType, $body: String!) {
  addPullRequestReviewThread(input: {pullRequestReviewId: $review, path: $path, line: $line,
      side: $side, startLine: $startLine, startSide: $startSide, subjectType: $subjectType,
      body: $body}) {
    thread { id }
  }
}"""

SUBMIT = """
mutation($review: ID!) {
  submitPullRequestReview(input: {pullRequestReviewId: $review, event: COMMENT}) {
    pullRequestReview { id }
  }
}"""


class GitHub:
    """GitHub's REST API, as the workflow's token reaches it."""

    def __init__(self, repository: str, token: str, api: str = "https://api.github.com") -> None:
        """Talk to `repository` with `token`."""
        self._repository = repository
        self._base = f"{api}/repos/{repository}"
        self._graph = f"{api}/graphql"
        self._token = token

    def _call(self, method: str, path: str, body: Any = None) -> tuple[Any, str]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310, the scheme is the API's own
            path if path.startswith("http") else self._base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self._token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                text = response.read().decode()
                return (json.loads(text) if text else None), response.headers.get("Link", "")
        except urllib.error.HTTPError as answer:
            raise refused(answer) from answer

    def _pages(self, path: str) -> list[dict[str, Any]]:
        every: list[dict[str, Any]] = []
        url: str | None = f"{path}{'&' if '?' in path else '?'}per_page=100"
        while url is not None:
            page, links = self._call("GET", url)
            every.extend(page)
            found = re.search(r'<([^>]+)>;\s*rel="next"', links)
            url = found[1] if found else None
        return every

    def review_comments(self, number: int) -> list[dict[str, Any]]:
        """Every review comment on the pull request."""
        return self._pages(f"/pulls/{number}/comments")

    def open_review(self, number: int, commit: str, comment: dict[str, Any]) -> tuple[int, str]:
        """A pending review opened with one comment on lines, by its number and its node."""
        opened, _ = self._call("POST", f"/pulls/{number}/reviews",
                               {"commit_id": commit, "comments": [comment]})
        return int(opened["id"]), str(opened["node_id"])

    def add_thread(self, review: str, thread: dict[str, Any]) -> bool:
        """One thread more in the pending review `review`, and whether GitHub made it."""
        data = self._graphql(ADD_THREAD, {"review": review, **thread})
        return bool((data.get("addPullRequestReviewThread") or {}).get("thread"))

    def submit_review(self, review: str) -> None:
        """The pending review `review` submitted, as a comment and not an approval."""
        self._graphql(SUBMIT, {"review": review})

    def review_state(self, number: int, review: int) -> str:
        """Whether a review is still pending, or how it was submitted."""
        answer, _ = self._call("GET", f"/pulls/{number}/reviews/{review}")
        return str(answer["state"])

    def pending_reviews(self, number: int) -> list[tuple[int, str]]:
        """The token's own pending reviews on the pull request, by number and node."""
        return [(int(r["id"]), str(r["node_id"])) for r in self._pages(f"/pulls/{number}/reviews")
                if r.get("state") == "PENDING"]

    def delete_review(self, number: int, review: int) -> None:
        """A pending review deleted, with every thread in it."""
        self._call("DELETE", f"/pulls/{number}/reviews/{review}")

    def review_thread_comments(self, number: int, review: int) -> list[dict[str, Any]]:
        """Every comment in one review."""
        return self._pages(f"/pulls/{number}/reviews/{review}/comments")

    def comment_on_file(self, number: int, commit: str, path: str, body: str) -> None:
        """One comment on a whole file, which GitHub takes where it takes no thread on a line."""
        self._call("POST", f"/pulls/{number}/comments",
                   {"commit_id": commit, "path": path, "subject_type": "file", "body": body})

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        """Every file the pull request changes, as GitHub lists it."""
        return self._pages(f"/pulls/{number}/files")

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        """Every comment on the pull request's conversation."""
        return self._pages(f"/issues/{number}/comments")

    def write_comment(self, number: int, existing: int | None, body: str) -> None:
        """A comment on the conversation, written anew or over the one numbered `existing`."""
        if existing is None:
            self._call("POST", f"/issues/{number}/comments", {"body": body})
        else:
            self._call("PATCH", f"/issues/comments/{existing}", {"body": body})

    def alerts(self, ref: str, state: str) -> list[dict[str, Any]]:
        """Every CodeQL alert in one state on one ref."""
        query = f"ref={urllib.parse.quote(ref, safe='')}&state={state}&tool_name=CodeQL"
        return self._pages(f"/code-scanning/alerts?{query}")

    def change_alert(self, number: int, fields: dict[str, Any]) -> dict[str, Any]:
        """One alert dismissed or opened again, and what GitHub made of it."""
        changed: dict[str, Any] = self._call("PATCH", f"/code-scanning/alerts/{number}", fields)[0]
        return changed

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        # GraphQL answers a refusal with 200 and its errors, so those are raised as REST's would
        # be, a refusal for rate among them, which `Refused.for_rate` reads from the message.
        answer, _ = self._call("POST", self._graph, {"query": query, "variables": variables})
        errors = answer.get("errors") if isinstance(answer, dict) else None
        if errors or not isinstance(answer, dict):
            kinds = {str(error.get("type", "")) for error in errors or []}
            status = 403 if kinds & {"FORBIDDEN", "RATE_LIMITED"} else 422
            raise Refused(status, "; ".join(str(error.get("message", "")) for error in errors or [])
                          or "GitHub's GraphQL API answered with nothing")
        data: dict[str, Any] = answer["data"]
        return data

    def resolved_threads(self, number: int) -> set[int]:
        """The first comment of every resolved thread on the pull request, by its number."""
        owner, name = self._repository.split("/", 1)
        resolved: set[int] = set()
        after: str | None = None
        while True:
            data = self._graphql(THREADS, {"owner": owner, "name": name, "number": number,
                                           "after": after})
            threads = data["repository"]["pullRequest"]["reviewThreads"]
            resolved.update(int(thread["comments"]["nodes"][0]["databaseId"])
                            for thread in threads["nodes"]
                            if thread["isResolved"] and thread["comments"]["nodes"])
            if not threads["pageInfo"]["hasNextPage"]:
                return resolved
            after = threads["pageInfo"]["endCursor"]

    def delete_comment(self, comment: int) -> None:
        """One review comment deleted, which takes a thread it is the only comment of with it."""
        self._call("DELETE", f"/pulls/comments/{comment}")


def post(github: Repository, number: int, commit: str, placed_notes: list[Note],
         text: str | Callable[[Posted], str]) -> int:
    """Put the notes on the pull request, those not already there, and the summary in its comment.

    Each note goes on its lines where GitHub takes a thread there, else on its whole file, which
    GitHub took where it took no line, else nowhere and into the summary, which `text` writes from
    what became of them when it is not written already. A thread of the review's own whose note it
    makes no more is deleted, unless somebody answered in it or resolved it. Answers how many
    threads it opened.
    """
    every = github.review_comments(number)
    comments = [c for c in every if c.get("in_reply_to_id") is None]
    posted = Posted(gone=_gone(comments, placed_notes))
    turn = _Turns()
    _post_reviews(github, number, commit, _not_posted(comments, placed_notes), posted, turn)
    _delete_gone(github, number, every, posted, turn)
    _write_summary(github, number, text if isinstance(text, str) else text(posted))
    return posted.lines + len(posted.files)


class _Turns:
    """One write after another, each after the first `THREAD_GAP` after the one before it."""

    def __init__(self) -> None:
        """No write made yet."""
        self._first = True

    def __call__(self) -> None:
        """Wait, unless this is the first write."""
        if not self._first:
            _wait(THREAD_GAP)
        self._first = False


def _delete_gone(github: Repository, number: int, every: list[dict[str, Any]],
                 posted: Posted, turn: _Turns) -> None:
    # A thread whose suppression is gone has nothing left in the pull request to read, and the
    # merge would wait for it. The workflow's token may delete a comment the review posted and may
    # not resolve a thread, which takes `contents: write`, as `spikes/codeql-suppressions`
    # measured, so the review deletes its own, and the thread goes with its only comment. One
    # somebody answered in or resolved is what a person wrote or decided, and stays; so does
    # every one when which are resolved cannot be read, since that is the question.
    if not posted.gone:
        return
    answered = {c.get("in_reply_to_id") for c in every if c.get("in_reply_to_id") is not None}
    try:
        resolved = github.resolved_threads(number)
    except Refused as refusal:
        reason = f"which threads are resolved could not be read: {refusal.message}"
        posted.kept.update((int(c["id"]), reason) for c in posted.gone)
        return
    for comment in posted.gone:
        thread = int(comment["id"])
        if thread in answered:
            posted.kept[thread] = "somebody answered in it"
        elif thread in resolved:
            posted.kept[thread] = "resolved"
        else:
            turn()
            try:
                _patiently(partial(github.delete_comment, thread),
                           partial(_deleted, github, number, thread))
            except Refused as refusal:
                if refusal.status != 404:
                    posted.kept[thread] = f"GitHub refused to delete it: {refusal.message}"
                    continue
            posted.deleted.append(thread)


def _deleted(github: Repository, number: int, comment: int) -> bool:
    # Whether a comment is gone from the pull request, which a 5xx leaves open.
    return all(c.get("id") != comment for c in github.review_comments(number))


def _gone(comments: list[dict[str, Any]], placed_notes: list[Note]) -> list[dict[str, Any]]:
    # The review's own threads, known by their mark, whose note it makes no more.
    keys = {n.key for n in placed_notes}
    return [c for c in comments if (found := MARKED.search(str(c.get("body", "")))) is not None
            and found[1] not in keys]


def _not_posted(comments: list[dict[str, Any]], placed_notes: list[Note]) -> list[Note]:
    # A mark is counted rather than looked for, since twins, two suppressions with the same comment
    # over the same code in one file, share one: a twin added later gets a thread of its own, on
    # the twin with none at its line, which GitHub keeps up to date for each thread across pushes.
    fresh: set[Note] = set()
    for key in dict.fromkeys(n.key for n in placed_notes):
        twins = [n for n in placed_notes if n.key == key]
        threads = [c for c in comments if MARK.format(key) in str(c.get("body", ""))]
        if len(twins) > len(threads):
            at = {(c.get("path"), c.get("line")) for c in threads}
            bare = [n for n in twins if (n.path, n.line) not in at
                    and (n.path, _comment(n, True)["line"]) not in at]
            fresh.update(bare[:len(twins) - len(threads)])
    return [n for n in placed_notes if n in fresh]


def _comment(note: Note, ranged: bool) -> dict[str, Any]:
    first, last = note.span if ranged and note.span is not None else (note.line, note.line)
    comment: dict[str, Any] = {"path": note.path, "line": last, "side": note.side,
                               "body": f"{note.body}\n\n{MARK.format(note.key)}"}
    if first != last:
        comment.update(start_line=first, start_side=note.side)
    return comment


def _post_reviews(github: Repository, number: int, commit: str, waiting: list[Note],
                  posted: Posted, turn: _Turns) -> None:
    # One review for what a push brings, built up while it is pending and submitted once. GitHub
    # holds a pending review to the 80 comments a minute a submitted one is held to, so it is
    # opened with one note and takes the others a thread at a time, each write `THREAD_GAP` after
    # the one before it, a refused one among them, which `_Turns` keeps. A pending review an
    # earlier run left behind is deleted first, since GitHub keeps one a user and refuses a second
    # beside it, and nobody else ever saw it. A note GitHub takes on none of its lines goes on its
    # whole file inside the review, and one it takes nowhere into the summary.
    if not waiting:
        return
    for left, _ in github.pending_reviews(number):
        turn()
        _patiently(partial(github.delete_review, number, left),
                   partial(_not_pending, github, number, left))
    opened: tuple[int, str] | None = None
    unplaced: list[Note] = []
    for note in waiting:
        if opened is None:
            opened = _open(github, number, commit, note, turn)
            if opened is None:
                unplaced.append(note)
            else:
                posted.lines += 1
        elif _add(github, number, opened, note, True, turn) or (
                note.span is not None and _add(github, number, opened, note, False, turn)):
            posted.lines += 1
        else:
            unplaced.append(note)
    for note in unplaced:
        if opened is None:
            _on_its_own_file(github, number, commit, note, posted, turn)
        elif _add(github, number, opened, note, None, turn):
            posted.files.append(note)
        else:
            posted.nowhere.append((note, "GitHub took no thread on its lines or its file"))
    if opened is not None:
        turn()
        _patiently(partial(github.submit_review, opened[1]),
                   partial(_submitted, github, number, opened[0]))


def _open(github: Repository, number: int, commit: str, note: Note,
          turn: _Turns) -> tuple[int, str] | None:
    # The pending review, opened with this note over its span, or on its line alone where GitHub
    # finds the span in no hunk of its diff; None where it takes neither, which leaves the opening
    # to the next note. A 5xx is read back rather than believed, since GitHub may have opened it.
    for ranged in (True, False) if note.span is not None else (False,):
        turn()
        setbacks = 0
        while True:
            try:
                return github.open_review(number, commit, _comment(note, ranged))
            except Refused as refusal:
                if refusal.status == 422:
                    break
                if refusal.status >= 500 and (left := github.pending_reviews(number)):
                    return left[0]
                _setback(refusal, setbacks)
                setbacks += 1
    return None


def _add(github: Repository, number: int, opened: tuple[int, str], note: Note,
         ranged: bool | None, turn: _Turns) -> bool:
    # One thread added to the pending review: over the note's span, on its line alone or, with
    # `ranged` None, on its whole file. GitHub answers a thread it does not make, one outside its
    # diff among them, with no thread and no error, which is False here as a 422 is.
    thread = _thread(note, ranged)
    turn()
    setbacks = 0
    while True:
        try:
            return github.add_thread(opened[1], thread)
        except Refused as refusal:
            if refusal.status == 422:
                return False
            if refusal.status >= 500 and _in_review(github, number, opened[0], note, ranged):
                return True
            _setback(refusal, setbacks)
            setbacks += 1


def _thread(note: Note, ranged: bool | None) -> dict[str, Any]:
    # A note as GraphQL's `addPullRequestReviewThread` takes it.
    body = f"{note.body}\n\n{MARK.format(note.key)}"
    if ranged is None:
        return {"path": note.path, "line": None, "side": None, "startLine": None,
                "startSide": None, "subjectType": "FILE", "body": body}
    comment = _comment(note, ranged)
    first = comment.get("start_line")
    return {"path": note.path, "line": comment["line"], "side": note.side, "startLine": first,
            "startSide": None if first is None else note.side, "subjectType": None, "body": body}


def _on_its_own_file(github: Repository, number: int, commit: str, note: Note,
                     posted: Posted, turn: _Turns) -> None:
    # Where no note opened a review, GitHub taking none on its lines, each goes on its whole file
    # as a comment of its own, which GitHub takes where it takes no line, else into the summary.
    turn()
    try:
        _patiently(partial(github.comment_on_file, number, commit, note.path,
                           _comment(note, False)["body"]),
                   partial(_on_file, github, number, note))
        posted.files.append(note)
    except Refused as refusal:
        if refusal.status != 422:
            raise
        posted.nowhere.append((note, refusal.message))


def _patiently(send: Callable[[], None], landed: Callable[[], bool]) -> None:
    # One request, waited out through refusals for rate and 5xx answers, a 5xx read back first,
    # since GitHub may have carried it out all the same.
    for setbacks in range(len(RETRY_WAITS) + 1):
        try:
            send()
            return
        except Refused as refusal:
            if refusal.status >= 500 and landed():
                return
            _setback(refusal, setbacks)


def _in_review(github: Repository, number: int, review: int, note: Note,
               ranged: bool | None) -> bool:
    # Whether a note is in the pending review, which a 5xx leaves open, by its mark and its place:
    # two suppressions with the same comment over the same code in one file share a mark.
    try:
        comments = github.review_thread_comments(number, review)
    except Refused:
        return False
    mark = MARK.format(note.key)
    line = None if ranged is None else _comment(note, ranged)["line"]
    return any(mark in str(c.get("body", "")) and c.get("path") == note.path
               and (c.get("subject_type") == "file" if line is None else c.get("line") == line)
               for c in comments)


def _on_file(github: Repository, number: int, note: Note) -> bool:
    # Whether a note is on the pull request as a comment on its whole file.
    return any(MARK.format(note.key) in str(c.get("body", "")) and c.get("path") == note.path
               and c.get("subject_type") == "file" for c in github.review_comments(number))


def _submitted(github: Repository, number: int, review: int) -> bool:
    # Whether a review GitHub answered 502 to was submitted all the same, as it was when measured.
    return github.review_state(number, review) != "PENDING"


def _not_pending(github: Repository, number: int, review: int) -> bool:
    # Whether a pending review is gone.
    return all(left != review for left, _ in github.pending_reviews(number))


def _write_summary(github: Repository, number: int, text: str) -> None:
    # Looked for again on every try, since a 5xx may have written it.
    for setbacks in range(len(RETRY_WAITS) + 1):
        existing = next((int(c["id"]) for c in github.issue_comments(number)
                         if SUMMARY in str(c.get("body", ""))), None)
        try:
            github.write_comment(number, existing, text)
            return
        except Refused as refusal:
            _setback(refusal, setbacks)


def _setback(refusal: Refused, setbacks: int) -> None:
    # Only a refusal for rate and a 5xx are worth another try, and only `RETRY_WAITS` of them.
    if (refusal.status < 500 and not refusal.for_rate) or setbacks == len(RETRY_WAITS):
        raise refusal
    _wait(RETRY_WAITS[setbacks] if refusal.wait is None else refusal.wait)


def _wait(seconds: float) -> None:
    time.sleep(seconds)


def _git(root: Path, *arguments: str) -> str:
    ran = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True,  # noqa: S603, S607
                         encoding="utf-8", check=True)
    return ran.stdout


def _show(root: Path, commit: str, name: str) -> str | None:
    # One file as `commit` has it, or None where it has no such file.
    shown = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=root,  # noqa: S603, S607
                           capture_output=True, text=True, encoding="utf-8", check=False)
    return shown.stdout if shown.returncode == 0 else None


def _markers_at(root: Path, commit: str, name: str, as_name: str) -> list[Placed]:
    # The markers in one file as `commit` has it, named `as_name`, its name at the head.
    language = suppressions.language_of(as_name)
    text = _show(root, commit, name)
    if language is None or text is None or suppressions.CANDIDATE.search(text) is None:
        return []
    source = suppressions.read(text, language)
    return placed(as_name, source, suppressions.suppressions(source, language, as_name))


def files_of(listing: list[dict[str, Any]]) -> list[Changed]:
    """The files GitHub lists for a pull request, by the names GitHub gives them."""
    return [Changed(str(f["filename"]), str(f.get("previous_filename") or f["filename"]),
                    str(f["status"]), f.get("patch")) for f in listing]


def local_files(root: Path, merge_base: str, commit: str) -> list[Changed]:
    """The files git finds changed from `merge_base` to `commit`, renames found, as GitHub has them.

    git ends each name with a NUL and writes it unquoted, whatever it holds. A copy is listed as
    added, as GitHub lists one, and no file has a patch, so each one's diff is git's.
    """
    fields = _git(root, "diff", "--name-status", "-M", "-z", merge_base, commit).split("\0")
    found: list[Changed] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        if status.startswith("R"):
            found.append(Changed(fields[index + 2], fields[index + 1], "renamed"))
            index += 3
        else:
            kind = {"A": "added", "D": "removed"}.get(status[0], "modified")
            found.append(Changed(fields[index + 1], fields[index + 1], kind))
            index += 2
    return found


def view_of(root: Path, files: list[Changed], merge_base: str, commit: str) -> View:
    """The pull request's diff as GitHub has it, `commit` against `merge_base`, file by file.

    GitHub's own patch where it gives one, and git's diff of the file's two sides where it does
    not; a file only one side has is all added or all removed.
    """
    previous = {c.path: c.previous for c in files}
    lines = _reader(root, {"RIGHT": commit, "LEFT": merge_base}, previous)
    patches: dict[str, Patch] = {}
    for changed in files:
        if changed.patch is not None:
            patches[changed.path] = read_patch(changed.patch)
        elif changed.status in ("added", "removed"):
            side = "RIGHT" if changed.status == "added" else "LEFT"
            patch = Patch()
            if count := len(lines(changed.path, side)):
                patch.hunks[side].append((1, count))
                patch.changed[side].update(range(1, count + 1))
            patches[changed.path] = patch
        else:
            patches[changed.path] = read_patch(_git(
                root, "diff", "--no-color", "--unified=3",
                f"{merge_base}:{changed.previous}", f"{commit}:{changed.path}"))
    return View(patches, lines)


def onto_head(root: Path, commit: str) -> Callable[[str, int], tuple[str, int | None]]:
    """Where a line of the checkout is on the pull request's head: its file there, and its line.

    The checkout is the commit merging the pull request, whose files hold what the branch it merges
    into changed since it was left, so a line moves past those changes, one they replaced is on the
    head nowhere, and a file that branch renamed goes by the head's name.
    """
    named = _renamed(root, "HEAD", commit)
    moves: dict[str, list[tuple[int, int, int, int]]] = {}

    def onto(path: str, line: int) -> tuple[str, int | None]:
        head = named.get(path, path)
        if path not in moves:
            done = subprocess.run(["git", "diff", "--no-color", "--unified=0", f"HEAD:{path}",  # noqa: S603, S607
                                   f"{commit}:{head}"], cwd=root, capture_output=True, text=True,
                                  encoding="utf-8", check=False)
            moves[path] = changes(done.stdout) if done.returncode == 0 else []
        return head, moved(line, moves[path])

    return onto


def _renamed(root: Path, old: str, new: str) -> dict[str, str]:
    # Each file `old` has under a name `new` gives it another, by its name in `old`.
    fields = _git(root, "diff", "--name-status", "-M", "-z", old, new).split("\0")
    named: dict[str, str] = {}
    index = 0
    while index < len(fields) and fields[index]:
        if fields[index].startswith("R"):
            named[fields[index + 1]] = fields[index + 2]
            index += 3
        else:
            index += 2
    return named


def _merge_base(root: Path, base: str, commit: str) -> str | None:
    found = subprocess.run(["git", "merge-base", base, commit], cwd=root,  # noqa: S603, S607
                           capture_output=True, text=True, encoding="utf-8", check=False)
    return found.stdout.strip() if found.returncode == 0 else None


def gather(root: Path, findings: list[code_scanning.Finding], base: str | None,
           commit: str = "HEAD", files: list[Changed] | None = None) -> tuple[Review, list[Note]]:
    """Compare the code at `root` with `base`, when there is one, and with what CodeQL found.

    `commit` is the pull request's head, and `files` what it changes as GitHub lists them, which
    git's own list stands in for when not given. What it suppresses anew and what it removes, and
    the notes, come from the diff GitHub shows, the head against the merge base, renames followed.
    """
    scanned = suppressions.scan(root, suppressions.tracked(root))
    head = [m for name, source, found in scanned for m in placed(name, source, found)]
    review = Review(stale=stale(head, findings), unaccounted=unaccounted(scanned, findings),
                    totals=Counter(f.rule for f in findings if f.suppressed), findings=findings)
    if base is None:
        return review, []
    if (merge_base := _merge_base(root, base, commit)) is None:
        print("::warning::the pull request's head or its merge base is not in the checkout, so "
              "its threads are placed on the lines of the commit merging it.")
        merge_base, commit = base, "HEAD"
    listed = files if files is not None else local_files(root, merge_base, commit)
    heads: list[Placed] = []
    bases: list[Placed] = []
    for changed in listed:
        if changed.status != "removed":
            heads.extend(_markers_at(root, commit, changed.path, changed.path))
        if changed.status != "added":
            bases.extend(_markers_at(root, merge_base, changed.previous, changed.path))
    added, removed = compare(bases, heads)
    # A suppression moved to another file has its thread there, and is no removal.
    elsewhere = {(m.rule, m.comment, m.target) for m in added}
    pulled = Review(added=added,
                    removed=[m for m in removed if (m.rule, m.comment, m.target) not in elsewhere])
    # What CodeQL made of the code is the checkout's, so every judgment is made there, and only a
    # suppression's own comment, a line of the pull request's, is moved onto the head to be placed.
    onto = onto_head(root, commit)
    on = {c.path for c in listed if c.status != "removed"}
    checkout = {}
    for marker in head:
        path, line = onto(marker.path, marker.line)
        if line is not None and path in on:
            checkout[path, line, marker.rule] = marker
    for marker in pulled.added:
        if (there := checkout.get((marker.path, marker.line, marker.rule))) is not None and (
                finding := _finding(findings, there)) is not None:
            end = onto(finding.path, finding.end_line)[1] if finding.end_line else None
            related = tuple((number, *_onto_or_not(onto, path, at))
                            for number, path, at in finding.related)
            pulled.findings.append(replace(finding, path=marker.path, line=marker.covered,
                                           end_line=end, related=related))
    for marker in review.stale:
        path, line = onto(marker.path, marker.line)
        if line is not None and path in on:
            pulled.stale.append(replace(marker, path=path, line=line,
                                        covered=line + marker.covered - marker.line))
    for finding in review.unaccounted:
        path, line = onto(finding.path, finding.line)
        if line is not None and path in on:
            end = onto(finding.path, finding.end_line)[1] if finding.end_line else None
            pulled.unaccounted.append(replace(finding, path=path, line=line, end_line=end))
    review.added, review.removed, review.findings = pulled.added, pulled.removed, pulled.findings
    return review, notes(pulled, view_of(root, listed, merge_base, commit))


def _onto_or_not(onto: Callable[[str, int], tuple[str, int | None]], path: str,
                 line: int) -> tuple[str, int]:
    # A place CodeQL's message links to, on the head where it is there, and as it was where not.
    there, moved_to = onto(path, line)
    return (there, moved_to) if moved_to is not None else (path, line)


def _reader(root: Path, commits: dict[str, str],
            previous: dict[str, str]) -> Callable[[str, str], list[str]]:
    # Each file as the commit for a side reads it, by its name at the head, which on the left is the
    # name it had there; read once, and empty where that side has none.
    read: dict[tuple[str, str], list[str]] = {}

    def lines(path: str, side: str) -> list[str]:
        if (path, side) not in read:
            text = _show(root, commits[side], previous.get(path, path) if side == "LEFT" else path)
            read[path, side] = text.splitlines() if text is not None else []
        return read[path, side]

    return lines


def _report(text: str) -> None:
    # The summary, into the run's own summary when there is one, and printed.
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as written:
            written.write(text + "\n")
    print(text)


def main(argv: list[str] | None = None) -> int:
    """Review the suppressions, and answer 1 only when a warning could not be put in place."""
    parser = argparse.ArgumentParser(description="Review the suppressions a change touches.")
    parser.add_argument("--analyzed", type=Path, required=True, help="the SARIF CodeQL wrote")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--repository", help="owner/name, for a pull request")
    parser.add_argument("--pull-request", type=int, help="its number, when there is one")
    parser.add_argument("--commit", help="the pull request's head commit, which comments are on")
    parser.add_argument("--base", default="HEAD^1", help="what the pull request is compared with")
    arguments = parser.parse_args(argv)
    findings = [f for log in code_scanning.logs(arguments.analyzed)
                for f in code_scanning.findings(code_scanning.read(log))]
    root = arguments.root.resolve()
    if arguments.pull_request is None:
        review = gather(root, findings, None)[0]
        for line in warnings(review):
            print(line)
        _report(summary(review, review.findings, False))
        return 0
    github = GitHub(arguments.repository, os.environ.get("GITHUB_TOKEN", ""))
    written: list[str] = []
    deleted: list[int] = []
    placed_notes: list[Note] = []

    def compose(posted: Posted) -> str:
        written.append(summary(review, review.findings, True, posted))
        deleted[:] = posted.deleted
        return written[-1]

    try:
        files = files_of(github.pull_files(arguments.pull_request))
        review, placed_notes = gather(root, findings, arguments.base, arguments.commit or "HEAD",
                                      files)
        for line in warnings(review):
            print(line)
        count = post(github, arguments.pull_request, arguments.commit, placed_notes, compose)
    except Refused as refusal:
        print(f"::error::the review could not be written to the pull request "
              f"({code_scanning.escape(str(refusal))}), so its warnings would go unseen: "
              f"{len(placed_notes)} note(s).")
        return 1
    _report(written[-1])
    print(f"  {count} new thread(s), {len(deleted)} whose suppression is gone deleted, "
          "the summary comment written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
