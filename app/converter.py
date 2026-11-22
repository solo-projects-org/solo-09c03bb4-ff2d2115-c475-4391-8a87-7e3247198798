"""换算关系图与一致性校验(纯逻辑,不依赖数据库)。

模型:同一商品的每个单位是图中的一个节点;一条换算关系
``1 from_unit = ratio to_unit``(ratio 为正整数)是一条带权有向边,
反向边的权重为 1/ratio。两仓关系合并后,同一商品任意两个单位之间
若存在多条换算路径,各路径算出的比例必须相同,否则即为矛盾。

- :func:`validate_new_table` 用于换表前校验:把另一仓的现行关系先入图,
  再逐条加入本仓提交的关系;若某条提交关系与图中已有路径推出的比例
  不一致,记录能证实矛盾的关系链(带来源仓)且该关系不入图。
- :func:`to_pieces` 用于查询时把某单位数量精确折算成「件」。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

#: 折算目标单位
TARGET_UNIT = "件"


@dataclass(frozen=True)
class Relation:
    """一条换算关系:1 from_unit = ratio to_unit。"""

    product: str
    from_unit: str
    to_unit: str
    ratio: int  # 正整数
    source: str  # 来源仓,如 "A" / "B";提交中的关系由调用方标注


@dataclass(frozen=True)
class Conflict:
    """一条被现行关系证伪的提交关系。"""

    submitted: Relation  # 引发矛盾的本次提交关系
    implied_ratio: Fraction  # 现行关系推出的 1 from_unit = implied_ratio to_unit
    proving: tuple[Relation, ...]  # 能证实矛盾的现行关系链(每条带来源仓)


def frac_str(value: Fraction) -> str:
    """精确数量的字符串形式:整数直接写,否则写成分数。"""
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


class ProductGraph:
    """单个商品的单位换算图。"""

    def __init__(self) -> None:
        # 节点 -> [(相邻节点, 边权, 对应关系)],边权 r 表示 1 本节点 = r 相邻节点
        self._adj: dict[str, list[tuple[str, Fraction, Relation]]] = {}

    def add(self, rel: Relation) -> None:
        r = Fraction(rel.ratio)
        self._adj.setdefault(rel.from_unit, []).append((rel.to_unit, r, rel))
        self._adj.setdefault(rel.to_unit, []).append((rel.from_unit, Fraction(1, 1) / r, rel))

    def factor(self, start: str, target: str) -> tuple[Fraction, list[Relation]] | None:
        """求 1 start = factor target,并给出换算路径上的关系;不可达返回 None。"""
        if start == target:
            return Fraction(1), []
        seen = {start}
        queue: deque[tuple[str, Fraction, list[Relation]]] = deque([(start, Fraction(1), [])])
        while queue:
            node, fac, path = queue.popleft()
            for nxt, edge, rel in self._adj.get(node, ()):
                if nxt in seen:
                    continue
                nfac, npath = fac * edge, path + [rel]
                if nxt == target:
                    return nfac, npath
                seen.add(nxt)
                queue.append((nxt, nfac, npath))
        return None


def build_graphs(relations: Iterable[Relation]) -> dict[str, ProductGraph]:
    graphs: dict[str, ProductGraph] = {}
    for rel in relations:
        graphs.setdefault(rel.product, ProductGraph()).add(rel)
    return graphs


def validate_new_table(existing: Iterable[Relation], submitted: Iterable[Relation]) -> list[Conflict]:
    """校验本仓新表与另一仓现行关系合并后是否自洽。

    ``existing`` 为另一仓提交时的现行关系(入库前已校验,彼此自洽);
    ``submitted`` 为本仓本次整表提交的关系。返回全部矛盾(可能为空)。
    矛盾关系不入图,因此后续关系仍基于"现行 + 已接受的提交"继续校验。
    """
    graphs = build_graphs(existing)
    conflicts: list[Conflict] = []
    for rel in submitted:
        graph = graphs.setdefault(rel.product, ProductGraph())
        found = graph.factor(rel.from_unit, rel.to_unit)
        if found is not None:
            implied, path = found
            if implied != rel.ratio:
                conflicts.append(Conflict(rel, implied, tuple(path)))
                continue
        graph.add(rel)
    return conflicts


def to_pieces(
    graphs: dict[str, ProductGraph], product: str, quantity: Fraction, unit: str
) -> tuple[Fraction | None, str | None]:
    """把 quantity(某单位)折成件。成功返回 (数量, None),失败返回 (None, 待补原因)。"""
    if unit == TARGET_UNIT:
        return quantity, None
    graph = graphs.get(product)
    if graph is None:
        return None, f"商品「{product}」没有任何换算关系"
    found = graph.factor(unit, TARGET_UNIT)
    if found is None:
        return None, f"商品「{product}」的单位「{unit}」与「{TARGET_UNIT}」之间没有换算路径"
    return quantity * found[0], None
