"""签收对账 API 端到端测试:冻结、暂缓建议、核实保留、冲突退回、并发核实。"""
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

from fastapi.testclient import TestClient

from app.main import app

# 种子底表(A v1):P001 1箱=12提、1提=6件。P001 出库行可折成件。
BATCH = {
    "batch_no": "RCV-1",
    "lines": [
        # P001 出库行:5箱=360件、3提=18件、10件=10件,合计上限 388
        {"line_no": 1, "product": "P001", "quantity": 5, "unit": "箱"},
        {"line_no": 2, "product": "P001", "quantity": 3, "unit": "提"},
        {"line_no": 5, "product": "P001", "quantity": 10, "unit": "件"},
        # 另一商品,用于验证对账单按商品隔离
        {"line_no": 3, "product": "P003", "quantity": 2, "unit": "箱"},
    ],
}


def _open(client, receipt_lines, product="P001", batch_no="RCV-1"):
    return client.post(
        f"/batches/{batch_no}/reconciliations",
        json={"product": product, "receipt_lines": receipt_lines},
    )


def _setup(client, batch=None):
    resp = client.post("/batches", json=batch or BATCH)
    assert resp.status_code == 201, resp.text


class TestOpen:
    def test_open_freezes_outbound_pieces_and_versions(self, client):
        _setup(client)
        resp = _open(client, [
            {"line_no": 1, "pieces": 300, "candidates": [1]},
            {"line_no": 2, "pieces": 60, "candidates": [1, 2]},
            {"line_no": 3, "pieces": 18, "candidates": [2]},
            {"line_no": 4, "pieces": 10, "candidates": [5]},
        ])
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["relation_versions"] == {"A": 1, "B": 0}
        assert {l["line_no"]: l["pieces"] for l in body["outbound_lines"]} == {
            1: "360", 2: "18", 5: "10"}
        assert body["suggestion"]["deferred_line_nos"] == []
        assert body["revision"] == 0

    def test_receipt_line_limit_is_12(self, client):
        _setup(client)
        too_many = [{"line_no": i, "pieces": 1, "candidates": [5]} for i in range(1, 14)]
        assert _open(client, too_many).status_code == 422
        exact = [{"line_no": i, "pieces": 1, "candidates": [5]} for i in range(1, 13)]
        assert _open(client, exact).status_code == 201

    def test_one_reconciliation_per_batch_and_product(self, client):
        _setup(client)
        assert _open(client, [{"line_no": 1, "pieces": 10, "candidates": [5]}]).status_code == 201
        resp = _open(client, [{"line_no": 2, "pieces": 10, "candidates": [5]}])
        assert resp.status_code == 409

    def test_pending_outbound_line_blocks_opening(self, client):
        _setup(client)
        # 把 P001 的底表清空 => 该商品出库行无法折算,开单应退回
        client.post("/warehouses/A/conversion-table", json={"relations": []})
        resp = _open(client, [{"line_no": 1, "pieces": 1, "candidates": [1]}])
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "outbound_pending"
        assert {p["line_no"] for p in detail["pending_lines"]} == {1, 2}

    def test_candidate_must_belong_to_product_outbound_lines(self, client):
        _setup(client)
        resp = _open(client, [{"line_no": 1, "pieces": 1, "candidates": [3]}])  # 3 是 P003 行
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "invalid_candidate"

    def test_frozen_data_does_not_follow_later_table_change(self, client):
        _setup(client)
        opened = _open(client, [{"line_no": 1, "pieces": 360, "candidates": [1]}]).json()
        assert opened["outbound_lines"][0]["pieces"] == "360"
        # 换表后重新折算会不同,但对账单仍用冻结值
        client.post("/warehouses/A/conversion-table", json={"relations": [
            {"product": "P001", "from_unit": "箱", "to_unit": "件", "ratio": 100},
            {"product": "P003", "from_unit": "箱", "to_unit": "件", "ratio": 10},
        ]})
        again = client.get("/batches/RCV-1/reconciliations/P001").json()
        assert again["relation_versions"] == {"A": 1, "B": 0}
        assert {l["line_no"]: l["pieces"] for l in again["outbound_lines"]} == {
            1: "360", 2: "18", 5: "10"}


class TestSuggestion:
    def test_minimum_deferral_with_exact_fraction(self, client):
        _setup(client)
        # 出库上限:1=360,2=18,5=10。签收总量超过 360 只能靠 2/5 号,
        # 3 号只能去 1 号且 1 号已被占满 => 至少暂缓一条。
        body = _open(client, [
            {"line_no": 1, "pieces": 360, "candidates": [1]},
            {"line_no": 2, "pieces": 18, "candidates": [2]},
            {"line_no": 3, "pieces": 10, "candidates": [5]},
            {"line_no": 4, "pieces": 1, "candidates": [1]},
        ]).json()
        assert body["suggestion"]["deferred_line_nos"] == [4]
        assert body["suggestion"]["deferred_pieces"] == "1"
        assert body["suggestion"]["kept_line_nos"] == [1, 2, 3]
        used = {u["line_no"]: u["pieces"] for u in body["suggestion"]["outbound_used"]}
        assert used == {1: "360", 2: "18", 5: "10"}

    def test_split_allocation_is_returned_in_fractions(self, client):
        _setup(client)
        body = _open(client, [
            {"line_no": 1, "pieces": "0.5", "candidates": [1, 2]},
            {"line_no": 2, "pieces": "17.5", "candidates": [1, 2]},
        ]).json()
        assert body["suggestion"]["deferred_line_nos"] == []
        alloc = body["suggestion"]["allocations"]
        expected = {1: Fraction(1, 2), 2: Fraction(35, 2)}
        # 每条签收行的件数必须完整归属
        for receipt, amount in expected.items():
            total = sum(
                (Fraction(a["pieces"]) for a in alloc if a["receipt_line_no"] == receipt),
                Fraction(0),
            )
            assert total == amount
        used = {u["line_no"]: Fraction(u["pieces"]) for u in body["suggestion"]["outbound_used"]}
        assert used.get(1, Fraction(0)) <= 360 and used.get(2, Fraction(0)) <= 18


class TestVerify:
    def _open_two(self, client):
        # 出库 5 号上限 10:两条签收行都只能去 5 号,各 6 件,合计 12 > 10
        _setup(client)
        return _open(client, [
            {"line_no": 1, "pieces": 6, "candidates": [5]},
            {"line_no": 2, "pieces": 6, "candidates": [5]},
        ]).json()

    def test_verify_keeps_lines_and_recomputes(self, client):
        self._open_two(client)
        before = client.get("/batches/RCV-1/reconciliations/P001").json()
        assert before["suggestion"]["deferred_line_nos"] == [2]
        resp = client.post("/batches/RCV-1/reconciliations/P001/verify", json={"line_nos": [2]})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["newly_verified"] == [2]
        assert body["reconciliation"]["suggestion"]["deferred_line_nos"] == [1]
        assert body["reconciliation"]["revision"] == 1

    def test_verified_lines_survive_later_suggestions(self, client):
        _setup(client)
        _open(client, [
            {"line_no": 1, "pieces": 6, "candidates": [5]},
            {"line_no": 2, "pieces": 6, "candidates": [5]},
        ])
        client.post("/batches/RCV-1/reconciliations/P001/verify", json={"line_nos": [1]})
        body = client.get("/batches/RCV-1/reconciliations/P001").json()
        states = {l["line_no"]: l["verified"] for l in body["receipt_lines"]}
        assert states[1] is True
        assert 1 not in body["suggestion"]["deferred_line_nos"]

    def test_infeasible_verification_rolls_back_and_returns_original(self, client):
        self._open_two(client)
        # 1、2 合计 12 > 10,无法同时归属 => 409,状态不变
        resp = client.post("/batches/RCV-1/reconciliations/P001/verify",
                           json={"line_nos": [1, 2]})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "verification_infeasible"
        rec = detail["reconciliation"]
        assert rec["revision"] == 0
        assert all(not l["verified"] for l in rec["receipt_lines"])
        # 原状态仍能出对账结果
        after = client.get("/batches/RCV-1/reconciliations/P001").json()
        assert after["suggestion"]["deferred_line_nos"] == [2]

    def test_verification_uses_submission_time_frozen_data(self, client):
        _setup(client)
        _open(client, [{"line_no": 1, "pieces": 360, "candidates": [1]}])
        # 换表把 P001 1箱 改成 100 件(若重新折算,360 就装不下)
        client.post("/warehouses/A/conversion-table", json={"relations": [
            {"product": "P001", "from_unit": "箱", "to_unit": "件", "ratio": 100},
            {"product": "P003", "from_unit": "箱", "to_unit": "件", "ratio": 10},
        ]})
        resp = client.post("/batches/RCV-1/reconciliations/P001/verify", json={"line_nos": [1]})
        assert resp.status_code == 200
        assert resp.json()["reconciliation"]["suggestion"]["deferred_line_nos"] == []

    def test_repeat_verification_counts_once(self, client):
        self._open_two(client)
        first = client.post("/batches/RCV-1/reconciliations/P001/verify",
                            json={"line_nos": [2]}).json()
        assert first["newly_verified"] == [2] and first["reconciliation"]["revision"] == 1
        second = client.post("/batches/RCV-1/reconciliations/P001/verify",
                             json={"line_nos": [2, 2]}).json()
        assert second["newly_verified"] == []
        assert second["already_verified"] == [2]
        assert second["reconciliation"]["revision"] == 1  # 未重复记账

    def test_unknown_receipt_line_rejected(self, client):
        self._open_two(client)
        resp = client.post("/batches/RCV-1/reconciliations/P001/verify",
                           json={"line_nos": [99]})
        assert resp.status_code == 422

    def test_batch_of_verifications_together(self, client):
        _setup(client)
        _open(client, [
            {"line_no": 1, "pieces": 6, "candidates": [5]},
            {"line_no": 2, "pieces": 4, "candidates": [5]},
        ])
        body = client.post("/batches/RCV-1/reconciliations/P001/verify",
                           json={"line_nos": [1, 2]}).json()
        assert body["newly_verified"] == [1, 2]
        assert body["reconciliation"]["suggestion"]["deferred_line_nos"] == []

    def test_concurrent_same_line_verification_counts_once(self, client):
        """两个请求同时核实同一行:都成功,但只记一次,revision 只加一。"""
        self._open_two(client)

        def verify(_):
            with TestClient(app) as c:
                return c.post("/batches/RCV-1/reconciliations/P001/verify",
                              json={"line_nos": [1]})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(verify, [0, 1]))
        assert [r.status_code for r in results] == [200, 200]
        newly_total = sum(len(r.json()["newly_verified"]) for r in results)
        already_total = sum(len(r.json()["already_verified"]) for r in results)
        assert newly_total == 1 and already_total == 1
        final = client.get("/batches/RCV-1/reconciliations/P001").json()
        assert final["revision"] == 1

    def test_concurrent_open_same_product_only_one_wins(self, client):
        _setup(client)

        def open_one(_):
            with TestClient(app) as c:
                return _open(c, [{"line_no": 1, "pieces": 1, "candidates": [5]}])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(open_one, [0, 1]))
        assert sorted(r.status_code for r in results) == [201, 409]

    def test_concurrent_verification_conflict(self, client):
        """并发核实互相争夺同一容量:至多一方成功,失败方退回且状态不变。"""
        self._open_two(client)

        def verify(nos):
            with TestClient(app) as c:
                return c.post("/batches/RCV-1/reconciliations/P001/verify",
                              json={"line_nos": nos})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(verify, [[1], [2]]))
        codes = sorted(r.status_code for r in results)
        # 两条各 6 件,5 号上限 10:1 与 2 不能同时核实 => 恰一成功一冲突
        assert codes == [200, 409]
        loser = next(r for r in results if r.status_code == 409)
        assert loser.json()["detail"]["error"] == "verification_infeasible"

        final = client.get("/batches/RCV-1/reconciliations/P001").json()
        verified = [l["line_no"] for l in final["receipt_lines"] if l["verified"]]
        assert len(verified) == 1  # 只有胜方那条
        assert final["revision"] == 1
        # 建议必须保留已核实行
        assert verified[0] not in final["suggestion"]["deferred_line_nos"]
