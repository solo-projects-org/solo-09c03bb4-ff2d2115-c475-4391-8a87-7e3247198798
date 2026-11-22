"""请求/响应模型(Pydantic),同时驱动 Swagger 文档。"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_RATIO = 1_000_000_000_000
MAX_QUANTITY = Decimal("1000000000000000")


class RelationIn(BaseModel):
    """一条换算关系:1 from_unit = ratio to_unit(ratio 为正整数)。"""

    product: str = Field(min_length=1, max_length=64, examples=["P001"], description="商品编码")
    from_unit: str = Field(min_length=1, max_length=16, examples=["箱"], description="源单位")
    to_unit: str = Field(min_length=1, max_length=16, examples=["提"], description="目标单位")
    ratio: int = Field(gt=0, le=MAX_RATIO, examples=[12], description="正整数比:1 源单位 = ratio 目标单位")

    @model_validator(mode="after")
    def _units_differ(self) -> "RelationIn":
        if self.from_unit == self.to_unit:
            raise ValueError("from_unit 与 to_unit 不能相同")
        return self


class TableReplaceRequest(BaseModel):
    """整表更换某仓的换算关系;提交空表即清空本仓关系。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "relations": [
                        {"product": "P001", "from_unit": "箱", "to_unit": "提", "ratio": 12},
                        {"product": "P001", "from_unit": "提", "to_unit": "件", "ratio": 6},
                    ]
                }
            ]
        }
    )

    relations: list[RelationIn] = Field(default_factory=list, description="本仓新的全部换算关系")


class RelationOut(BaseModel):
    product: str
    from_unit: str
    to_unit: str
    ratio: int
    source_warehouse: str = Field(description="来源仓:A / B;冲突详情中本次提交的关系会标注「(本次提交)」")


class TableOut(BaseModel):
    warehouse: str
    version: int = Field(description="当前生效版本号,0 表示尚未提交过")
    relation_count: int
    relations: list[RelationOut]


class EffectiveTableOut(BaseModel):
    versions: dict[str, int] = Field(description="两仓各自当前生效版本号")
    relations: list[RelationOut] = Field(description="两仓合并后的全部现行关系,每条带来源仓")


class ConflictInfo(BaseModel):
    submitted_relation: RelationOut = Field(description="本次提交中被证伪的关系")
    implied_ratio: str = Field(description="现行关系推出的 1 from_unit = implied_ratio to_unit(精确值)")
    proving_relations: list[RelationOut] = Field(description="能证实矛盾的现行关系链及各自来源仓")


class ConflictBody(BaseModel):
    error: str
    message: str
    validated_against: dict[str, int] = Field(description="校验时两仓的现行版本号(提交时核准)")
    conflicts: list[ConflictInfo]


class BatchLineIn(BaseModel):
    line_no: int = Field(gt=0, examples=[1], description="行号,批次内唯一")
    product: str = Field(min_length=1, max_length=64, examples=["P001"], description="商品编码")
    quantity: Decimal = Field(gt=0, le=MAX_QUANTITY, examples=["5"], description="数量(正数,精确存储)")
    unit: str = Field(min_length=1, max_length=16, examples=["箱"], description="单位,如 箱/提/件")


class BatchIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "batch_no": "OUT-20261122-001",
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
            ]
        }
    )

    batch_no: str = Field(min_length=1, max_length=64, description="出库批次号,全局唯一")
    lines: list[BatchLineIn] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def _unique_line_no(self) -> "BatchIn":
        line_nos = [line.line_no for line in self.lines]
        if len(line_nos) != len(set(line_nos)):
            raise ValueError("同一批次内 line_no 不能重复")
        return self


class BatchLineOut(BaseModel):
    line_no: int
    product: str
    quantity: str
    unit: str


class BatchOut(BaseModel):
    batch_no: str
    created_at: str
    lines: list[BatchLineOut]


class BatchSummaryOut(BaseModel):
    batch_no: str
    created_at: str
    line_count: int


class LinePiecesOut(BaseModel):
    line_no: int
    product: str
    quantity: str
    unit: str
    status: str = Field(description="已换算 / 待补")
    pieces: str | None = Field(default=None, description="折成件的精确数量(整数或分数),待补时为 null")
    reason: str | None = Field(default=None, description="待补原因")


class BatchPiecesOut(BaseModel):
    batch_no: str
    relation_versions: dict[str, int] = Field(description="本次查询所用的两仓底表版本(同一快照)")
    total_pieces: str = Field(description="已换算行合计(精确值)")
    lines: list[LinePiecesOut]
