"""回收申报出库量换算底表 API(FastAPI 入口)。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from fractions import Fraction
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from . import db
from .converter import Relation, build_graphs, frac_str, to_pieces, validate_new_table
from .reconcile import (
    ReceiptLine,
    ReconcileResult,
    feasible,
    reconcile,
)
from .schemas import (
    AllocationOut,
    BatchIn,
    BatchLineOut,
    BatchOut,
    BatchPiecesOut,
    BatchSummaryOut,
    ConflictBody,
    EffectiveTableOut,
    FrozenOutboundLineOut,
    LinePiecesOut,
    OutboundUsedOut,
    ReceiptLineOut,
    ReconciliationOpenRequest,
    ReconciliationOut,
    RelationOut,
    SuggestionOut,
    TableOut,
    TableReplaceRequest,
    VerificationConflictBody,
    VerifyOut,
    VerifyRequest,
)

Warehouse = Literal["A", "B"]
WAREHOUSES: tuple[str, str] = ("A", "B")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="回收申报出库量换算底表与签收对账 API",
    version="1.1.0",
    description=(
        "两仓(A/B)各自维护包装换算底表(按商品填正整数比,如 1箱=12提)。"
        "换表按整表更换,提交时与另一仓现行关系合并校验:同一商品任意换算路径"
        "必须得出相同比例,矛盾则指出能证实矛盾的关系及来源仓并保留原表。"
        "出库批次按行(行号/商品/数量/单位)入库,查询时逐行精确折成件,"
        "换算不到的行标为待补;换表成功后已入库批次按新关系出数。"
        "签收对账按「批次 × 商品」开单:冻结该商品出库行折件数与两仓底表版号,"
        "在候选出库行容量约束下求出最少要暂缓入账的签收行,并给出其余行的完整"
        "精确分数归属;核对员可将签收行标为已核实,之后的建议必须保留这些行。"
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


def _rel_out(rel: Relation) -> dict:
    return {
        "product": rel.product,
        "from_unit": rel.from_unit,
        "to_unit": rel.to_unit,
        "ratio": rel.ratio,
        "source_warehouse": rel.source,
    }


def _row_to_relation(row, source: str | None = None) -> Relation:
    return Relation(
        product=row["product"],
        from_unit=row["from_unit"],
        to_unit=row["to_unit"],
        ratio=row["ratio"],
        source=source if source is not None else row["warehouse"],
    )


# ---------------------------------------------------------------- 换算底表


@app.get(
    "/warehouses/{warehouse}/conversion-table",
    response_model=TableOut,
    tags=["换算底表"],
    summary="查看某仓当前生效的换算底表",
)
def get_table(warehouse: Warehouse) -> TableOut:
    with db.read_tx() as conn:
        rows = db.get_active_relations(conn, warehouse)
        version = db.get_active_version(conn, warehouse)
    return TableOut(
        warehouse=warehouse,
        version=version,
        relation_count=len(rows),
        relations=[RelationOut(**_rel_out(_row_to_relation(r))) for r in rows],
    )


@app.post(
    "/warehouses/{warehouse}/conversion-table",
    response_model=TableOut,
    responses={409: {"model": ConflictBody, "description": "与另一仓现行关系矛盾,原表已保留"}},
    tags=["换算底表"],
    summary="整表更换某仓换算底表(与另一仓现行关系合并校验)",
)
def replace_table(warehouse: Warehouse, req: TableReplaceRequest) -> TableOut:
    other: Warehouse = "B" if warehouse == "A" else "A"
    # BEGIN IMMEDIATE 写事务:并发换表在此串行化,
    # 读到的一定是另一仓已提交的现行关系(按提交时核准)。
    with db.write_tx() as conn:
        other_rows = db.get_active_relations(conn, other)
        versions = {w: db.get_active_version(conn, w) for w in WAREHOUSES}
        existing = [_row_to_relation(r) for r in other_rows]
        submitted = [
            Relation(x.product, x.from_unit, x.to_unit, x.ratio, f"{warehouse}(本次提交)")
            for x in req.relations
        ]
        conflicts = validate_new_table(existing, submitted)
        if conflicts:
            # 抛异常即回滚,原表保留
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "conversion_conflict",
                    "message": "提交的换算关系与现行关系矛盾,原表已保留",
                    "validated_against": versions,
                    "conflicts": [
                        {
                            "submitted_relation": _rel_out(c.submitted),
                            "implied_ratio": frac_str(c.implied_ratio),
                            "proving_relations": [_rel_out(r) for r in c.proving],
                        }
                        for c in conflicts
                    ],
                },
            )
        version = db.replace_active_table(
            conn, warehouse, [(x.product, x.from_unit, x.to_unit, x.ratio) for x in req.relations]
        )
        rows = db.get_active_relations(conn, warehouse)
    return TableOut(
        warehouse=warehouse,
        version=version,
        relation_count=len(rows),
        relations=[RelationOut(**_rel_out(_row_to_relation(r))) for r in rows],
    )


@app.get(
    "/conversion-table/effective",
    response_model=EffectiveTableOut,
    tags=["换算底表"],
    summary="查看两仓合并后的现行换算关系(查询折算实际使用的底表)",
)
def effective_table() -> EffectiveTableOut:
    with db.read_tx() as conn:
        rows = db.get_active_relations(conn)
        versions = {w: db.get_active_version(conn, w) for w in WAREHOUSES}
    return EffectiveTableOut(
        versions=versions,
        relations=[RelationOut(**_rel_out(_row_to_relation(r))) for r in rows],
    )


# ---------------------------------------------------------------- 出库批次


@app.post(
    "/batches",
    status_code=201,
    response_model=BatchOut,
    tags=["出库批次"],
    summary="接收一个出库批次(行号/商品/数量/单位)",
)
def create_batch(req: BatchIn) -> BatchOut:
    lines = [
        (line.line_no, line.product, format(line.quantity, "f"), line.unit) for line in req.lines
    ]
    with db.write_tx() as conn:
        if db.get_batch(conn, req.batch_no) is not None:
            raise HTTPException(
                status_code=409,
                detail={"error": "batch_exists", "message": f"批次「{req.batch_no}」已存在"},
            )
        created_at = db.create_batch(conn, req.batch_no, lines)
    return BatchOut(
        batch_no=req.batch_no,
        created_at=created_at,
        lines=[
            BatchLineOut(line_no=line.line_no, product=line.product, quantity=format(line.quantity, "f"), unit=line.unit)
            for line in req.lines
        ],
    )


@app.get("/batches", response_model=list[BatchSummaryOut], tags=["出库批次"], summary="列出全部批次")
def list_batches() -> list[BatchSummaryOut]:
    with db.read_tx() as conn:
        rows = db.list_batches(conn)
    return [BatchSummaryOut(batch_no=r["batch_no"], created_at=r["created_at"], line_count=r["line_count"]) for r in rows]


@app.get("/batches/{batch_no}", response_model=BatchOut, tags=["出库批次"], summary="查看批次原始行")
def get_batch(batch_no: str) -> BatchOut:
    with db.read_tx() as conn:
        batch = db.get_batch(conn, batch_no)
        if batch is None:
            raise HTTPException(status_code=404, detail={"error": "batch_not_found", "message": f"批次「{batch_no}」不存在"})
        lines = db.get_batch_lines(conn, batch["id"])
    return BatchOut(
        batch_no=batch["batch_no"],
        created_at=batch["created_at"],
        lines=[
            BatchLineOut(line_no=l["line_no"], product=l["product"], quantity=l["quantity"], unit=l["unit"])
            for l in lines
        ],
    )


@app.get(
    "/batches/{batch_no}/pieces",
    response_model=BatchPiecesOut,
    tags=["出库批次"],
    summary="按批查询:逐行折成件的精确数量,换算不到的行标为待补",
)
def batch_pieces(batch_no: str) -> BatchPiecesOut:
    # 单个读事务:批次行与两仓现行底表来自同一份已提交快照。
    with db.read_tx() as conn:
        batch = db.get_batch(conn, batch_no)
        if batch is None:
            raise HTTPException(status_code=404, detail={"error": "batch_not_found", "message": f"批次「{batch_no}」不存在"})
        lines = db.get_batch_lines(conn, batch["id"])
        rows = db.get_active_relations(conn)
        versions = {w: db.get_active_version(conn, w) for w in WAREHOUSES}
    graphs = build_graphs([_row_to_relation(r) for r in rows])
    out_lines: list[LinePiecesOut] = []
    total = Fraction(0)
    for line in lines:
        quantity = Fraction(line["quantity"])
        pieces, reason = to_pieces(graphs, line["product"], quantity, line["unit"])
        if pieces is None:
            out_lines.append(
                LinePiecesOut(
                    line_no=line["line_no"],
                    product=line["product"],
                    quantity=line["quantity"],
                    unit=line["unit"],
                    status="待补",
                    pieces=None,
                    reason=reason,
                )
            )
        else:
            total += pieces
            out_lines.append(
                LinePiecesOut(
                    line_no=line["line_no"],
                    product=line["product"],
                    quantity=line["quantity"],
                    unit=line["unit"],
                    status="已换算",
                    pieces=frac_str(pieces),
                    reason=None,
                )
            )
    return BatchPiecesOut(
        batch_no=batch_no,
        relation_versions=versions,
        total_pieces=frac_str(total),
        lines=out_lines,
    )


# ---------------------------------------------------------------- 签收对账


def _reconciliation_view(conn, row) -> ReconciliationOut:
    """按冻结数据现算建议,组装对账单视图(调用方需在同一读事务内)。"""
    outbound = db.get_reconciliation_outbound(conn, row["id"])
    receipt_rows = db.get_reconciliation_receipt_lines(conn, row["id"])
    candidates = db.get_reconciliation_candidates(conn, row["id"])
    batch = conn.execute("SELECT batch_no FROM batches WHERE id = ?", (row["batch_id"],)).fetchone()

    capacities = {r["line_no"]: Fraction(r["pieces"]) for r in outbound}
    lines = [
        ReceiptLine(
            line_no=r["line_no"],
            pieces=Fraction(r["pieces"]),
            candidates=tuple(candidates.get(r["line_no"], ())),
            verified=bool(r["verified"]),
        )
        for r in receipt_rows
    ]
    pieces_by_line = {line.line_no: line.pieces for line in lines}
    result: ReconcileResult = reconcile(lines, capacities)
    return ReconciliationOut(
        batch_no=batch["batch_no"],
        product=row["product"],
        created_at=row["created_at"],
        revision=row["revision"],
        relation_versions={"A": row["relation_a_version"], "B": row["relation_b_version"]},
        outbound_lines=[
            FrozenOutboundLineOut(line_no=r["line_no"], pieces=r["pieces"]) for r in outbound
        ],
        receipt_lines=[
            ReceiptLineOut(
                line_no=r["line_no"],
                pieces=r["pieces"],
                candidates=candidates.get(r["line_no"], []),
                verified=bool(r["verified"]),
                verified_at=r["verified_at"],
            )
            for r in receipt_rows
        ],
        suggestion=SuggestionOut(
            deferred_line_nos=list(result.deferred_line_nos),
            deferred_pieces=frac_str(
                sum((pieces_by_line[no] for no in result.deferred_line_nos), Fraction(0))
            ),
            kept_line_nos=list(result.kept_line_nos),
            allocations=[
                AllocationOut(
                    receipt_line_no=a.receipt_line_no,
                    outbound_line_no=a.outbound_line_no,
                    pieces=frac_str(a.pieces),
                )
                for a in result.allocations
            ],
            outbound_used=[
                OutboundUsedOut(line_no=line_no, pieces=frac_str(pieces))
                for line_no, pieces in result.outbound_used
            ],
        ),
    )


@app.post(
    "/batches/{batch_no}/reconciliations",
    status_code=201,
    response_model=ReconciliationOut,
    tags=["签收对账"],
    summary="开对账单:冻结所选商品各出库行折件数与两仓底表版号,给出最少暂缓行建议",
)
def open_reconciliation(batch_no: str, req: ReconciliationOpenRequest) -> ReconciliationOut:
    with db.write_tx() as conn:
        batch = db.get_batch(conn, batch_no)
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "batch_not_found", "message": f"批次「{batch_no}」不存在"},
            )
        if db.get_reconciliation(conn, batch["id"], req.product) is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "reconciliation_exists",
                    "message": f"批次「{batch_no}」商品「{req.product}」已有对账单,每批每种商品只开一单",
                },
            )

        # 冻结:所选商品各出库行折件数 + 两仓底表版号(同一写事务快照)。
        lines = db.get_batch_lines(conn, batch["id"])
        rows = db.get_active_relations(conn)
        versions = {w: db.get_active_version(conn, w) for w in WAREHOUSES}
        graphs = build_graphs([_row_to_relation(r) for r in rows])
        outbound: list[tuple[int, str]] = []
        frozen_pieces: dict[int, Fraction] = {}
        pending: list[dict] = []
        for line in lines:
            if line["product"] != req.product:
                continue
            pieces, reason = to_pieces(graphs, line["product"], Fraction(line["quantity"]), line["unit"])
            if pieces is None:
                pending.append({"line_no": line["line_no"], "reason": reason})
                continue
            outbound.append((line["line_no"], frac_str(pieces)))
            frozen_pieces[line["line_no"]] = pieces
        if pending:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "outbound_pending",
                    "message": f"商品「{req.product}」仍有出库行无法折算成件,不能开单;请补全换算底表后重试",
                    "pending_lines": pending,
                    "relation_versions": versions,
                },
            )
        if not outbound:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "no_outbound_lines",
                    "message": f"批次「{batch_no}」中没有商品「{req.product}」的可折算出库行",
                },
            )

        allowed = set(frozen_pieces)
        for line in req.receipt_lines:
            unknown = [c for c in line.candidates if c not in allowed]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "invalid_candidate",
                        "message": f"签收行 {line.line_no} 的候选出库行不属于本批次该商品的可折算行",
                        "invalid_candidates": sorted(set(unknown)),
                        "allowed_candidates": sorted(allowed),
                    },
                )

        reconciliation_id = db.create_reconciliation(
            conn,
            batch["id"],
            req.product,
            versions,
            outbound,
            [
                (line.line_no, format(line.pieces, "f"), False, sorted(set(line.candidates)))
                for line in req.receipt_lines
            ],
        )
        row = db.get_reconciliation_by_id(conn, reconciliation_id)
        return _reconciliation_view(conn, row)


@app.get(
    "/batches/{batch_no}/reconciliations/{product}",
    response_model=ReconciliationOut,
    tags=["签收对账"],
    summary="查看对账单与当前对账建议(按开单时冻结的数据)",
)
def get_reconciliation(batch_no: str, product: str) -> ReconciliationOut:
    with db.read_tx() as conn:
        batch = db.get_batch(conn, batch_no)
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "batch_not_found", "message": f"批次「{batch_no}」不存在"},
            )
        row = db.get_reconciliation(conn, batch["id"], product)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "reconciliation_not_found", "message": "对账单不存在"},
            )
        return _reconciliation_view(conn, row)


@app.post(
    "/batches/{batch_no}/reconciliations/{product}/verify",
    response_model=VerifyOut,
    responses={409: {"model": VerificationConflictBody, "description": "已核实行加本次核实行无法同时归属"}},
    tags=["签收对账"],
    summary="核实签收行:之后的建议必须保留这些行;冲突则整次退回",
)
def verify_receipt_lines(batch_no: str, product: str, req: VerifyRequest) -> VerifyOut:
    line_nos = sorted(set(req.line_nos))
    # BEGIN IMMEDIATE:并发核实串行化,后到者看到先到者已提交的核实状态。
    with db.write_tx() as conn:
        batch = db.get_batch(conn, batch_no)
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "batch_not_found", "message": f"批次「{batch_no}」不存在"},
            )
        row = db.get_reconciliation(conn, batch["id"], product)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "reconciliation_not_found", "message": "对账单不存在"},
            )
        existing = {r["line_no"] for r in db.get_reconciliation_receipt_lines(conn, row["id"])}
        missing = [n for n in line_nos if n not in existing]
        if missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unknown_receipt_line",
                    "message": "提交了对账单中不存在的签收行号",
                    "line_nos": missing,
                },
            )

        # 冻结数据(开单时的件数/候选/出库行)不变,核实时以它为准。
        outbound = db.get_reconciliation_outbound(conn, row["id"])
        receipt_rows = db.get_reconciliation_receipt_lines(conn, row["id"])
        candidates = db.get_reconciliation_candidates(conn, row["id"])
        capacities = {r["line_no"]: Fraction(r["pieces"]) for r in outbound}
        verified_now = set(line_nos)
        lines = [
            ReceiptLine(
                line_no=r["line_no"],
                pieces=Fraction(r["pieces"]),
                candidates=tuple(candidates.get(r["line_no"], ())),
                verified=bool(r["verified"]) or r["line_no"] in verified_now,
            )
            for r in receipt_rows
        ]
        # 已核实行 + 本次核实行不能同时归属 -> 整次退回(尚未写入,事务回滚),状态不变。
        if not feasible([line for line in lines if line.verified], capacities):
            body = _reconciliation_view(conn, row)  # 原状态(本次未落库)
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "verification_infeasible",
                    "message": "已核实行加上本次核实行无法同时归属,本次核实已退回,对账结果按原状态给出",
                    "reconciliation": body.model_dump(),
                },
            )

        newly, already = db.mark_verified(conn, row["id"], line_nos)
        if newly:
            db.bump_revision(conn, row["id"])
        fresh = db.get_reconciliation_by_id(conn, row["id"])
        body = _reconciliation_view(conn, fresh)
    return VerifyOut(newly_verified=newly, already_verified=already, reconciliation=body)
