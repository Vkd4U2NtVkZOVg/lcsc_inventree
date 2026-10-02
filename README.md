# lcsc2inventree

将[立创商城（LCSC）](https://www.lcsc.com)的商品信息自动导入到自托管的 [InvenTree](https://inventree.org) 库存管理系统。

受 [sparkmicro/Ki-nTree](https://github.com/sparkmicro/Ki-nTree) 启发，但聚焦在 **LCSC → InvenTree** 这一条数据通路，砍掉了 KiCad 集成和 GUI，提供一个轻量的 CLI + CSV 批量工具。

> [!IMPORTANT]
> **本项目由 AI 全程创建**（代码、文档、测试均由 AI 助手生成），作者未做人工审查，
> **不提供任何形式的技术支持**。请自行评估代码质量与安全性后谨慎使用，
> 使用产生的任何后果自负。Issue / PR 未必有人处理，请酌情自取。

## 功能

- 🔍 输入 LCSC C-code（如 `C28323`）或商品 URL，自动从商品页 `ld+json` 抽取字段
- 🏷️ 三层自动分类匹配（直接映射 → Function Type → 模糊匹配），无须人工指定
- 📊 字段映射（YAML 驱动）+ 智能单位清洗（`10nF (X7R)`、`10kOhms`、尺寸归一化…）
- 🖼️ **自动上传图片**（`image_urls[0]` → `Part.image`），重复运行跳过（幂等），`--update` 强制重传
- 📦 **批量导入**：CSV 一列 C-code 即可
- 📦 **库存管理**：默认不建 StockItem（只建器件），`--stock` 选建库存；库存建议由用户手工管理
- ♻️ **幂等写入**（以 LCSC C-code 作为 IPN），重复执行安全
- 🛡️ 本地缓存（默认 7 天）+ 速率限制 + 失败重试
- 💾 **InvenTree 数据库一键备份**：Web「备份」页一键进入 InvenTree 容器执行原生备份，dump 全库（含条码/二维码绑定表 `barcode_barcode`）与 media，产物落到容器 `/backup`（映射到宿主机 `./backup`），可随时取走或在线下载

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

## Web 界面

除 CLI 外，项目自带一个 **Flask Web 服务**，把抓取 + 写入逻辑通过 HTTP 暴露出来，并提供一个简单的单页中文界面：

- **单条导入**：输入 C-code 或商品 URL，一键导入
- **批量导入**：上传 CSV（`lcsc_code` 列，可选 `quantity` / `note`），逐行导入并返回结果
- **状态检查**：验证 InvenTree 连通性与配置
- **缓存管理**：查看 / 清空 LCSC 本地缓存
- **备份**：一键备份 InvenTree 数据库（含条码/二维码绑定表）与 media，可下载或从宿主机取走

### 本地运行

```bash
uv sync --extra web          # 或 pip install .[web]
lcsc2inv-web                 # 监听 0.0.0.0:8080
# 浏览器打开 http://localhost:8080
```

API 端点：`GET /`、`POST /api/import`、`POST /api/batch`、`GET /api/doctor`、`GET /api/cache`、`POST /api/cache/clear`、`POST /api/backup`、`GET /api/backup`、`GET /api/backup/<快照>/<文件>`。

> ⚠️ Web 层未内置登录鉴权（按需可加）。它具备**写入 InvenTree** 的权限，请仅在可信内网使用，或前置反向代理做鉴权后再暴露到公网。

## Docker 部署

将 Web 服务打包成镜像，方便部署到服务器：

```bash
docker compose up -d --build
# 或手动：
docker build -t lcsc2inventree-web .
docker run -d --name lcsc2inventree-web -p 8080:8080 \
  -e INVENTREE_URL=https://inventree.example.com \
  -e INVENTREE_TOKEN=your_token \
  -v lcsc-cache:/var/lib/lcsc2inventree/cache \
  lcsc2inventree-web
```

关键环境变量（详见 `docker-compose.yml` 与 `.env.example`）：

| 变量 | 说明 |
|------|------|
| `INVENTREE_URL` / `INVENTREE_TOKEN` | InvenTree API 地址（`/api` 结尾）与 Token |
| `LCSC_CACHE_DIR` | 缓存目录（镜像内 `/var/lib/lcsc2inventree/cache`，建议挂卷持久化） |
| `LCSC_CONFIG_DIR` | YAML 配置目录（默认镜像内 `/opt/lcsc2inventree/config`） |
| `LCSC_UPLOAD_IMAGE` / `DRY_RUN` / `CATEGORY_MATCH_RATIO_LIMIT` | 行为开关，与 CLI 一致 |
| `INVENTREE_BACKUP_CONTAINER` | InvenTree 容器名（一键备份必填，`docker ps` 查看） |
| `INVENTREE_BACKUP_CMD` / `INVENTREE_BACKUP_SRC_DIR` / `INVENTREE_BACKUP_STORAGE` | 容器内备份命令、产物目录、本容器暴露目录（一般用默认值） |

容器默认以**单 worker**（gunicorn `-w 1`）运行，因为 InvenTree SDK 非线程安全且 LCSC 有 1 req/s 限速，多 worker 会导致限速竞争与重复抓取。

### InvenTree 数据库一键备份

「备份」页的一键备份通过宿主机 `docker.sock` 进入 InvenTree 容器执行其原生备份命令（默认 `invoke backup`），dump 整个数据库（**含条码 / 二维码绑定表 `barcode_barcode`**）与 media 附件，再把产物拷进本容器 `/backup`（已 bind-mount 到宿主机 `./backup`，可直接取走），也支持在页面里下载。

**前提：**
- InvenTree 也是宿主机 Docker 部署；
- `docker-compose.yml` 已挂载 `docker.sock` 与 `./backup:/backup`（无需额外改，但需在 `.env` 填 `INVENTREE_BACKUP_CONTAINER`）；
- 宿主机 `/var/run/docker.sock` 需对容器内用户（uid 10001）可读写——若不满足，请调整 socket 权限（如 `chmod 666`）或把容器用户加入 docker 组。

> ⚠️ 挂载 `docker.sock` 等于把宿主机 Docker 权限交给容器，仅限可信部署使用。

## 项目结构

```
lcsc2inventree/
├── lcsc2inv/
│   ├── lcsc_client.py        # requests + ld+json + 缓存
│   ├── lcsc_models.py        # Pydantic LCSCPart
│   ├── categorizer.py        # 三层分类匹配
│   ├── mapping.py            # 字段映射 + clean_parameter_value
│   ├── inventree_writer.py   # SDK 幂等 upsert
│   ├── client.py             # 构造 InvenTree API（CLI / Web 共用）
│   ├── backup.py             # InvenTree 数据库一键备份（docker.sock 交互）
│   ├── web.py                # Flask Web 服务
│   ├── cli.py                # Click CLI
│   └── config.py             # 环境变量 / YAML 加载
├── templates/
│   └── index.html            # Web 单页界面
├── config/
│   ├── lcsc_categories.yaml  # LCSC 类目 → InvenTree 类别
│   └── field_map.yaml        # LCSC 字段 → InvenTree 参数
├── Dockerfile                # 容器镜像
├── docker-compose.yml        # 一键部署
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