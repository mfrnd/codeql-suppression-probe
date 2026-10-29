"""Prototype: make the dismissed alerts on the default branch match what the code suppresses.

Runs once one category's whole SARIF was uploaded for the commit analyzed, and processed. An open
alert whose result there is suppressed in the code is dismissed, with the comment's reason. An
alert this dismissed earlier that the analysis no longer reports as suppressed is opened again:
GitHub settles it as fixed when the finding is gone, and leaves it open when the finding stayed.

Usage: sync_dismissals.py <sarif> <category> <owner/repo> <commit> <ref>
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

PREFIX = "Suppressed in code: "
LIMIT = 280
MARKERS = re.compile(r"(?i)(?:\b(?:codeql|lgtm)\s*\[[^\]]*\]\s*)+(.*)$")


def gh(*arguments):
    """Runs `gh api` and gives back what it printed."""
    command = ["gh", "api", *arguments]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def alerts(repo, ref, state):
    """Every CodeQL alert in one state on one ref."""
    url = f"repos/{repo}/code-scanning/alerts?ref={ref}&tool_name=CodeQL&state={state}&per_page=100"
    printed = gh("--paginate", url, "--jq", ".[] | tojson")
    return [json.loads(line) for line in printed.splitlines() if line]


def reason(path, line):
    """The text after the markers on the line above `line`, or on `line` itself."""
    with open(path, encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    for text in lines[max(line - 2, 0):line]:
        found = MARKERS.search(text)
        if found:
            return found.group(1).strip()
    return ""


def suppressed_results(path):
    """The results the code suppresses, by rule, file and line, with the reason each gives."""
    with open(path, encoding="utf-8") as stream:
        sarif = json.load(stream)
    found = {}
    for run in sarif["runs"]:
        for result in run["results"]:
            if any(s.get("kind") == "inSource" for s in result.get("suppressions", [])):
                where = result["locations"][0]["physicalLocation"]
                uri = where["artifactLocation"]["uri"]
                line = where["region"]["startLine"]
                found[(result["ruleId"], uri, line)] = reason(uri, line)
    return found


def place(alert):
    """The rule, file and line an alert was last reported at."""
    location = alert["most_recent_instance"]["location"]
    return (alert["rule"]["id"], location["path"], location["start_line"])


def dismiss(endpoint, comment):
    """Dismisses one alert, cutting the comment to GitHub's limit only when the whole is refused."""
    fields = ["-f", "state=dismissed", "-f", "dismissed_reason=won't fix"]
    try:
        gh("-X", "PATCH", endpoint, *fields, "-f", f"dismissed_comment={comment}")
        return "whole"
    except subprocess.CalledProcessError as error:
        print(f"  a comment of {len(comment)} characters was refused: {error.stderr.strip()}")
        gh("-X", "PATCH", endpoint, *fields, "-f", f"dismissed_comment={comment[:LIMIT]}")
        return f"cut to {LIMIT}"


def main():
    sarif, category, repo, sha, ref = sys.argv[1:6]
    suppressed = suppressed_results(sarif)
    endpoint = f"repos/{repo}/code-scanning/alerts"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dismissed = 0
    for alert in alerts(repo, ref, "open"):
        instance = alert["most_recent_instance"]
        if instance.get("category") != category or instance.get("commit_sha") != sha:
            continue
        if place(alert) in suppressed:
            how = dismiss(f"{endpoint}/{alert['number']}", PREFIX + suppressed[place(alert)])
            dismissed += 1
            print(f"dismissed #{alert['number']} {place(alert)}, comment {how}, "
                  f"created {alert['created_at']}, dismissed at about {now}")
    kept = 0
    opened = 0
    for alert in alerts(repo, ref, "dismissed"):
        instance = alert["most_recent_instance"]
        ours = (alert.get("dismissed_comment") or "").startswith(PREFIX)
        if instance.get("category") != category or not ours:
            continue
        if instance.get("commit_sha") == sha and place(alert) in suppressed:
            kept += 1
            continue
        state = json.loads(gh("-X", "PATCH", f"{endpoint}/{alert['number']}", "-f", "state=open"))
        opened += 1
        print(f"opened #{alert['number']} again {place(alert)}, last reported at "
              f"{instance.get('commit_sha', '')[:7]}, now {state['state']}")
    print(f"{len(suppressed)} suppressed in the code: {dismissed} dismissed now, "
          f"{kept} dismissed already, {opened} opened again")


main()
