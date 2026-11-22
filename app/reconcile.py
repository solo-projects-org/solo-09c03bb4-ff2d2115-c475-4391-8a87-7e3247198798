"""签收行对账逻辑(纯逻辑,不依赖数据库)。

问题模型
--------
一个对账单对应「一个出库批次 × 一种商品」。签收单上的每一行有:

- ``pieces``:该签收行记的件数(精确分数);
- ``candidates``:允许归属的同商品出库行号集合;
- ``verified``:核对员是否已核实。已核实行**必须**参与归属,不得暂缓。

每条出库行有一个冻结的件数上限(开单时按当时的换算底表现算并冻结)。保留的
签收行必须把**全部件数**拆到各自候选出库行上,可任意拆分,但每条出库行分到的
合计不得超过冻结件数。这是一个带需求与容量的二部分数流问题。

目标:先让**暂缓的签收行条数最少**,再让暂缓的件数合计最少;若仍并列,优先
暂缓行号较大的行(尽量保留先记的行),保证结果确定。签收行最多 12 条,因此直接
枚举暂缓子集(至多 2^12 个),每个子集用分数容量最大流判定可行性,总能得到
精确的全局最优解。

算法
----
判定一组签收行是否可同时归属:建网络 源 → 签收行(容量=件数)、签收行 → 候选
出库行(容量=无限)、出库行 → 汇(容量=冻结件数);可行当且仅当最大流等于全部
签收件数之和(所有需求都被满足)。所有容量用 :class:`fractions.Fraction`,
全程无浮点误差。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ReceiptLine:
    """一条签收行(冻结后的数据)。"""

    line_no: int
    pieces: Fraction
    candidates: tuple[int, ...]
    verified: bool = False


@dataclass(frozen=True)
class Allocation:
    """签收行 → 出库行的一份归属(件数)。"""

    receipt_line_no: int
    outbound_line_no: int
    pieces: Fraction


@dataclass(frozen=True)
class ReconcileResult:
    """一次对账的完整结果。"""

    kept_line_nos: tuple[int, ...]
    deferred_line_nos: tuple[int, ...]
    allocations: tuple[Allocation, ...]
    outbound_used: tuple[tuple[int, Fraction], ...]


class VerifiedLinesInfeasible(Exception):
    """已核实的签收行本身就无法同时归属(正常流程不应出现)。"""


class _Edge:
    __slots__ = ("to", "rev", "cap", "original")

    def __init__(self, to: int, rev: int, cap: Fraction) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap
        self.original = cap  # 正向边用于反推实际流量;反向边为 0


def _dinic(graph: list[list[_Edge]], source: int, sink: int, limit: Fraction) -> Fraction:
    """分数容量 Dinic 最大流。limit 为源点可流出的上界。"""
    if limit <= 0:
        return Fraction(0)
    size = len(graph)
    total = Fraction(0)
    while True:
        level = [-1] * size
        level[source] = 0
        queue: deque[int] = deque([source])
        while queue:
            node = queue.popleft()
            for edge in graph[node]:
                if edge.cap > 0 and level[edge.to] < 0:
                    level[edge.to] = level[node] + 1
                    queue.append(edge.to)
        if level[sink] < 0:
            return total

        next_edge = [0] * size

        def dfs(node: int, pushed: Fraction) -> Fraction:
            if node == sink:
                return pushed
            while next_edge[node] < len(graph[node]):
                edge = graph[node][next_edge[node]]
                if edge.cap > 0 and level[edge.to] == level[node] + 1:
                    sent = dfs(edge.to, pushed if pushed < edge.cap else edge.cap)
                    if sent > 0:
                        edge.cap -= sent
                        graph[edge.to][edge.rev].cap += sent
                        return sent
                next_edge[node] += 1
            return Fraction(0)

        while True:
            sent = dfs(source, limit)
            if sent == 0:
                break
            total += sent


def _route(
    lines: Sequence[ReceiptLine], capacities: Mapping[int, Fraction]
) -> tuple[bool, dict[int, list[Allocation]]]:
    """尝试把全部 lines 的件数归属到候选出库行。

    返回 (是否全部满足, 每行的归属列表)。不可行时归属列表为空。
    """
    line_list = list(lines)
    demand = sum((line.pieces for line in line_list), Fraction(0))
    if not line_list:
        return True, {}
    for line in line_list:
        for candidate in line.candidates:
            if candidate not in capacities:
                raise ValueError(f"候选出库行 {candidate} 没有冻结件数")

    source = 0
    next_node = 1
    line_node: dict[int, int] = {}
    for line in line_list:
        line_node[line.line_no] = next_node
        next_node += 1
    outbound_lines = sorted({c for line in line_list for c in line.candidates})
    outbound_node: dict[int, int] = {}
    node_outbound: dict[int, int] = {}
    for outbound in outbound_lines:
        outbound_node[outbound] = next_node
        node_outbound[next_node] = outbound
        next_node += 1
    sink = next_node
    next_node += 1

    graph: list[list[_Edge]] = [[] for _ in range(next_node)]

    def add_edge(u: int, v: int, cap: Fraction) -> None:
        graph[u].append(_Edge(v, len(graph[v]), cap))
        graph[v].append(_Edge(u, len(graph[u]) - 1, Fraction(0)))

    for line in line_list:
        # 去重候选,避免同一签收行到同一出库行建多条平行边影响流量回读。
        add_edge(source, line_node[line.line_no], line.pieces)
        for candidate in dict.fromkeys(line.candidates):
            # 单条签收行最多贡献自己的全部件数,demand 是安全上界。
            add_edge(line_node[line.line_no], outbound_node[candidate], demand)
    for outbound in outbound_lines:
        add_edge(outbound_node[outbound], sink, capacities[outbound])

    flow = _dinic(graph, source, sink, demand)
    if flow != demand:
        return False, {}

    allocations: dict[int, list[Allocation]] = {}
    for line in line_list:
        entries: list[Allocation] = []
        for edge in graph[line_node[line.line_no]]:
            outbound = node_outbound.get(edge.to)
            if outbound is None:
                continue  # 指向源点的反向边
            amount = edge.original - edge.cap
            if amount > 0:
                entries.append(Allocation(line.line_no, outbound, amount))
        allocations[line.line_no] = entries
    return True, allocations


def feasible(lines: Sequence[ReceiptLine], capacities: Mapping[int, Fraction]) -> bool:
    """给定签收行能否全部同时归属。"""
    return _route(lines, capacities)[0]


def mandatory_feasible(lines: Sequence[ReceiptLine], capacities: Mapping[int, Fraction]) -> bool:
    """已核实的签收行(必须保留)能否同时归属。"""
    return feasible([line for line in lines if line.verified], capacities)


def reconcile(lines: Sequence[ReceiptLine], capacities: Mapping[int, Fraction]) -> ReconcileResult:
    """求最少暂缓的签收行及保留行的完整归属。

    - 已核实行一律保留;暂缓集合只从**未核实**的行里选。
    - 先最小化暂缓行数,再最小化暂缓件数合计;若仍并列,优先暂缓行号较大的
      行(尽量保留先记的行),保证结果确定。
    - ``capacities`` 为冻结的出库行件数(键为出库行号)。

    若已核实行本身无法同时归属,抛 :class:`VerifiedLinesInfeasible`。
    """
    ordered = sorted(lines, key=lambda line: line.line_no)
    mandatory = [line for line in ordered if line.verified]
    unverified = [line for line in ordered if not line.verified]
    if not feasible(mandatory, capacities):
        raise VerifiedLinesInfeasible("已核实的签收行无法同时归属")

    for defer_count in range(len(unverified) + 1):
        best: tuple[tuple[Fraction, tuple[int, ...]], tuple[ReceiptLine, ...], list[ReceiptLine]] | None = None
        for combo in combinations(unverified, defer_count):
            deferred_nos = {line.line_no for line in combo}
            kept = [
                line
                for line in ordered
                if line.verified or line.line_no not in deferred_nos
            ]
            if not feasible(kept, capacities):
                continue
            # 次级目标:暂缓件数最少;末级:尽量保留先记的行(暂缓行号靠后的组合
            # 优先,即保留行号更小者),保证结果确定。
            key = (
                sum((line.pieces for line in combo), Fraction(0)),
                tuple(-no for no in sorted(line.line_no for line in combo)),
            )
            if best is None or key < best[0]:
                best = (key, combo, kept)
        if best is not None:
            _, combo, kept = best
            ok, allocations = _route(kept, capacities)
            assert ok
            flat = tuple(a for line in kept for a in allocations.get(line.line_no, ()))
            used: dict[int, Fraction] = {}
            for allocation in flat:
                used[allocation.outbound_line_no] = (
                    used.get(allocation.outbound_line_no, Fraction(0)) + allocation.pieces
                )
            return ReconcileResult(
                kept_line_nos=tuple(sorted(line.line_no for line in kept)),
                deferred_line_nos=tuple(sorted(line.line_no for line in combo)),
                allocations=flat,
                outbound_used=tuple(sorted(used.items())),
            )

    # 未核实行全部暂缓后只剩 mandatory,前面已确认可行,理论上不会到这里。
    raise VerifiedLinesInfeasible("无法找到可行归属")
