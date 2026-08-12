from __future__ import annotations

import ast
import unittest
from pathlib import Path


SPEC_PATH = Path(__file__).parents[2] / "packaging" / "wiki-backend.spec"


def _data_destinations() -> set[str]:
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"), filename=str(SPEC_PATH))
    datas_value = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "datas"
            for target in node.targets
        )
    )
    return {
        tuple_node.elts[1].value
        for tuple_node in ast.walk(datas_value)
        if isinstance(tuple_node, ast.Tuple)
        and len(tuple_node.elts) == 2
        and isinstance(tuple_node.elts[1], ast.Constant)
        and isinstance(tuple_node.elts[1].value, str)
    }


class PackagingSpecTests(unittest.TestCase):
    def test_backend_static_assets_are_in_pyinstaller_datas(self) -> None:
        destinations = _data_destinations()
        self.assertTrue(
            {
                "frontend_dist",
                "backend/app/dashboard_static",
            }.issubset(destinations),
            f"missing static asset destination in {SPEC_PATH}: {destinations}",
        )
