"""Validate every recipe file. Run after extraction; also run by the test suite."""

import sys

import yaml

from .paths import RECIPES
from .schema import validate_recipe


def validate_all() -> dict[str, list[str]]:
    """Map recipe filename -> list of problems, for files that have problems."""
    problems: dict[str, list[str]] = {}
    for path in sorted(RECIPES.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems[path.name] = [f"unparseable YAML: {exc}"]
            continue
        errors = validate_recipe(data, expected_id=path.stem)
        if errors:
            problems[path.name] = errors
    return problems


def main() -> int:
    problems = validate_all()
    count = len(list(RECIPES.glob("*.yaml")))
    if not problems:
        print(f"All {count} recipe file(s) valid.")
        return 0
    for name, errors in problems.items():
        print(f"\n{name}:")
        for err in errors:
            print(f"  - {err}")
    print(f"\n{len(problems)} of {count} recipe file(s) invalid.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
