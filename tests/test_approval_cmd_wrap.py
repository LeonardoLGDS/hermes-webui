import re
from pathlib import Path


def test_approval_cmd_wrap():
    css = (Path(__file__).resolve().parent / "../static/style.css").read_text(
        encoding="utf-8"
    )
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    rules = re.findall(r"(?:^|[{}])\s*\.approval-cmd\s*\{([^{}]*)\}", css)
    wrapping_properties = {"white-space", "word-break", "overflow-wrap"}
    wrapping_rules = []
    for rule in rules:
        declarations = [
            tuple(part.strip() for part in declaration.split(":", 1))
            for declaration in rule.split(";")
            if declaration.strip()
        ]
        assert ("word-break", "break-all") not in declarations
        if any(name in wrapping_properties for name, value in declarations):
            wrapping_rules.append(declarations)
    assert len(wrapping_rules) == 1
    declarations = wrapping_rules[0]
    assert ("white-space", "pre-wrap") in declarations
    assert ("word-break", "normal") in declarations
    assert ("overflow-wrap", "anywhere") in declarations
