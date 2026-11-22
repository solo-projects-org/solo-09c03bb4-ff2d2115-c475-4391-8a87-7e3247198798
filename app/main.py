"""回收申报出库量换算底表 API(FastAPI 入口)。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from fractions import Fraction
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from . import db
from .converter import Relation, build_graphs, frac_str, to_pieces, validate_new_table
from .schemas import (
    BatchIn,
    BatchLineOut,
    BatchOut,
    BatchPiecesOut,
    BatchSummaryOut,
    ConflictBody,
    EffectiveTableOut,
    LinePiecesOut,
    RelationOut,
    TableOut,
    TableReplaceRequest,
)

Warehouse = Literal["A", "B"]
WAREHOUSES: tuple[str, str] = ("A", "B")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="回收申报出库量换算底表 API",
    version="1.0.0",
    description=(
        "两仓(A/B)各自维护包装换算底表(按商品填正整数比,如 1箱=12提)。"
        "换表按整表更换,提交时与另一仓现行关系合并校验:同一商品任意换算路径"
        "必须得出相同比例,矛盾则指出能证实矛盾的关系及来源仓并保留原表。"
        "出库批次按行(行号/商品/数量/单位)入库,查询时逐行精确折成件,"
        "换算不到的行标为待补;换表成功后已入库批次按新关系出数。"
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
