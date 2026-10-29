"""Prototype: drop the results the code suppresses from a SARIF file before it is uploaded.

A result CodeQL's alert-suppression query marked `inSource` is left out, so a pull request gets no
alert for it. A security finding is dropped like any other, and named as one in the log.
"""
import json
import os
import sys

source, target = sys.argv[1], sys.argv[2]
with open(source, encoding="utf-8") as stream:
    sarif = json.load(stream)
for run in sarif["runs"]:
    severities = {}
    for component in [run["tool"]["driver"]] + run["tool"].get("extensions", []):
        for rule in component.get("rules", []):
            severities[rule["id"]] = rule.get("properties", {}).get("security-severity")
    kept = []
    for result in run["results"]:
        where = result["locations"][0]["physicalLocation"]
        at = f'{where["artifactLocation"]["uri"]}:{where["region"]["startLine"]}'
        if any(s.get("kind") == "inSource" for s in result.get("suppressions", [])):
            severity = severities.get(result["ruleId"])
            note = f" (security severity {severity})" if severity else ""
            print(f'dropped {result["ruleId"]} at {at}{note}')
        else:
            kept.append(result)
    run["results"] = kept
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "w", encoding="utf-8") as stream:
    json.dump(sarif, stream)
