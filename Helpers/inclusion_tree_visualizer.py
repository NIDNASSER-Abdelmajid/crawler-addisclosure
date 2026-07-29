import hashlib
import html
import os
from urllib.parse import urlparse


_TYPE_COLORS = {
    "document": "#cc00ff",
    "script": "#ffcc00",
    "image": "#00ccff",
    "xhr": "#00ff00",
    "other": "#cccccc",
}

_NODE_WIDTH = 180
_NODE_HEIGHT = 34
_TYPE_BAR_WIDTH = 10
_HORIZONTAL_GAP = 72
_VERTICAL_GAP = 22
_PADDING = 24
_LEGEND_WIDTH = 190


def _second_level_tld(url: str) -> str:
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return url
    if not hostname:
        return url
    parts = hostname.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname


def _node_id(node: dict) -> str:
    url = node.get("url") or ""
    return hashlib.sha256(url.encode()).hexdigest()[:10]


def _collect_type_counts(node: dict, counts: dict) -> None:
    resource_type = node.get("type", "other")
    counts[resource_type] = counts.get(resource_type, 0) + 1
    for child in node.get("children", []):
        _collect_type_counts(child, counts)


def _collect_levels(node: dict, depth: int, levels: dict[int, list[dict]]) -> None:
    levels.setdefault(depth, []).append(node)
    for child in node.get("children", []):
        _collect_levels(child, depth + 1, levels)


def _build_positions(levels: dict[int, list[dict]]) -> dict[str, tuple[int, int]]:
    positions: dict[str, tuple[int, int]] = {}
    for depth in sorted(levels):
        x = _PADDING + depth * (_NODE_WIDTH + _HORIZONTAL_GAP)
        for index, node in enumerate(levels[depth]):
            y = _PADDING + index * (_NODE_HEIGHT + _VERTICAL_GAP)
            positions[_node_id(node)] = (x, y)
    return positions


def _max_rows(levels: dict[int, list[dict]]) -> int:
    return max((len(nodes) for nodes in levels.values()), default=1)


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _svg_header(width: int, height: int) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">',
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#4b5563" />',
        "</marker>",
        "</defs>",
        '<rect width="100%" height="100%" fill="#ffffff" />',
    ]


def _render_edges(node: dict, positions: dict[str, tuple[int, int]], lines: list[str]) -> None:
    parent_id = _node_id(node)
    parent_x, parent_y = positions[parent_id]
    start_x = parent_x + _NODE_WIDTH
    start_y = parent_y + (_NODE_HEIGHT // 2)

    for child in node.get("children", []):
        child_id = _node_id(child)
        child_x, child_y = positions[child_id]
        end_x = child_x
        end_y = child_y + (_NODE_HEIGHT // 2)
        mid_x = start_x + ((end_x - start_x) // 2)
        path = (
            f"M {start_x} {start_y} "
            f"C {mid_x} {start_y}, {mid_x} {end_y}, {end_x} {end_y}"
        )
        lines.append(
            f'<path d="{path}" fill="none" stroke="#4b5563" stroke-width="1.5" marker-end="url(#arrow)" />'
        )
        _render_edges(child, positions, lines)


def _render_nodes(node: dict, positions: dict[str, tuple[int, int]], lines: list[str]) -> None:
    node_key = _node_id(node)
    x, y = positions[node_key]
    resource_type = node.get("type", "other")
    color = _TYPE_COLORS.get(resource_type, _TYPE_COLORS["other"])
    label = _escape(_second_level_tld(node.get("url", "")) or "[unknown]")
    lines.extend(
        [
            f'<g id="node-{node_key}">',
            f'<rect x="{x}" y="{y}" rx="8" ry="8" width="{_NODE_WIDTH}" height="{_NODE_HEIGHT}" fill="#f8fafc" stroke="#cbd5e1" />',
            f'<rect x="{x}" y="{y}" rx="8" ry="8" width="{_TYPE_BAR_WIDTH}" height="{_NODE_HEIGHT}" fill="{color}" />',
            f'<text x="{x + _TYPE_BAR_WIDTH + 10}" y="{y + 22}" font-family="Segoe UI, Arial, sans-serif" font-size="12" fill="#0f172a">{label}</text>',
            "</g>",
        ]
    )
    for child in node.get("children", []):
        _render_nodes(child, positions, lines)


def _render_legend(counts: dict[str, int], total: int, origin_x: int, lines: list[str]) -> None:
    legend_height = 36 + max(len(counts), 1) * 22
    lines.extend(
        [
            '<g id="legend">',
            f'<rect x="{origin_x}" y="{_PADDING}" rx="10" ry="10" width="{_LEGEND_WIDTH}" height="{legend_height}" fill="#ffffff" stroke="#cbd5e1" />',
            f'<text x="{origin_x + 14}" y="{_PADDING + 22}" font-family="Segoe UI, Arial, sans-serif" font-size="13" font-weight="600" fill="#0f172a">Legend</text>',
        ]
    )
    for index, resource_type in enumerate(sorted(counts)):
        count = counts[resource_type]
        percent = (count / total * 100) if total else 0
        color = _TYPE_COLORS.get(resource_type, _TYPE_COLORS["other"])
        row_y = _PADDING + 36 + index * 22
        label = _escape(f"{resource_type} ({count}, {percent:.1f}%)")
        lines.extend(
            [
                f'<rect x="{origin_x + 14}" y="{row_y - 10}" width="10" height="10" fill="{color}" />',
                f'<text x="{origin_x + 32}" y="{row_y}" font-family="Segoe UI, Arial, sans-serif" font-size="12" fill="#334155">{label}</text>',
            ]
        )
    lines.append("</g>")


def visualize_tree(tree: dict, output_dir: str, filename: str | None = None) -> str | None:
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    if filename is None:
        root_url = tree.get("url", "")
        digest = hashlib.sha256(root_url.encode()).hexdigest()[:8]
        filename = f"{digest}_inclusion_tree.svg"

    counts: dict[str, int] = {}
    _collect_type_counts(tree, counts)
    total = sum(counts.values())

    levels: dict[int, list[dict]] = {}
    _collect_levels(tree, 0, levels)
    positions = _build_positions(levels)

    max_rows = _max_rows(levels)
    tree_width = len(levels) * _NODE_WIDTH + max(len(levels) - 1, 0) * _HORIZONTAL_GAP
    tree_height = max_rows * _NODE_HEIGHT + max(max_rows - 1, 0) * _VERTICAL_GAP
    width = tree_width + _LEGEND_WIDTH + (_PADDING * 3)
    height = max(tree_height + (_PADDING * 2), 120)

    lines = _svg_header(width, height)
    _render_edges(tree, positions, lines)
    _render_nodes(tree, positions, lines)
    _render_legend(counts, total, tree_width + (_PADDING * 2), lines)
    lines.append("</svg>")

    output_path = os.path.join(output_dir, filename)
    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
    except Exception:
        return None

    return output_path
