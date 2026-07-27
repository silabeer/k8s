#!/usr/bin/env python3
"""Ищет условия assert, которые не являются строками.

Классическая YAML-ловушка: `: ` (двоеточие с пробелом) внутри незакавыченного
элемента списка превращает его в отображение, а не в строку:

    that:
      - (x | b64decode) is search('ANSIBLE MANAGED: cluster')

YAML при этом валиден, поэтому yamllint молчит; ansible-lint тоже пропускает.
Ansible падает уже в рантайме с «Conditional expressions must be strings»,
и найти это можно только прогоном.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

ASSERT_KEYS = {"assert", "ansible.builtin.assert"}
SKIP_PARTS = {"collections", ".ansible_facts", "artifacts"}


def walk(node: object, path: pathlib.Path, found: list[tuple[pathlib.Path, object]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ASSERT_KEYS and isinstance(value, dict):
                that = value.get("that")
                items = that if isinstance(that, list) else [that]
                for item in items:
                    if item is not None and not isinstance(item, str):
                        found.append((path, item))
            walk(value, path, found)
    elif isinstance(node, list):
        for value in node:
            walk(value, path, found)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    found: list[tuple[pathlib.Path, object]] = []

    for candidate in sorted(root.glob("**/*.yml")):
        if candidate.is_symlink() or SKIP_PARTS & set(candidate.parts):
            continue
        try:
            data = yaml.safe_load(candidate.read_text())
        except yaml.YAMLError:
            continue
        walk(data, candidate.relative_to(root), found)

    if not found:
        print("assert: все условия — строки")
        return 0

    print("Условия assert, которые не являются строками (нужны кавычки):")
    for path, item in found:
        print(f"  {path}: {item}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
