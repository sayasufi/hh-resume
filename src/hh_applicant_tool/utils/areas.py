"""Expand selected hh regions for strict client-side vacancy filtering."""


async def expand_areas(api, selected: set[str]) -> set[str]:
    allowed = set(selected)

    def visit(node):
        allowed.add(str(node['id']))
        for child in node.get('areas') or []:
            visit(child)

    for area_id in sorted(selected):
        visit(await api.get(f'/areas/{area_id}'))
    return allowed
