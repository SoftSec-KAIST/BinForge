def render_status_tree(root, indent: str = "", is_last: bool = True, is_root: bool = True) -> str:
    status_map = {
        "pending": "🟨",
        "building": "🟦",
        "failed": "🟥",
        "success": "🟩"
    }
    symbol = status_map.get(root.status, "❓")

    if is_root:
        line = f"{symbol} {root.project.name} ({root.status})"
    else:
        prefix = indent + ("└── " if is_last else "├── ")
        line = f"{prefix}{symbol} {root.project.name} ({root.status})"

    lines = [line]
    for i, child in enumerate(root.children):
        is_last_child = (i == len(root.children) - 1)
        new_indent = indent + ("    " if not is_root and is_last else "│   ") if not is_root else ""
        lines.append(render_status_tree(child, new_indent, is_last_child, is_root=False))
    return "\n".join(lines)


def boxify_lines(lines: list[str]) -> str:
    max_width = max(len(line) for line in lines)
    top = f"┌{'─' * (max_width + 2)}┐"
    bottom = f"└{'─' * (max_width + 2)}┘"
    body = [f"│ {line.ljust(max_width)}│" for line in lines]
    return "\n".join([top] + body + [bottom])


def print_dependency_tree_status(root_node, label: str = "Dependency Tree Status"):
    print(f"\n[{label}]")
    tree_str = render_status_tree(root_node)
    lines = tree_str.splitlines()
    boxed = boxify_lines(lines)
    print(boxed)
    print()

    return f"[{label}]\n{boxed}"