"""对账算法单元测试:最少暂缓、精确分数、已核实行必须保留。"""
from fractions import Fraction

import pytest

from app.reconcile import (
    ReceiptLine,
    VerifiedLinesInfeasible,
    feasible,
    reconcile,
)


def line(no, pieces, candidates, verified=False):
    return ReceiptLine(no, Fraction(pieces), tuple(candidates), verified)


def caps(**kwargs):
    return {int(k[1:]): Fraction(v) for k, v in kwargs.items()}


class TestFeasible:
    def test_simple_split(self):
        assert feasible([line(1, 15, [10, 20])], caps(l10=10, l20=5))

    def test_capacity_shortfall(self):
        assert not feasible([line(1, 11, [10, 20])], caps(l10=10, l20=0))

    def test_candidate_subset_matters(self):
        # 两行都可归 10 号,只有 2 号能归 20 号:总量 15 但 20 号只能接 2 号
        lines = [line(1, 10, [10]), line(2, 5, [10, 20])]
        assert feasible(lines, caps(l10=10, l20=5))
        assert not feasible([line(1, 10, [10]), line(2, 6, [10, 20])], caps(l10=10, l20=5))

    def test_fraction_capacity_is_exact(self):
        assert feasible([line(1, Fraction(3, 5), [7])], {7: Fraction(3, 5)})
        assert not feasible([line(1, Fraction(3, 5), [7])], {7: Fraction(2, 5)})

    def test_unknown_candidate_raises(self):
        with pytest.raises(ValueError):
            feasible([line(1, 1, [99])], caps(l10=10))


class TestReconcile:
    def test_minimum_deferral_is_one_line_not_two(self):
        # 三条候选同为出库 1(上限 100);总量 120,最少只能暂缓一条。
        # 暂缓件数最少 => 暂缓件数最小的那条(30),保留 90:
        lines = [line(1, 50, [1]), line(2, 40, [1]), line(3, 30, [1])]
        result = reconcile(lines, {1: Fraction(100)})
        assert result.deferred_line_nos == (3,)
        assert result.kept_line_nos == (1, 2)
        assert result.outbound_used == ((1, Fraction(90)),)
        assert sum(a.pieces for a in result.allocations) == 90

    def test_deferral_needed_for_connectivity(self):
        # 总量装得下但行 2 只能去已满的出库行,必须暂缓其中一条
        lines = [line(1, 10, [1]), line(2, 5, [1])]
        assert reconcile(lines, {1: Fraction(10), 2: Fraction(100)}).deferred_line_nos == (2,)

    def test_no_deferral_needed(self):
        lines = [line(1, 300, [1]), line(2, 60, [1]), line(3, 18, [2])]
        result = reconcile(lines, {1: Fraction(360), 2: Fraction(18)})
        assert result.deferred_line_nos == ()
        assert result.kept_line_nos == (1, 2, 3)
        assert dict(result.outbound_used) == {1: Fraction(360), 2: Fraction(18)}

    def test_allocation_respects_capacity_and_conserves_pieces(self):
        lines = [
            line(1, Fraction(7, 2), [1, 2]),
            line(2, Fraction(5, 3), [2]),
            line(3, 4, [1]),
        ]
        capacity = {1: Fraction(10), 2: Fraction(3)}
        result = reconcile(lines, capacity)
        assert result.deferred_line_nos == ()
        by_line = {l.line_no: l.pieces for l in lines}
        for receipt_no in by_line:
            total = sum(a.pieces for a in result.allocations if a.receipt_line_no == receipt_no)
            assert total == by_line[receipt_no]
        for outbound, used in result.outbound_used:
            assert used <= capacity[outbound]

    def test_verified_lines_are_never_deferred(self):
        # 未核实行更小,但已核实行必须保留;二者抢同一容量,应暂缓未核实行
        lines = [line(1, 80, [1], verified=True), line(2, 30, [1])]
        result = reconcile(lines, {1: Fraction(100)})
        assert result.kept_line_nos == (1,)
        assert result.deferred_line_nos == (2,)
        assert result.outbound_used == ((1, Fraction(80)),)

    def test_verified_infeasible_raises(self):
        lines = [line(1, 80, [1], verified=True), line(2, 40, [1], verified=True)]
        with pytest.raises(VerifiedLinesInfeasible):
            reconcile(lines, {1: Fraction(100)})

    def test_tie_break_prefers_smaller_line_numbers(self):
        # 等件数的两条里任选其一都可,应选行号字典序最小(保留 1、暂缓 2)
        lines = [line(1, 10, [1]), line(2, 10, [1])]
        result = reconcile(lines, {1: Fraction(10)})
        assert result.deferred_line_nos == (2,)
        assert result.kept_line_nos == (1,)

    def test_minimize_deferred_pieces_after_count(self):
        # 暂缓行数只能是 1;选件数小的那条暂缓 => 暂缓 1 号(5 件)
        lines = [line(1, 5, [1]), line(2, 90, [1])]
        result = reconcile(lines, {1: Fraction(90)})
        assert result.deferred_line_nos == (1,)


class TestAgainstBruteForce:
    """随机小实例:与穷举所有暂缓子集的结果比对,验证"最少暂缓"是全局最优。"""

    def test_minimum_deferral_matches_exhaustive_search(self):
        import random
        from itertools import combinations

        rng = random.Random(20261122)
        for _ in range(300):
            outbound_nos = rng.sample(range(1, 8), rng.randint(1, 4))
            capacity = {no: Fraction(rng.randint(0, 10)) for no in outbound_nos}
            line_count = rng.randint(1, 7)
            lines = []
            for no in range(1, line_count + 1):
                pool = rng.sample(outbound_nos, rng.randint(1, len(outbound_nos)))
                pieces = Fraction(rng.randint(1, 12), rng.choice([1, 1, 2, 3]))
                lines.append(line(no, pieces, pool))

            result = reconcile(lines, capacity)

            # 独立穷举:最小暂缓数,再最小暂缓件数,再优先暂缓大行号。
            expected = None
            for count in range(line_count + 1):
                candidates = []
                for combo in combinations(lines, count):
                    kept = [l for l in lines if l not in combo]
                    if feasible(kept, capacity):
                        candidates.append((
                            sum((l.pieces for l in combo), Fraction(0)),
                            tuple(-l.line_no for l in combo),
                            tuple(sorted(l.line_no for l in combo)),
                        ))
                if candidates:
                    expected = min(candidates)[2]
                    break
            assert tuple(result.deferred_line_nos) == expected

            # 结果自洽:归属守恒、不超容量、候选合法、保留集合正确。
            kept = set(result.kept_line_nos)
            deferred = set(result.deferred_line_nos)
            assert kept | deferred == {l.line_no for l in lines}
            assert kept.isdisjoint(deferred)
            by_no = {l.line_no: l for l in lines}
            for receipt_no in kept:
                total = sum(
                    (a.pieces for a in result.allocations if a.receipt_line_no == receipt_no),
                    Fraction(0),
                )
                assert total == by_no[receipt_no].pieces
                for a in result.allocations:
                    if a.receipt_line_no == receipt_no:
                        assert a.outbound_line_no in by_no[receipt_no].candidates
            for outbound_no, used in result.outbound_used:
                assert used <= capacity[outbound_no]
            assert not any(l.verified for l in lines)
