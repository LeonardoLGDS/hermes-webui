"""Compare complete pytest JUnit reports without hiding baseline failures."""
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ElementTree


def summarize(path):
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    counts = dict(total=len(cases), passed=0, failed=0, errors=0, skipped=0, xfailed=0)
    failures = {}
    for case in cases:
        identity = case.get("classname", "") + "::" + case.get("name", "")
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None:
            counts["failed"] += 1
            failures[identity] = failure.get("message", "")
        elif error is not None:
            counts["errors"] += 1
            failures[identity] = error.get("message", "")
        elif skipped is not None:
            counts["xfailed" if skipped.get("type") == "pytest.xfail" else "skipped"] += 1
        else:
            counts["passed"] += 1
    log_lines = Path(path).with_suffix(".log").read_text().splitlines()
    summary = next(line for line in reversed(log_lines) if "passed" in line and " in " in line)
    pytest_counts = {name: int(value) for value, name in re.findall(
        r"(\d+) (failed|passed|skipped|xfailed|xpassed|warnings|errors|subtests passed)", summary)}
    return {"pytest_counts": pytest_counts, "raw_junit_counts": counts, "summary": summary}, failures


if __name__ == "__main__":
    candidate_counts, candidate_failures = summarize(sys.argv[1])
    base_counts, base_failures = summarize(sys.argv[2])
    print(json.dumps({
        "candidate": candidate_counts,
        "base": base_counts,
        "candidate_only_failures": {identity: message for identity, message in candidate_failures.items()
                                    if identity not in base_failures},
        "base_only_failures": {identity: message for identity, message in base_failures.items()
                               if identity not in candidate_failures},
        "shared_failure_count": len(set(candidate_failures) & set(base_failures)),
    }, indent=2))
