# lcsc2inventree 功能与架构文档

> 更新：2026-09-29。本文档总结 Web 工具的全部功能、关键设计决策、部署要点与
> 配套的 InvenTree BLE 定位插件，供后续维护参考。

## 1. 项目概览

将立创商城（LCSC）商品数据导入自托管 InvenTree，并提供一个覆盖日常仓库操作
的 Web 工作台（端口 `8095`）：

```
LCSC 商品页 ──► 抓取/解析 ──► 分类匹配 ──► 字段映射 ──► InvenTree 写入
                                    │
Web 工作台：导入 · 扫码(转移/盘点/扣减) · 条码查看/绑定 · 缓存 · 备份
```

- 源码结构：`lcsc2inv/`（CLI + Flask Web）、`lcsc2inv/templates/index.html`
  （单页前端）、`inventree-plugin-ble-locate/`（InvenTree 定位插件）；
- 部署：`docker compose up -d --build`，容器 `lcsc2inventree-web`，gunicorn 单
  worker（InvenTree SDK 非线程安全 + LCSC 限速，写操作经全局锁串行）。

## 2. Web 界面布局

左侧分组侧边栏（窄屏自动折叠为顶部横排）：

| 分组 | 页签 | 功能 |
|------|------|------|
| 导入 | 📥 单条导入 | 输入 C-code/URL 导入，支持预览、历史、库存码绑定 |
| | 🔄 更新现有 Part | 给已有 Part 按 LCSC 最新数据刷新所选字段（见下） |
| | 📋 批量导入 | CSV 逐条导入，可勾选行、重试失败项 |
| | 🧾 订单导入 | 上传立创订单 .xls，自动匹配/刷新/入库（见下） |
| 扫码库存 | 📦 扫码操作 | **三合一**：转移 / 盘点 / 扣减，一个输入框连续扫码（见下） |
| | 🏷️ 条码查看 | 双码显示 / 绑定 / 缓存管理（见 §4） |
| 系统 | 🩺 状态检查 | 连通性 + 配置检查 + **库存统计**（条目数、总数量） |
| | 🗂️ 缓存管理 | LCSC 抓取缓存列表/清理 |
| | 💾 数据备份 | InvenTree 全库备份，**实时日志**、快照下载/删除 |

## 3. 条码查看页（核心新增）

### 扫码操作页（三合一）

转移 / 盘点 / 扣减合并为一个页面：顶部三个**模式按钮**切换，共用一个扫码
输入框与操作日志，模式间切换自动复位流程状态，扫码枪回车即提交。

- **📦 转移**：先扫货品库存码（暂存），再扫目标货位码 → 整笔转移；
  转移完成自动回到"等待下一件货品"；
- **🧮 盘点**：先输入实际数量，每扫一次库存码就把该笔库存设为该数量；
- **➖➕ 增减**：方向可切换（扣减/加库存）+ 每次数量（正整数），每扫一次生效；
  数量不足时扣减方向会拦截；后端带符号数量：正走 `remove`（出库记录）、
  负走 `add`（入库记录），InvenTree 均有跟踪记录；
- 错误统一提示（如"识别为货位码但转移需要库存码"），成功/失败有提示音。



上传立创商城「订单详情」导出的 **.xls**（xlrd 解析，自动扫全部 sheet 的
「商品明细列表」段），流程：**解析 → 匹配 → 勾选 → 执行**：

1. **匹配**：`IPN == 商品编号(C-code)` → `ManufacturerPart.MPN` → 未找到；
   每行显示现有库存数量（`in_stock`）辅助决策；
2. **执行**（勾选行，未匹配行走「自动新建」开关，新建用 `upsert_part`）：
   - 已有 Part：按勾选字段刷新（描述/图片/关键词默认开，名称/备注/参数默认关，
     逻辑同「更新现有 Part」）；
   - **入库**：按订单数量新建一笔 StockItem（可填默认货位 ID，备注含订单号）；
3. 解析器兼容多段明细 / 跨 sheet 续表；新增依赖 `xlrd`。

### 立创订单导入（🧾 页签）

上传立创商城「订单详情」导出的 **.xls**（xlrd 解析，自动扫全部 sheet 的
「商品明细列表」段），流程：**解析 → 匹配 → 勾选 → 执行**：

1. **匹配**：`IPN == 商品编号(C-code)` → `ManufacturerPart.MPN` → 未找到；
   每行显示现有库存数量（`in_stock`）辅助决策；
2. **执行**（勾选行，未匹配行走「自动新建」开关，新建用 `upsert_part`）：
   - 已有 Part：按勾选字段刷新（描述/图片/关键词默认开，名称/备注/参数默认关，
     逻辑同「更新现有 Part」）；
   - **入库**：按订单数量新建一笔 StockItem（可填默认货位 ID，备注含订单号）；
   - **封装**：订单里解析出的「封装」列会透传给 writer——描述尾部自动追加
     `（封装：xxx）` 后缀（描述已含该串则不重复），缺 LCSC 参数数据时兜底；
3. 解析器兼容多段明细 / 跨 sheet 续表；新增依赖 `xlrd`。


### 淘宝商品导入（🛒 页签）

淘宝反爬强、无法直抓，走「**浏览器另存为 MHTML**」路线：在商品页**选好规格**，
另存为单个 `.mhtml` 文件后上传（可多选，≤20 个/次，单文件建议 <20MB）。

- 解析（`POST /api/taobao/parse`）：纯本地解析渲染后的 DOM（CSS-module 类名
  按前缀匹配），提取标题/高亮价/店铺/商品链接（item id、skuId）/**选中 SKU**/
  全部可选规格/规格参数+重点参数/图集；图片按 URL 匹配 **mhtml 内嵌字节**
  （离线可用，不回源 alicdn），webp 自动转 jpg。解析结果服务端缓存
  （token，LRU 12 个），预览不做任何写入。
- 导入（`POST /api/taobao/import`）：按行确认后创建器件——
  IPN 默认 `TB<商品ID>`（可改）、名称默认标题（可改）、价格可覆盖（默认页面价，
  CNY）、规格下拉可改（默认页面选中项）、主图可从图集下拉换；
  参数表写入 InvenTree 参数（模板按需自动创建），「品牌」可勾选创建厂商公司
  （「无品牌」跳过）；供应商默认「淘宝」+ SupplierPart + 单档价格；
  notes 记录淘宝链接/店铺/价格/规格/来源文件。IPN 已存在默认跳过，
  可勾选更新。分类 ID 可选（整批共用）。
  **入库**：行内填「入库数」即按该数量创建 StockItem（挂采购价+供应商部件，
  货位用整批「入库货位 ID」），结果行给「器件↗ / 库存↗」直达链接。
- 解析模块 `lcsc2inv/taobao_import.py` 独立无依赖（email+bs4），
  writer 侧新增通用 `upsert_custom_part()`（非 LCSC 来源复用）。


### 更新现有 Part（单条导入页内）

输入 **Part ID** + **LCSC 代码/链接**，按 LCSC 最新数据刷新已存在的 Part。
字段勾选：描述 / 图片（强制重传）/ 关键词（默认开），备注+链接 / 参数（默认关）。

- **不新建**、不动名称/分类/厂商/供应商/价格/库存；
- 复用导入的图片上传逻辑（下载 → `Part.uploadImage`，失败只记原因）；
- 参数写入按 LCSC 分类走 `field_map.yaml` 模板映射，只写不删；
- **封装**：更新描述时自动在尾部追加 `（封装：xxx）`（取 LCSC `Package`/国内站
  「商品封装」参数，或订单导入显式传入的 footprint；已含则不重复）。
- 结果按字段反馈（`updated_fields` / `image.skipped_reason` / `errors`），
  部分失败返回 502 且写入历史（type=`update`）；
- Part ID 不存在返回 404。



围绕「自定义绑定条码」的一整套功能。背景：本项目 InvenTree 版本较新，
`barcode_data` 存在 `StockItem` 模型字段上但 **REST API 不暴露**（只暴露
`barcode_hash`），因此读取原文需要进容器（见 §5 缓存机制）。

### 3.1 查看（双码）

- 输入库存 ID（纯数字）或扫现有条码 → 显示库存信息（元器件/数量/货位/批次）
  + **两个二维码**：
  - **InvenTree 标准库存码**：数据 `{"stockitem": <pk>}`，服务端 `qrcode` 库
    绘制 PNG（离线可用），扫码可反查（InvenTree 兼容旧格式解析）；
  - **自定义绑定条码**：内容来自本地缓存（见下），同样服务端绘制；
- 缓存中没有该库存的绑定码时显示「缓存中无」并提示同步，**不进容器**，
  查询速度恒定（~100ms）。

### 3.2 绑定

- 输入库存 ID + 条码内容（支持扫码枪直接扫新码回车）→ `POST /api/barcode/bind`
  → 调 InvenTree `/api/barcode/link/` → 成功后**同步写入本地缓存**并自动在上方
  加载该库存视图；条码被其它库存占用时错误透传、缓存不动。

### 3.3 缓存（性能核心）

REST API 读不到绑定码原文，采用**一次性全量读取 + 本地缓存**：

- 「全量同步」按钮：进 InvenTree 容器跑只读 `manage.py shell`，一次 exec 读出
  **全部**非空 `barcode_data`（~1600 条约 3.5s），**整体镜像**到本地缓存：
  新增 / 覆盖更新 / 删除（InvenTree 已解绑的条码同步移除）三向对齐，
  同步后缓存与 InvenTree 完全一致，日志展示三类明细；
- 缓存落盘 `<cache>/bound_barcodes.json`（临时文件 + 原子替换，随 docker 卷
  持久化，重建容器不丢）；查看时只读缓存；
- 备份：**下载 JSON**（完整备份，可再导入）/ **下载 CSV**（`stock_pk,
  barcode_data`，带 BOM 供 Excel）；**导入 JSON** 整体替换恢复（保留原更新
  时间）；所有操作写入页面日志。

## 4. 数据备份

- 流程：挂载的 `docker.sock` 进入 `inventree-server` 容器执行 `invoke backup`
  → docker 低层 API **流式读取**输出实时回传网页 → `get_archive` 拉取
  `/home/inventree/data/backup` → 按 mtime 过滤本次新文件 → 落到容器 `/backup`
  （宿主机 `./backup`）；
- 网页端：实时日志（轮询 1s，增量渲染）、快照列表（递归列出，含子目录）、
  单文件下载、**删除快照**（备份进行中 409 拒绝；防路径穿越）。

## 5. 关键设计决策 / 踩坑记录

| 问题 | 结论 |
|------|------|
| docker.sock 权限 | 群晖 socket 是 `root:root 660`（组 GID 0）；Dockerfile 里
  `useradd --groups 0` 让容器内 appuser 加入 GID 0；`./backup` 目录需
  `chown 10001:10001` |
| `INVENTREE_BACKUP_SRC_DIR` | 必须是 **InvenTree 容器内部**路径
  （`/home/inventree/data/backup`），不是宿主机路径 |
| 备份产物嵌套 | `get_archive` 拉目录带 `backup/` 前缀，快照列表需递归 |
| 绑定码原文读取 | REST API 不暴露 `barcode_data`，只能进容器 ORM 读取；
  因此做了全量缓存 |
| 条码缓存键 | 货位绑定推荐用**货位参数**（模板名 `TAG_ID`，界面可编辑）；
  metadata 字段这版 API 不处理（ORM 可用） |
| InvenTree 插件设置物化 | 插件**首次注册时**默认设置写入数据库，之后改代码
  默认值无效——需删掉对应 `PluginSetting` 行或界面里改 |
| 插件日志 | worker 只输出 WARNING+；插件 logger 命名须挂
  `inventree.plugins.*` 层级才会被 InvenTree 的 LOGGING 配置接收 |
| 封装信息来源 | 国际站 ld+json 自带 `Package` 参数；国内站 ld+json **没有**
  任何参数，封装只能从 SSR HTML 的 `<dt>商品封装</dt><dd>…</dd>` 抽取。
  写入端兜底三层：① `Package` 参数（field_map Base 链路恒命中）；
  ② 描述尾部追加 `（封装：xxx）`（已含则不重复）；③ 订单导入把 .xls
  「封装」列显式透传（`WriteOptions.footprint` / `update_part_fields(footprint=)`） |
| 参数写入绕过 SDK | **所有 PartParameter 一直写不上的根因**：服务端 API 530 >
  inventree-python 0.23.2 的 MAX_API_VERSION 428，SDK 的
  `PartParameter*`/`PartParameterTemplate*` 一律抛 `NotImplementedError`。
  已改为直接 REST 调用（`InvenTreeWriter._rest`），导入与回填共用 |
| 封装一键回填 | `./backfill_package.sh`（容器内 `python3 -m lcsc2inv
  backfill-package`）：扫描 IPN 为 LCSC 编号的全部 Part，重抓 LCSC 更新
  描述后缀/keywords/Package 参数。幂等，默认 dry-run，`--commit` 写入 |

## 6. API 端点总表（web.py）

| 端点 | 说明 |
|------|------|
| `POST /api/import` | 单条导入（含库存码绑定） |
| `POST /api/part/update` | 更新现有 Part（LCSC 刷新所选字段） |
| `POST /api/order/parse` | 解析立创订单 .xls + 逐行匹配（只读） |
| `POST /api/order/import` | 执行订单导入（更新/新建 + 入库） |
| `POST /api/batch`、`POST /api/batch/jobs`、`GET /api/batch/jobs/<id>` | 批量导入（同步/异步） |
| `POST /api/preview` | 无写入预览 |
| `GET/POST /api/history(/clear)` | 导入历史 |
| `GET /api/doctor` | 状态检查（含库存条目数/总数量） |
| `GET/POST /api/cache(/clear)` | LCSC 抓取缓存 |
| `POST /api/barcode/scan` | 条码识别（库存项/货位） |
| `POST /api/stock/transfer` `/api/stock/count` | 转移 / 盘点 |
| `POST /api/stock/decrement` | 扣减（InvenTree remove 动作，留跟踪记录） |
| `GET /api/stock/<pk>/barcode-info` | 库存信息 + 标准码数据 + 绑定码（读缓存） |
| `GET /api/stock/<pk>/qrcode.png`、`GET /api/qrcode.png?data=` | 服务端二维码 |
| `POST /api/barcode/bind` | 绑定自定义条码（同步缓存） |
| `GET /api/barcode/cache`、`POST /api/barcode/cache/sync` | 缓存概况 / 全量同步（只补新码） |
| `GET /api/barcode/cache/download?format=json\|csv`、`POST /api/barcode/cache/restore` | 缓存备份 / 恢复 |
| `POST /api/backup`、`GET /api/backup` | 启动备份（流式日志）/ 状态+快照 |
| `DELETE /api/backup/<snapshot>`、`GET /api/backup/<snapshot>/<path>` | 删除 / 下载快照 |

## 7. InvenTree BLE 定位插件（hablelocate）

源码 `inventree-plugin-ble-locate/`，已部署到
`/volume1/docker/inventree/inventree-data/plugins/hablelocate/` 并激活。

**用途**：InvenTree 库存页「定位项目」→ 选 `hablelocate` → 通过 Home
Assistant 触发 BLE 标签 LED 闪烁（标签同时继续广播 BTHome 供 HA 读电量，互不影响）。

```
定位项目 → POST /api/locate/ {plugin:"hablelocate", item|location}
        → 后台 worker 执行插件
        → 标签解析：库存项 metadata → 货位参数 TAG_ID → 货位 metadata
        → POST {HA_URL}/api/services/script.ble_tag_locate {"tag_id","seconds"}
        → HA 脚本 → BLE 写标签 → LED 闪
```

- 插件设置（管理界面）：`HA_URL`、`HA_TOKEN`（长期访问令牌）、`HA_SERVICE`
  （默认 `script.ble_tag_locate`）、`TAG_METADATA_KEY`（默认 `TAG_ID`）、
  `BLINK_SECONDS`（10）、`REQUEST_TIMEOUT`（5）；
- **结果通知**：HA 调用完成后，插件把真实结果写入 InvenTree **站内通知**
  （右上角 🔔 铃铛，发给所有活跃员工用户，点击可跳转到对应库存/货位）——
  例如「BLE 定位成功：标签 1234」或「BLE 定位失败：标签 1234 / ConnectionError…」。
  注意：点按钮时弹出的绿色 toast「已请求项目位置」是 InvenTree 前端的接口受理
  提示（后台异步执行所致），**无法更改**；真实结果以铃铛通知为准；
- HA 侧：建脚本 `script.ble_tag_locate`（fields: `tag_id`/`seconds`），内部用
  你的固件协议发送——换发送方式只改 HA 脚本，插件契约不变；
- 失败处理：HA 不可达/未配置标签只记 WARNING，不影响 InvenTree。

## 8. 测试与质量

- 主套件 `tests/`：**142 通过**（2 个与本次无关的既有失败被排除：
  `test_web.py::TestImport::test_part_url_import_*`，断言 fetch 收规范化代码
  与实现传原始 URL 不一致，遗留问题）；
- 插件套件 `inventree-plugin-ble-locate/tests/`：**20 通过**（离线，纯逻辑模块
  按文件路径加载，不依赖 InvenTree 运行时）；
- lint：`ruff`（E,F,B,N,W,UP,SIM,I），本次改动未引入新问题。

## 9. 环境速查

| 项 | 值 |
|----|----|
| Web 工具 | 容器 `lcsc2inventree-web`，端口 8095，配置在 `.env`；
  **HTTPS**：挂载 `certs/nas.crt+nas.key` 时 gunicorn 自动启用 TLS（无证书退回
  HTTP）；SAN 覆盖 `192.168.11.106`，自签证书首次访问需点「继续前往」或导入设备受信任根 |
| InvenTree | `http://192.168.11.106`，容器 `inventree-server` + `inventree-worker`，
  数据目录 `/volume1/docker/inventree/inventree-data` |
| 备份产物 | `/volume1/docker/lcsc2inventree/backup/<时间戳>/backup/` |
| 条码缓存 | 容器卷内 `bound_barcodes.json`（当前 1587 条） |
| 插件部署 | 拷贝 `hablelocate/` 到数据目录 `plugins/` + 重启 server/worker
  （**不要**写入 plugins.txt，那是 pip 包列表） |
| 日志级别 | `/volume1/docker/inventree/.env:21` `INVENTREE_LOG_LEVEL=WARNING`
  （详见 §10） |

## 10. worker 日志查看与自测指南（BLE 定位）

定位插件跑在 **`inventree-worker`** 容器的后台任务里，验证功能就是
「触发一次定位 + 看 worker 日志」。

### 10.1 查看日志的两种方式

**方式 A：SSH 命令行**（灵活，推荐自测用）

```bash
# 实时跟踪（最常用：开着不动，去网页点「定位项目」，回来看输出）
sudo docker logs -f inventree-worker

# 实时跟踪 + 只看定位相关（-a 必加：日志含 ANSI 颜色码，否则 grep 可能漏匹配）
sudo docker logs -f inventree-worker 2>&1 | grep -a "标签"

# 看最近 5 分钟 / 最近 50 行
sudo docker logs --since 5m inventree-worker
sudo docker logs --tail 50 inventree-worker
```

**方式 B：群晖 Container Manager 图形界面**

Container Manager → 容器 → `inventree-worker` → 「日志」页签，右上角可搜索
（搜 `标签` 即可过滤定位日志）。适合不想 SSH 的时候。

### 10.2 日志级别：为什么只看到 WARNING

插件日志分两级：

- **WARNING+**：默认可见——如「标签 1234 触发失败: ...」（HA 不可达等）；
- **INFO**：默认不可见——如「定位货位 186：触发标签 1234」「库存 xxx 未配置
  标签，跳过」。

默认级别由 `/volume1/docker/inventree/.env` 的 `INVENTREE_LOG_LEVEL=WARNING`
控制。想看 INFO：

```bash
# 1. 编辑 /volume1/docker/inventree/.env，把第 21 行改为：
INVENTREE_LOG_LEVEL=INFO
# 2. 重建容器（env 变更需要重建，单纯 restart 不生效）
cd /volume1/docker/inventree && sudo docker compose up -d
```

### 10.3 三种自测方法

**方法 1：网页点击（最接近真实使用）**

1. SSH 开一个窗口跑 `sudo docker logs -f inventree-worker 2>&1 | grep -a "标签"`；
2. InvenTree 网页 → 库存页/货位页 → 点「定位项目」→ 选 hablelocate；
3. 回到终端看日志输出。

**方法 2：curl 直接调接口**

```bash
TOKEN=$(grep INVENTREE_TOKEN /volume1/docker/lcsc2inventree/.env | cut -d= -f2)

# 定位库存项（会自动继承其货位的 TAG_ID）
curl -s -X POST http://192.168.11.106/api/locate/ \
  -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
  -d '{"plugin": "hablelocate", "item": 1535}'

# 定位货位
curl -s -X POST http://192.168.11.106/api/locate/ \
  -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
  -d '{"plugin": "hablelocate", "location": 186}'
```

**方法 3：容器内同步直跑（最快，能看到 INFO 日志，不用改全局级别）**

```bash
sudo docker exec -w /home/inventree/src/backend/InvenTree inventree-server \
  python manage.py shell -c "
import logging; logging.basicConfig(level=logging.INFO)
from plugin import registry
registry.get_plugin('hablelocate').locate_stock_location(186)  # 换成要测的货位 pk
"
```

### 10.4 预期日志样例

| 场景 | 日志（级别） |
|------|--------------|
| 正常触发，HA 已配置 | `INFO HA 已触发标签 1234（服务 script.ble_tag_locate，时长 10s）` |
| HA 地址未配/不可达（当前状态） | `WARNING 标签 1234 触发失败: ConnectionError: ... Failed to resolve 'homeassistant.local' ...` |
| 令牌错误 | `WARNING HA 调用返回 401 tag=...: {"detail": "Invalid token."}` |
| 货位没配 TAG_ID | `INFO 货位 186 未配置标签（参数 TAG_ID / metadata 均无），跳过定位` |
| 库存不在库 | `INFO 库存 xxx 不在库（已出货/耗尽），跳过定位` |

> 提示：方法 1/2 的任务由 worker 异步执行，日志延迟 1~5 秒；方法 3 是同步
> 执行、当场出结果，调试插件代码时最方便。改过插件代码后必须
> `sudo docker restart inventree-server inventree-worker` 才会生效。
