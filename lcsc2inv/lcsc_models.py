"""LCSC 商品页 ld+json 抽取出的结构化数据模型。

设计原则：
- 字段命名贴近 Schema.org JSON-LD 原文，便于 `lcsc_client.parse_ldjson` 一对一映射。
- 所有可空字段默认 None，避免 Pydantic 强校验失败。
- 在 `property`（字段表中）保留原始 `unitText`（虽然实测为空，但防御未来扩展）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class Brand(BaseModel):
    """LCSC ld+json 中的 `brand` 对象。"""

    name: str


class PropertyValue(BaseModel):
    """LCSC `additionalProperty[]` 中的每一项规格参数。"""

    name: str = Field(..., description="例如 'Capacitance'、'Resistance'、'Tolerance'")
    value: str = Field(..., description="原始字符串，例如 '1uF'、'±10%'、'50V'")
    unit_text: str | None = Field(None, alias="unitText", description="原始 unitText（实测恒为空）")


class Offer(BaseModel):
    """LCSC ld+json 中的 `offers` 对象（库存 + 价格 + 卖家）。"""

    url: str | None = None
    price_currency: str = Field("USD", alias="priceCurrency")
    price: float | None = None
    availability: str | None = None  # 例如 "http://schema.org/InStock"
    inventory_level: int | None = Field(None, alias="inventoryLevel")
    seller_name: str | None = Field(None, alias="seller_name")  # 下方 Seller 抽取
    description: str | None = None

    @classmethod
    def from_raw(cls, raw: dict) -> "Offer":
        seller = raw.get("seller") or {}
        return cls(
            url=raw.get("url"),
            priceCurrency=raw.get("priceCurrency", "USD"),
            price=raw.get("price"),
            availability=raw.get("availability"),
            inventoryLevel=raw.get("inventoryLevel"),
            seller_name=seller.get("name"),
            description=raw.get("description"),
        )


class SubjectOf(BaseModel):
    """数据手册 / 关联文档（`subjectOf`）。"""

    name: str | None = None
    url: str | None = None


class LCSCPart(BaseModel):
    """LCSC 单个商品的完整数据模型（解析 ld+json 后得到）。"""

    sku: str = Field(..., description="LCSC C-code，例如 'C28323'")
    mpn: str | None = Field(None, description="Manufacturer Part Number")
    name: str | None = Field(None, description="ld+json 中的商品名")
    description: str | None = None
    brand: Brand | None = None
    category: str | None = Field(None, description="斜杠分隔，如 'Capacitors/Ceramic Capacitors'")
    image_urls: list[str] = Field(default_factory=list)
    additional_properties: list[PropertyValue] = Field(default_factory=list)
    offer: Offer | None = None
    datasheet_url: str | None = None
    page_url: str | None = None  # LCSC 商品详情页 URL（由 client 拼出）

    @property
    def lcsc_code(self) -> str:
        return self.sku

    @property
    def manufacturer_name(self) -> str | None:
        return self.brand.name if self.brand else None

    @property
    def category_parts(self) -> list[str]:
        if not self.category:
            return []
        return [p.strip() for p in self.category.split("/") if p.strip()]

    @property
    def category_top(self) -> str | None:
        return self.category_parts[0] if self.category_parts else None

    @property
    def category_sub(self) -> str | None:
        parts = self.category_parts
        return parts[1] if len(parts) > 1 else None

    def get_property(self, name: str) -> str | None:
        """按名获取第一个匹配的规格参数原始值（大小写不敏感）。"""
        n = name.lower()
        for p in self.additional_properties:
            if p.name.lower() == n:
                return p.value
        return None

    @property
    def datasheet_url_resolved(self) -> str | None:
        """优先取 `subjectOf.url`，否则根据 productCode 拼出标准 datasheet URL。"""
        if self.datasheet_url:
            return self.datasheet_url
        return f"https://datasheet.lcsc.com/datasheet/pdf/{self.sku}.pdf"