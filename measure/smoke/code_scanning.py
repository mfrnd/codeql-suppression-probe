"""What the CodeQL workflow does with an analysis between CodeQL and a merge.

CodeQL writes every finding into one SARIF file for each language, suppressed or not, and marks a
finding a comment suppresses as `inSource`. Code scanning ignores that mark: uploaded as it is, a
suppressed finding is an alert like any other, and a pull request adding one is held by the ruleset
until somebody dismisses it. So the workflow keeps the files CodeQL wrote, and this decides what
each upload gets and whether the analysis passes:

- `prepare` writes what is uploaded. On a pull request, every finding the code suppresses is left
  out, so none becomes an alert there; on `master` and on the weekly run, everything is uploaded, so
  that the Security tab can list every suppression as dismissed.
- `verdict` fails when any finding is left that nothing suppresses, anywhere in the analysis rather
  than only on the lines a pull request changed, which is all the ruleset's own check looks at. Each
  is annotated at its line, so the pull request shows where.

**The files CodeQL wrote are what is judged**, never what was uploaded: a pull request's upload is
cut to the lines it changed before code scanning reads it, while CodeQL's own files hold every
finding in the code.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    """One result of the analysis: its rule, where it is, and what CodeQL made of it."""

    rule: str
    path: str
    line: int
    message: str
    severity: str | None
    suppressed: bool
    # What the review quotes: the rule's own title, the last line CodeQL flagged when it flagged
    # more than one, and the places its message links to, by the number the message gives each.
    name: str = ""
    end_line: int | None = None
    related: tuple[tuple[int, str, int], ...] = ()


def _names(run: dict[str, Any]) -> dict[str, str]:
    """The title of every rule a run names, its short description."""
    tool = run["tool"]
    return {rule["id"]: rule.get("shortDescription", {}).get("text", "")
            for component in [tool["driver"], *tool.get("extensions", [])]
            for rule in component.get("rules", [])}


def _related(result: dict[str, Any]) -> tuple[tuple[int, str, int], ...]:
    """The places a result's message links to, each as its number, its file and its line."""
    places = []
    for location in result.get("relatedLocations", []):
        where = location.get("physicalLocation", {})
        line = where.get("region", {}).get("startLine")
        if "id" in location and line is not None:
            places.append((int(location["id"]), str(where["artifactLocation"]["uri"]), int(line)))
    return tuple(places)


def _severities(run: dict[str, Any]) -> dict[str, str | None]:
    """The security severity of every rule a run names, `None` for one that is not security."""
    tool = run["tool"]
    found: dict[str, str | None] = {}
    for component in [tool["driver"], *tool.get("extensions", [])]:
        for rule in component.get("rules", []):
            found[rule["id"]] = rule.get("properties", {}).get("security-severity")
    return found


def _suppressed(result: dict[str, Any]) -> bool:
    return any(s.get("kind") == "inSource" for s in result.get("suppressions", []))


def findings(sarif: dict[str, Any]) -> list[Finding]:
    """Every result in a SARIF log, in the order CodeQL wrote them."""
    found: list[Finding] = []
    for run in sarif["runs"]:
        severities = _severities(run)
        names = _names(run)
        for result in run["results"]:
            where = result["locations"][0]["physicalLocation"]
            found.append(Finding(
                rule=result["ruleId"],
                path=where["artifactLocation"]["uri"],
                line=where["region"]["startLine"],
                message=result["message"]["text"],
                severity=severities.get(result["ruleId"]),
                suppressed=_suppressed(result),
                name=names.get(result["ruleId"], ""),
                end_line=where["region"].get("endLine"),
                related=_related(result),
            ))
    return found


def without_suppressed(sarif: dict[str, Any]) -> dict[str, Any]:
    """The same log with every result the code suppresses left out, everything else as it was."""
    runs = [{**run, "results": [r for r in run["results"] if not _suppressed(r)]}
            for run in sarif["runs"]]
    return {**sarif, "runs": runs}


def read(path: Path) -> dict[str, Any]:
    """One SARIF log, as CodeQL wrote it."""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def logs(folder: Path) -> list[Path]:
    """Every SARIF log under `folder`, refusing a folder that holds none."""
    found = sorted(folder.rglob("*.sarif"))
    if not found:
        raise SystemExit(f"No SARIF file under {folder}: the analysis wrote nothing to judge.")
    return found


def prepare(analyzed: Path, upload: Path, pull_request: bool) -> list[str]:
    """Write into `upload` what each language uploads, and name every finding left out."""
    upload.mkdir(parents=True, exist_ok=True)
    dropped: list[str] = []
    for log in logs(analyzed):
        target = upload / log.name
        if not pull_request:
            shutil.copyfile(log, target)
            continue
        sarif = read(log)
        for finding in findings(sarif):
            if finding.suppressed:
                kind = f"security, {finding.severity}" if finding.severity else "quality"
                dropped.append(f"{finding.rule} at {finding.path}:{finding.line} ({kind})")
        target.write_text(json.dumps(without_suppressed(sarif)), encoding="utf-8")
    return dropped


def escape(text: str, *, value: bool = False) -> str:
    """Text a workflow command reads as it is, as its message or, with `value`, as a property."""
    escaped = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if value:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def annotation(finding: Finding) -> str:
    """The workflow command that marks a finding at its line, as an error."""
    where = f"file={escape(finding.path, value=True)},line={finding.line}"
    return f"::error {where},title={escape(finding.rule, value=True)}::{escape(finding.message)}"


def verdict(analyzed: Path) -> tuple[list[Finding], int]:
    """The findings nothing suppresses, and how many the code does."""
    every = [finding for log in logs(analyzed) for finding in findings(read(log))]
    left = [finding for finding in every if not finding.suppressed]
    return left, len(every) - len(left)


def main(argv: list[str] | None = None) -> int:
    """Run one of the steps the workflow takes, named first."""
    parser = argparse.ArgumentParser(description="What the CodeQL workflow does with an analysis.")
    steps = parser.add_subparsers(dest="step", required=True)
    preparing = steps.add_parser("prepare", help="write what each language uploads")
    preparing.add_argument("--analyzed", type=Path, required=True)
    preparing.add_argument("--upload", type=Path, required=True)
    preparing.add_argument("--event", required=True, help="the event the workflow runs for")
    judging = steps.add_parser("verdict", help="fail on a finding that nothing suppresses")
    judging.add_argument("--analyzed", type=Path, required=True)
    arguments = parser.parse_args(argv)

    if arguments.step == "prepare":
        pull_request = arguments.event == "pull_request"
        dropped = prepare(arguments.analyzed, arguments.upload, pull_request)
        for line in dropped:
            print(f"  left out, suppressed in the code: {line}")
        print(f"  {len(dropped)} suppressed finding(s) left out of the upload." if pull_request
              else "  everything uploaded, the suppressed findings included.")
        return 0

    left, suppressed = verdict(arguments.analyzed)
    for finding in left:
        print(annotation(finding))
    if left:
        print(f"{len(left)} finding(s) nothing suppresses, {suppressed} suppressed in the code. "
              "Fix each, or suppress it with its reason as CLAUDE.md describes under Conventions.")
        return 1
    print(f"  no finding left, {suppressed} suppressed in the code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
