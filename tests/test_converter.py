"""换算图与一致性校验的单元测试。"""
from fractions import Fraction

from app.converter import Relation, build_graphs, to_pieces, validate_new_table


def rel(product, f, t, ratio, source="A"):
    return Relation(product, f, t, ratio, source)


class TestValidateNewTable:
    def test_consistent_merge_passes(self):
        existing = [rel("P", "箱", "提", 12), rel("P", "提", "件", 6)]
        submitted = [rel("P", "箱", "件", 72, source="B")]  # 12*6=72,一致
        assert validate_new_table(existing, submitted) == []

    def test_direct_contradiction_reports_proving_relation(self):
        existing = [rel("P", "提", "件", 6, source="A")]
        submitted = [rel("P", "提", "件", 5, source="B")]
        (conflict,) = validate_new_table(existing, submitted)
        assert conflict.submitted.ratio == 5
        assert conflict.implied_ratio == 6
        assert [r.source for r in conflict.proving] == ["A"]

    def test_transitive_contradiction_reports_full_path(self):
        existing = [rel("P", "箱", "提", 12, source="A"), rel("P", "提", "件", 6, source="A")]
        submitted = [rel("P", "箱", "件", 100, source="B")]  # 路径推出 72
        (conflict,) = validate_new_table(existing, submitted)
        assert conflict.implied_ratio == 72
        assert {(r.from_unit, r.to_unit, r.ratio) for r in conflict.proving} == {
            ("箱", "提", 12),
            ("提", "件", 6),
        }

    def test_contradiction_inside_submitted_table(self):
        submitted = [rel("P", "箱", "件", 10, source="B"), rel("P", "箱", "件", 11, source="B")]
        (conflict,) = validate_new_table([], submitted)
        assert conflict.implied_ratio == 10
        assert all(r.source == "B" for r in conflict.proving)

    def test_conflicting_relation_not_added_further_checks_continue(self):
        # 第一条矛盾(不入图),第二条与现行一致,应只有 1 个矛盾
        existing = [rel("P", "提", "件", 6, source="A")]
        submitted = [rel("P", "提", "件", 5, source="B"), rel("P", "箱", "件", 72, source="B")]
        # 注意:箱->件 72 需要 箱 与 件 连通;现有图只有 提-件,箱 不可达,故一致通过
        conflicts = validate_new_table(existing, submitted)
        assert len(conflicts) == 1

    def test_products_are_independent(self):
        existing = [rel("P1", "箱", "件", 10, source="A")]
        submitted = [rel("P2", "箱", "件", 99, source="B")]
        assert validate_new_table(existing, submitted) == []


class TestToPieces:
    def test_direct_and_transitive_conversion(self):
        graphs = build_graphs([rel("P", "箱", "提", 12), rel("P", "提", "件", 6)])
        assert to_pieces(graphs, "P", Fraction(5), "箱") == (Fraction(360), None)
        assert to_pieces(graphs, "P", Fraction(3), "提") == (Fraction(18), None)

    def test_target_unit_itself_needs_no_relation(self):
        assert to_pieces({}, "UNKNOWN", Fraction(7), "件") == (Fraction(7), None)

    def test_fractional_factor_is_exact(self):
        graphs = build_graphs([rel("P", "件", "提", 5)])  # 1件=5提 => 1提=1/5件
        pieces, reason = to_pieces(graphs, "P", Fraction(3), "提")
        assert reason is None
        assert pieces == Fraction(3, 5)

    def test_unknown_product_is_pending(self):
        pieces, reason = to_pieces({}, "P", Fraction(1), "箱")
        assert pieces is None and "没有任何换算关系" in reason

    def test_unreachable_unit_is_pending(self):
        graphs = build_graphs([rel("P", "箱", "提", 12)])  # 提 与 件 不连通
        pieces, reason = to_pieces(graphs, "P", Fraction(1), "提")
        assert pieces is None and "没有换算路径" in reason
