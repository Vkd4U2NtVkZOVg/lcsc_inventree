# LCSC 国内 / 国际站点选择

立创商城有两个主要入口，本文档说明它们的差异以及 lcsc2inventree 的支持情况。

| 站点 | 域名 | 用户群 |
|------|------|--------|
| **国际版** | `https://www.lcsc.com` | 海外、英文界面、USD 价格 |
| **国内版** | `https://item.szlcsc.com` | 中国大陆、中文界面、CNY 价格（深圳主站） |

## 默认与切换

**默认使用国际版**（数据完整、C-code 直链、稳定）。

如要切到国内版，需要在 `.env` 中设置 `LCSC_BASE_URL=item.szlcsc.com`，**但请先阅读下面的限制**：

```ini
LCSC_BASE_URL=item.szlcsc.com
```

## 关键差异

### 1. URL 模式不同

- **国际版**：`https://www.lcsc.com/product-detail/C28323.html`（LCSC C-code 即可）
- **国内版**：`https://item.szlcsc.com/360864.html`（**数字内部 ID**，C-code 返回 404）

实测：`https://item.szlcsc.com/C28323.html` → HTTP 302 → `https://www.szlcsc.com/404.html`

这意味着：**国内版不接受 C-code 直链**，必须先用数字 ID。lcsc2inventree 当前**不支持** C-code → 数字 ID 的自动转换（这需要调用国内站搜索 API 或预建映射表），所以从 CLI 传入 `C28323` 时程序会拒绝并提示使用国际站。

> 📌 **如果你手头已有国内站 numeric URL**（例如 `https://item.szlcsc.com/360864.html`），可以直接传给 lcsc2inventree，程序会识别为 `CN:360864` 并走国内站路径。

### 2. ld+json 结构差异

| 字段 | 国际版 | 国内版 |
|------|--------|--------|
| 顶层结构 | 单个 Product 对象 | 包裹在 `@graph` 列表中（需遍历） |
| `price` | `0.0344` | `1.23` |
| `priceCurrency` | `USD` | `CNY` |
| `inventoryLevel` | `2000`（裸 int） | `{"@type": "QuantitativeValue", "value": 56588}` |
| `category` | `Capacitors/Ceramic Capacitors`（英文） | `以太网连接器(RJ45 RJ11)`（中文） |
| `additionalProperty` | ✅ 含规格参数 | ❌ **不含**（参数需要 JS 渲染页面或单独 API） |
| 图片域名 | `assets.lcsc.com` | `alimg.szlcsc.com` |
| 数据手册域名 | `datasheet.lcsc.com` | `atta.szlcsc.com` |

✅ **lcsc2inventree 已自动处理**：
- `@graph` 包装（`_extract_ldjson_dict` 遍历 graph 找 Product）
- `QuantitativeValue` 库存（`Offer.from_raw` 抽取 `.value`）
- CNY 价格（直接读取 `priceCurrency`）
- 中文/英文类别字段差异不影响数据本身

❌ **仍然受限**：
- 国内站不返回 `additionalProperty`，所以**所有规格参数（电阻值/容值/封装等）都拿不到**——InvenTree PartParameter 为空
- 中文类别无法匹配 `config/lcsc_categories.yaml`（该 YAML 是英文路径），自动归类失败，会落到 `__uncategorized__`

### 3. 反爬严格度

- **国际版**：标准浏览器 UA 即可，1 req/s 安全
- **国内版**：阿里云 WAF + ACL 严格校验，**未携带 Referer/Cookie 时返回 `403 非法ACL-URL请求`**。实测 `list.szlcsc.com/anon/products/detail?code=C28323` 直接返回 403。

如果坚持用国内站，可能需要：

```python
# 手工调用时增加 headers
session.headers.update({
    "Referer": "https://item.szlcsc.com/",
    "Origin": "https://item.szlcsc.com",
})
```

并考虑登录态 cookie（需在浏览器登录后导出）。

### 4. 图片上传

国内站图片 URL 格式：

```text
alimg.szlcsc.com/.../C28323_front.jpg
```

lcsc2inventree 的 `Fetcher.download_image()` 会自动从国内站下载并上传到 InvenTree，但有以下注意：

- ⚠️ **国内站 ld+json 缺少 `image` 字段**：实测部分商品页 `ld+json` 不含 `image` 或仅含少量图片（front 一张），导致 `image_urls` 为空或仅 1-2 张
- ⚠️ **图片域名不同**：`assets.lcsc.com`（国际） vs `alimg.szlcsc.com`（国内），`Fetcher.download_image()` 已适配不同域名，但需网络可达

### 5. 缓存与代码兼容性

- 解析器自动兼容两种 ld+json 结构
- 缓存文件按 `{sku}.json` 命名，国内站的 `CN:360864` 会缓存为 `CN_360864.json`（避免和 `C360864` 冲突）
- 7 天 TTL 对两个站点同样生效

## 推荐用法

### 场景 A：从 BOM CSV 导入（推荐国际版）

```bash
# 默认配置即可
lcsc2inv batch examples/bom.csv
```

### 场景 B：已经有国内站 URL 列表

如果你的 BOM 来源是 JLCPCB 配单或国内采购系统的导出（只提供国内站 URL），可以：

```bash
# 直接传入国内站 URL
lcsc2inv import "https://item.szlcsc.com/360864.html" --dry-run
```

此时 lcsc2inventree 会：
1. 解析出 `CN:360864`
2. 走国内站路径抓取
3. 跳过 `additionalProperty` 提取（没有）
4. 中文类别 → uncategorized（需手工归类或扩展 YAML）

### 场景 C：希望混合使用

不建议。原因：库存/价格货币不同，混在一起会让 InvenTree 的报表失真。如果非要混，建议每个 Part 的 `notes` 里加 `[LCSC-CN]` / `[LCSC-INT]` 标记。

## 已知未支持功能

| 功能 | 状态 |
|------|------|
| C-code → 国内站 numeric id 自动转换 | ❌ 需要国内站搜索 API 凭证，**不建议做**（合规风险） |
| 国内站规格参数（电阻值/容值等） | ❌ ld+json 不含；JS 渲染页面才有，需要 Selenium |
| 国内站自动分类 | ❌ 需要单独的 `lcsc_categories_cn.yaml` 中文映射表 |
| 国内站 User-Agent / Referer 注入 | ⚠️ 部分实现，建议手工扩 `_make_session()` |

## 实测样例（CN_360864.html）

```text
sku:        C386757
mpn:        R-RJ45R08P-C000
brand:      Ckmtw(灿科盟)
category:   以太网连接器(RJ45 RJ11)        # 中文
price:      1.23 CNY
inventory:  56588
datasheet:  https://atta.szlcsc.com/.../C386757_xxx.pdf
page_url:   https://item.szlcsc.com/360864.html
```

对应的 InvenTree 写入结果：
- ✅ Part（含 name / description / IPN=C386757 / brand=MfrPart）
- ✅ SupplierPart（SKU=C386757, price=1.23 CNY）
- ⚠️ 无 PartParameter（additionalProperty 为空）
- ⚠️ category=None（中文无法匹配 YAML，落到 uncategorized）

## 建议的演进路线

1. **优先用国际版** —— 当前项目数据流已稳定，dry-run 已在 3 个真实 C-code 上验证
2. 国内站作为**未来增强**：等找到稳定的 numeric id 转换方式再考虑
3. 如果确实需要国内站规格参数，可以单独写一个 `lcsc_cn_detail.py` 用 Selenium 抓取详情页（JS 渲染）