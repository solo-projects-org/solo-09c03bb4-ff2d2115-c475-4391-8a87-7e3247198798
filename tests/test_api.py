"""API 端到端测试:换表校验、冲突保留原表、并发换表、按批折算。"""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app

# 种子底表(A 仓 v1):P001 1箱=12提、1提=6件;P003 1箱=10件。B 仓为空。

DEMO_BATCH = {
    "batch_no": "OUT-1",
    "lines": [
        {"line_no": 1, "product": "P001", "quantity": 5, "unit": "箱"},
        {"line_no": 2, "product": "P001", "quantity": 3, "unit": "提"},
        {"line_no": 3, "product": "P003", "quantity": 2, "unit": "箱"},
        {"line_no": 4, "product": "P002", "quantity": 7, "unit": "箱"},
        {"line_no": 5, "product": "P001", "quantity": 10, "unit": "件"},
        {"line_no": 6, "product": "P004", "quantity": 3, "unit": "提"},
        {"line_no": 7, "product": "P999", "quantity": 1, "unit": "箱"},
    ],
}


def _create_batch(client, batch=DEMO_BATCH):
    resp = client.post("/batches", json=batch)
    assert resp.status_code == 201, resp.text
    return resp


def _pieces_by_line(resp):
    return {line["line_no"]: line for line in resp.json()["lines"]}


class TestBatchAndPieces:
    def test_query_before_replace(self, client):
        _create_batch(client)
        resp = client.get("/batches/OUT-1/pieces")
        assert resp.status_code == 200
        body = resp.json()
        assert body["relation_versions"] == {"A": 1, "B": 0}
        lines = _pieces_by_line(resp)
        assert lines[1]["status"] == "已换算" and lines[1]["pieces"] == "360"  # 5箱*12提*6件
        assert lines[2]["pieces"] == "18"  # 3提*6件
        assert lines[3]["pieces"] == "20"  # 2箱*10件
        assert lines[4]["status"] == "待补"  # P002 无关系
        assert lines[5]["pieces"] == "10"  # 件本身无需关系
        assert lines[6]["status"] == "待补"  # P004 无关系
        assert lines[7]["status"] == "待补"  # 未知商品
        assert body["total_pieces"] == "408"  # 360+18+20+10,待补行不计入

    def test_decimal_quantity_is_exact(self, client):
        _create_batch(client, {"batch_no": "OUT-D", "lines": [
            {"line_no": 1, "product": "P001", "quantity": "0.5", "unit": "箱"},
        ]})
        lines = _pieces_by_line(client.get("/batches/OUT-D/pieces"))
        assert lines[1]["pieces"] == "36"

    def test_duplicate_batch_no_rejected(self, client):
        _create_batch(client)
        resp = client.post("/batches", json=DEMO_BATCH)
        assert resp.status_code == 409

    def test_batch_not_found(self, client):
        assert client.get("/batches/NOPE/pieces").status_code == 404

    def test_invalid_input_rejected(self, client):
        assert client.post("/batches", json={"batch_no": "X", "lines": [
            {"line_no": 1, "product": "P", "quantity": 0, "unit": "箱"}]}).status_code == 422
        assert client.post("/batches", json={"batch_no": "X", "lines": [
            {"line_no": 1, "product": "P", "quantity": 1, "unit": "箱"},
            {"line_no": 1, "product": "P", "quantity": 2, "unit": "箱"}]}).status_code == 422
        assert client.post("/warehouses/A/conversion-table", json={"relations": [
            {"product": "P", "from_unit": "箱", "to_unit": "件", "ratio": 0}]}).status_code == 422
        assert client.post("/warehouses/A/conversion-table", json={"relations": [
            {"product": "P", "from_unit": "箱", "to_unit": "箱", "ratio": 1}]}).status_code == 422


class TestTableReplace:
    def test_conflict_points_out_proving_relations_and_keeps_original(self, client):
        resp = client.post("/warehouses/B/conversion-table", json={"relations": [
            {"product": "P001", "from_unit": "提", "to_unit": "件", "ratio": 5}]})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["validated_against"] == {"A": 1, "B": 0}
        (conflict,) = detail["conflicts"]
        assert conflict["submitted_relation"]["ratio"] == 5
        assert conflict["submitted_relation"]["source_warehouse"] == "B(本次提交)"
        assert conflict["implied_ratio"] == "6"
        assert [(r["ratio"], r["source_warehouse"]) for r in conflict["proving_relations"]] == [(6, "A")]
        # 原表保留:A 仍是 v1,B 仍无表
        assert client.get("/warehouses/A/conversion-table").json()["version"] == 1
        assert client.get("/warehouses/B/conversion-table").json()["version"] == 0

    def test_transitive_conflict_reports_path(self, client):
        resp = client.post("/warehouses/B/conversion-table", json={"relations": [
            {"product": "P001", "from_unit": "箱", "to_unit": "件", "ratio": 100}]})
        assert resp.status_code == 409
        (conflict,) = resp.json()["detail"]["conflicts"]
        assert conflict["implied_ratio"] == "72"
        assert {(r["from_unit"], r["to_unit"], r["ratio"]) for r in conflict["proving_relations"]} == {
            ("箱", "提", 12), ("提", "件", 6)}

    def test_replace_changes_stored_batches_and_fraction_is_exact(self, client):
        _create_batch(client)
        before = _pieces_by_line(client.get("/batches/OUT-1/pieces"))
        assert before[3]["pieces"] == "20" and before[4]["status"] == "待补"

        # B 仓建表:与 A 现行关系一致(1箱=72件),并补充 P002、P004
        resp = client.post("/warehouses/B/conversion-table", json={"relations": [
            {"product": "P001", "from_unit": "箱", "to_unit": "件", "ratio": 72},
            {"product": "P002", "from_unit": "箱", "to_unit": "件", "ratio": 24},
            {"product": "P004", "from_unit": "件", "to_unit": "提", "ratio": 5},
        ]})
        assert resp.status_code == 200 and resp.json()["version"] == 1

        # A 仓整表更换:P001 保持与 B 一致(12*6=72),P003 改为 1箱=20件
        resp = client.post("/warehouses/A/conversion-table", json={"relations": [
            {"product": "P001", "from_unit": "箱", "to_unit": "提", "ratio": 12},
            {"product": "P001", "from_unit": "提", "to_unit": "件", "ratio": 6},
            {"product": "P003", "from_unit": "箱", "to_unit": "件", "ratio": 20},
        ]})
        assert resp.status_code == 200 and resp.json()["version"] == 2

        # 已入库批次按新关系出数
        resp = client.get("/batches/OUT-1/pieces")
        assert resp.json()["relation_versions"] == {"A": 2, "B": 1}
        after = _pieces_by_line(resp)
        assert after[1]["pieces"] == "360"  # 两条路径同为 72,一致
        assert after[3]["pieces"] == "40"   # P003 换表后 2箱*20件
        assert after[4]["pieces"] == "168"  # P002 有了关系 7箱*24件
        assert after[6]["pieces"] == "3/5"  # 1件=5提 => 3提=3/5件,精确分数

    def test_replace_with_empty_table_clears_warehouse(self, client):
        _create_batch(client)
        resp = client.post("/warehouses/A/conversion-table", json={"relations": []})
        assert resp.status_code == 200 and resp.json()["relation_count"] == 0
        lines = _pieces_by_line(client.get("/batches/OUT-1/pieces"))
        assert lines[1]["status"] == "待补"  # P001 关系已清空
        assert lines[5]["pieces"] == "10"    # 件本身仍可直接出数

    def test_effective_table_merges_both_warehouses(self, client):
        client.post("/warehouses/B/conversion-table", json={"relations": [
            {"product": "P001", "from_unit": "箱", "to_unit": "件", "ratio": 72}]})
        body = client.get("/conversion-table/effective").json()
        assert body["versions"] == {"A": 1, "B": 1}
        sources = {(r["product"], r["from_unit"], r["to_unit"]): r["source_warehouse"] for r in body["relations"]}
        assert sources[("P001", "箱", "提")] == "A"
        assert sources[("P001", "箱", "件")] == "B"


class TestConcurrentReplace:
    def test_concurrent_conflicting_replace_exactly_one_wins(self, client):
        """两仓同时提交互相矛盾的表:后到者按先到者已提交的现行关系核准,恰一成功。"""
        def submit(warehouse, ratio):
            # 每个线程独立的 TestClient,模拟并发请求
            with TestClient(app) as c:
                return c.post(f"/warehouses/{warehouse}/conversion-table", json={"relations": [
                    {"product": "P009", "from_unit": "箱", "to_unit": "件", "ratio": ratio}]})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: submit(*args), [("A", 10), ("B", 12)]))
        assert sorted(r.status_code for r in results) == [200, 409]

        # 失败方看到的矛盾证明来自胜方仓库,且最终生效关系与胜方一致
        loser = next(r for r in results if r.status_code == 409)
        (conflict,) = loser.json()["detail"]["conflicts"]
        winner_ratio = 10 if conflict["submitted_relation"]["ratio"] == 12 else 12
        assert conflict["implied_ratio"] == str(winner_ratio)
        assert conflict["proving_relations"][0]["source_warehouse"] in ("A", "B")
        effective = client.get("/conversion-table/effective").json()
        p009 = [r for r in effective["relations"] if r["product"] == "P009"]
        assert len(p009) == 1 and p009[0]["ratio"] == winner_ratio
