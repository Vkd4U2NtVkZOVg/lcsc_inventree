# lcsc2inventree

将[立创商城（LCSC）](https://www.lcsc.com)的商品信息自动导入到自托管的 [InvenTree](https://inventree.org) 库存管理系统。

受 [sparkmicro/Ki-nTree](https://github.com/sparkmicro/Ki-nTree) 启发，但聚焦在 **LCSC → InvenTree** 这一条数据通路，砍掉了 KiCad 集成和 GUI，提供一个轻量的 CLI + CSV 批量工具。

## 功能

- 🔍 输入 LCSC C-code（如 `C28323`）或商品 URL，自动从商品页 `ld+json` 抽取字段
- 🏷️ 三层自动分类匹配（直接映射 → Function Type → 模糊匹配），无须人工指定
- 📊 字段映射（YAML 驱动）+ 智能单位清洗（`10nF (X7R)`、`10kOhms`、尺寸归一化…）
- 🖼️ **自动上传图片**（`image_urls[0]` → `Part.image`），重复运行跳过（幂等），`--update` 强制重传
- 📦 **批量导入**：CSV 一列 C-code 即可
- 📦 **库存管理**：默认不建 StockItem（只建器件），`--stock` 选建库存；库存建议由用户手工管理
- ♻️ **幂等写入**（以 LCSC C-code 作为 IPN），重复执行安全
- 🛡️ 本地缓存（默认 7 天）+ 速率限制 + 失败重试

## 数据流

```
LCSC 商品页 ─(requests 解析 ld+json)─► LCSCPart
                                          │
                          categorizer ────┤  (三层匹配)
                          mapping  ───────┤  (字段清洗 + YAML 映射)
                                          ▼
                InvenTree SDK 写入 (Part + ManufacturerPart + SupplierPart
                                    + PriceBreaks + Parameters + 可选 StockItem)
```

## 默认行为

- **StockItem**：默认不建（只建器件），用 `--stock` 选建库存；建议由用户手工管理
- **PartParameter**：InvenTree 1.2.6+ 与 SDK 不兼容时会跳过（不影响 Part/SupplierPart 写入）
- **图片上传**：默认开启；重复运行跳过；`LCSC_UPLOAD_IMAGE=false` 关闭
- **LCSC_BASE_URL**：默认国际版 (`www.lcsc.com`)；国内版需设置 `LCSC_BASE_URL=item.szlcsc.com`

## 字段映射

| LCSC 字段 | InvenTree 目标 |
|-----------|----------------|
| `sku` (C-code) | `Part.IPN` + `SupplierPart.SKU` |
| `mpn` | `ManufacturerPart.MPN` |
| `brand.name` | `Manufacturer.name` |
| `description` | `Part.description` |
| `category` (X/Y) | `PartCategory` 按 `/` 分层 |
| `additionalProperty[*]` | `PartParameter[*]`（按类别 field_map.yaml） |
| `offers.price` | `SupplierPriceBreak` |
| `offers.inventoryLevel` | （可选）`StockItem.quantity` |
| `subjectOf.url` | `Part.notes` Markdown |
| `image[0]` | `Part.image` 上传（自动幂等跳过；`LCSC_UPLOAD_IMAGE=false` 关闭） |

## 安装

```bash
# 推荐：使用 uv
uv tool install .

# 或：pipx
pipx install .

# 或：开发模式
git clone <repo> ~/lcsc2inventree
cd ~/lcsc2inventree
uv pip install -e ".[dev]"
```

> 📘 **Windows 用户**：请参阅 [docs/WINDOWS.md](docs/WINDOWS.md) — 涵盖 PowerShell/CMD 区别、UTF-8、路径分隔符、长路径限制等坑。
>
> 🌐 **国内立创商城（item.szlcsc.com）**：请参阅 [docs/LCSC_CN_VS_INTL.md](docs/LCSC_CN_VS_INTL.md) — 国内/国际版差异、URL 模式、ld+json 结构差异与限制。

## 配置

```bash
cp .env.example .env
vim .env   # 填写 INVENTREE_URL 和 INVENTREE_TOKEN
```

取得 token：

```bash
curl -X POST -u 'username:password' https://inventree.example.com/api/user/me/token/
```

## 用法

### 单个导入

```bash
lcsc2inv import C28323
lcsc2inv import https://www.lcsc.com/product-detail/C28323.html --dry-run
lcsc2inv import C28323 --update      # 强制更新已存在 Part
```

### 批量导入（CSV）

```csv
# bom.csv
lcsc_code,quantity,note
C28323,2000,主电源去耦
C191386,500,
C146404,,
```

```bash
# 默认只建器件，不建 StockItem（库存由用户手工管理）
lcsc2inv batch examples/bom.csv

# 需建库存？加 --stock
lcsc2inv batch bom.csv --stock --workers 4
```

### StockItem 管理

```bash
# 导入时默认不建 StockItem
lcsc2inv import C28323

# 需要建库存？加 --stock 和 --qty
lcsc2inv import C28323 --stock --qty 1000

# 在 InvenTree Web UI 中可以随时手动添加 StockItem（推荐）
```

### 健康检查

```bash
lcsc2inv doctor    # 验证 InvenTree 连通 + 权限
```

## 项目结构

```
lcsc2inventree/
├── lcsc2inv/
│   ├── lcsc_client.py        # requests + ld+json + 缓存
│   ├── lcsc_models.py        # Pydantic LCSCPart
│   ├── categorizer.py        # 三层分类匹配
│   ├── mapping.py            # 字段映射 + clean_parameter_value
│   ├── inventree_writer.py   # SDK 幂等 upsert
│   ├── cli.py                # Click CLI
│   └── config.py             # 环境变量 / YAML 加载
├── config/
│   ├── lcsc_categories.yaml  # LCSC 类目 → InvenTree 类别
│   └── field_map.yaml        # LCSC 字段 → InvenTree 参数
└── tests/                    # pytest + 离线 fixtures
```

## 风险与免责

- LCSC 没有公开 API，本工具抓取商品页 SSR HTML 中的 `ld+json` 块。**前端改版可能使解析失败**，所有选择器集中在 `lcsc_client.py`。
- 速率限制默认 1 req/s，请勿无限制并发抓取，否则 IP 会被 LCSC 临时封禁。
- 仅供个人 / 内部库存管理用途，商业用途请自行评估合规风险。

## 致谢

- [sparkmicro/Ki-nTree](https://github.com/sparkmicro/Ki-nTree) — 整体架构灵感
- [chenfenghao/lcsc_inventree](https://github.com/chenfenghao/lcsc_inventree) — 路径可行性验证
- [inventree-python](https://github.com/inventree/inventree-python) — 官方 SDK（pip 包名 `inventree`）