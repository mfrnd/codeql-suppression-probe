"""Prototype: which suppressions does a pull request add or change, compared with its base.

A suppression is a `codeql[...]` comment, which covers the line after it, or an `lgtm`
comment, which covers its own line. One is reported when the pair of its comment line and the
line it covers is not in the base, so a new comment, a changed covered line and a covered line
that another one replaced are all reported. Prints JSON: a list of {path, line, comment, covered}.
"""
import json
import re
import subprocess
import sys

MARK = re.compile(r"(?i)\b(codeql|lgtm)\b\s*(\[[^\]]*\])?")
SOURCES = (".cs", ".py")


def lines_at(rev, path):
    try:
        out = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        return []
    return out.stdout.decode("utf-8", "replace").split("\n")


def comment_part(text, path):
    token = "#" if path.endswith(".py") else "//"
    i = text.find(token)
    if i < 0 and not path.endswith(".py"):
        i = text.find("/*")
    return None if i < 0 else text[i:]


def pairs(lines, path):
    found = []
    for n, text in enumerate(lines, start=1):
        comment = comment_part(text, path)
        if comment is None or not MARK.search(comment):
            continue
        own = text[: text.find(comment)].strip() == ""
        if MARK.search(comment).group(1).lower() == "codeql" and own:
            covered = lines[n] if n < len(lines) else ""
            found.append((n, text.strip(), covered.strip(), n + 1))
        else:
            found.append((n, text.strip(), text.strip(), n))
    return found


base, head = sys.argv[1], sys.argv[2]
changed = subprocess.run(["git", "diff", "--name-only", f"{base}...{head}"], capture_output=True,
                         check=True, text=True).stdout.split()
report = []
for path in changed:
    if not path.endswith(SOURCES):
        continue
    before = {(c, v) for _, c, v, _ in pairs(lines_at(base, path), path)}
    for n, comment, covered, target in pairs(lines_at(head, path), path):
        if (comment, covered) not in before:
            report.append({"path": path, "line": n, "covers": target, "comment": comment,
                           "covered": covered})
print(json.dumps(report, indent=1))
