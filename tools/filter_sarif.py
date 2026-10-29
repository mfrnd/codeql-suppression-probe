"""Prototype: drop the results the code suppresses from a SARIF file before it is uploaded.

A result CodeQL's alert-suppression query marked `inSource` is left out, so no alert is ever made
for it. A suppressed result of a security query is kept, and the run is failed afterwards, since a
security finding is fixed rather than suppressed. Writes the filtered file and, when a security
finding was suppressed, a file named `refused` beside it.
"""
import json
import os
import sys

source, target = sys.argv[1], sys.argv[2]
with open(source, encoding="utf-8") as stream:
    sarif = json.load(stream)
refused = []
for run in sarif["runs"]:
    rules = {}
    for component in [run["tool"]["driver"]] + run["tool"].get("extensions", []):
        for rule in component.get("rules", []):
            rules[rule["id"]] = rule.get("properties", {}).get("security-severity")
    kept = []
    for result in run["results"]:
        where = result["locations"][0]["physicalLocation"]
        at = f'{where["artifactLocation"]["uri"]}:{where["region"]["startLine"]}'
        marked = [s for s in result.get("suppressions", []) if s.get("kind") == "inSource"]
        if not marked:
            kept.append(result)
        elif rules.get(result["ruleId"]):
            refused.append(f'{result["ruleId"]} at {at} (security severity {rules[result["ruleId"]]})')
            kept.append(result)
        else:
            print(f'dropped {result["ruleId"]} at {at}')
    run["results"] = kept
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "w", encoding="utf-8") as stream:
    json.dump(sarif, stream)
if refused:
    open(os.path.join(os.path.dirname(target), "refused"), "w").write("\n".join(refused))
    for line in refused:
        print(f"::error::a security finding is suppressed, which is refused: {line}")
