# 在 Windows 上运行 lcsc2inventree

本文档针对 **Windows 10 / 11**，覆盖安装、配置、运行与故障排查。所有命令同时给出 **PowerShell** 与 **CMD** 两种语法。

> 🌐 **国内立创商城支持**：本工具已适配 `item.szlcsc.com`（深圳主站），详见 [docs/LCSC_CN_VS_INTL.md](LCSC_CN_VS_INTL.md)。两站点对比、URL 差异、ld+json 结构差异都在那篇文档。

---

## 1. 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| 操作系统 | Windows 10 1809 (build 17763) | Windows 11 |
| Python | 3.10 | 3.11 或 3.12 |
| PowerShell | 5.1（内置） | **PowerShell 7.x**（对 UTF-8 支持更完整） |
| 架构 | x64 | x64（ARM64 大部分依赖无 wheel） |
| 网络 | 可访问 `www.lcsc.com` 与你的 InvenTree 实例 | — |
| 磁盘 | 200 MB（含 venv 与缓存） | — |

> ⚠️ **不推荐 Windows on ARM**：部分二进制 wheel 缺失，需要额外安装 MSVC 工具链。
>
> ⚠️ **Python 3.13 暂不推荐**：`inventree-python` 0.23.x 尚未声明兼容；如使用可能需从源码安装。

---

## 2. 安装 Python（如果尚未安装）

建议从 [python.org](https://www.python.org/downloads/windows/) 下载安装包，安装时**务必勾选**：

- ✅ Add Python to PATH
- ✅ Install py launcher

安装完成后验证：

```powershell
python --version
py --version
```

应输出 `Python 3.10.x` 或更高。

---

## 3. 安装 uv（推荐）

`uv` 是 Rust 写的 pip 替代品，比 `pip` 快 10-100 倍，且能自动管理 venv。

```powershell
# PowerShell（管理员）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后**重新打开一个终端**让 PATH 生效，验证：

```powershell
uv --version
```

> 备选：不使用 uv，直接用 `pip` / `pipx`，见 §7。

---

## 4. 拉取项目

### 4.1 用 git

```powershell
cd $HOME\Documents
git clone <your-repo-url> lcsc2inventree
cd lcsc2inventree
```

### 4.2 下载 ZIP

在 GitHub 上点 `Code → Download ZIP`，解压到 `C:\tools\lcsc2inventree\`。

---

## 5. 创建虚拟环境并安装依赖

### 5.1 用 uv（推荐）

```powershell
cd $HOME\Documents\lcsc2inventree     # 或你的解压路径
uv venv
uv pip install -e ".[dev]"
```

### 5.2 用 pip（备选）

```powershell
cd $HOME\Documents\lcsc2inventree
py -m venv .venv
.\.venv\Scripts\Activate.ps1          # PowerShell
# 或 .\.venv\Scripts\activate.bat    # CMD

python -m pip install -e ".[dev]"
```

> ⚠️ PowerShell 首次激活可能报「无法加载脚本，因为在此系统上禁止运行脚本」。**以管理员身份**执行一次：
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```
> 然后重新激活。

### 5.3 验证安装

```powershell
python -m lcsc2inv --help
```

应输出 Click 帮助信息。

---

## 6. 配置 `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

最小必填：

```ini
INVENTREE_URL=https://inventree.example.com
INVENTREE_TOKEN=<your-token-here>
LCSC_CACHE_DIR=%USERPROFILE%\.cache\lcsc2inventree
```

> 💡 Windows 路径里 `%USERPROFILE%` 等价于 Linux 的 `~`，本工具会自动 `expandvars + expanduser`。也可以写：
> - 绝对路径：`C:\Users\<you>\.cache\lcsc2inventree`
> - 带波浪号：`~\.cache\lcsc2inventree`（程序会自动展开）

---

## 7. 第一次运行（dry-run）

```powershell
$env:DRY_RUN = "true"                    # PowerShell
# 或 set DRY_RUN=true                   # CMD

python -m lcsc2inv import C28323
```

预期输出（节选）：

```
[DRY] C28323  CL21B105KBFNNNE
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ name         │ Samsung Electro-Mechanics …    ┃
┃ category     │ Capacitors/… → Passive/Capaci…┃
┃ parameters   │ Value=1u; Tolerance=±10%; …   ┃
└──────────────┴───────────────────────────────┘
```

取消 dry-run：

```powershell
Remove-Item Env:\DRY_RUN                  # PowerShell
# 或 set DRY_RUN=                        # CMD
```

---

## 8. 运行批量导入

```powershell
python -m lcsc2inv batch examples\bom.csv
```

CSV 格式（UTF-8，**不要带 BOM**——用 `notepad` 另存为 `UTF-8` 而不是 `UTF-8 with BOM`）：

```csv
lcsc_code,quantity,note
C28323,2000,主电源去耦
C191386,500,
```

---

## 9. 常见坑与解决方案

### 9.1 路径分隔符

代码内部统一用 `pathlib.Path`，**用户输入的字符串路径**会自动转换。但**不要**在 `.env` 里写带尾随空格或混合分隔符的路径。

### 9.2 长路径限制（MAX_PATH 260）

如果缓存目录或项目目录很深（比如放在 OneDrive 同步文件夹里），可能触发 `FileNotFoundError`。两种解法：

**方案 A**：把项目放在浅层目录（如 `C:\tools\lcsc2inventree`）。

**方案 B（Win10 1607+）**：启用长路径支持——以管理员身份运行：
```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
                 -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

### 9.3 编码 / 中文乱码

- **CSV 文件**：务必保存为 UTF-8（无 BOM）。Excel 打开 CSV 默认按 GBK 解码会乱码——可换用 VSCode / Notepad++ 打开。
- **PowerShell 控制台**：
  ```powershell
  $OutputEncoding = [System.Text.Encoding]::UTF8
  chcp 65001 | Out-Null
  ```
- **CMD 控制台**：运行 `chcp 65001` 切到 UTF-8 代码页。
- **国内立创商城（item.szlcsc.com）输出含中文**：例如类别字段是 `以太网连接器(RJ45 RJ11)`、品牌是 `Ckmtw(灿科盟)`。**如果控制台不是 UTF-8，会显示乱码或问号**。务必先执行上面的 `chcp 65001` 再跑 `lcsc2inv import`。
- **PowerShell 5.1（旧版 Windows 自带）**：对中文等宽字符渲染有缺陷，建议升级到 PowerShell 7：
  ```powershell
  winget install Microsoft.PowerShell
  ```

### 9.4 防火墙 / 代理

如果公司网络走代理：

```powershell
$env:HTTPS_PROXY = "http://proxy.corp.example.com:8080"
python -m lcsc2inv import C28323
```

或者在 `.env` 里加：

```ini
HTTPS_PROXY=http://proxy.corp.example.com:8080
HTTP_PROXY=http://proxy.corp.example.com:8080
```

### 9.5 Windows Defender 误报

`python-levenshtein` 这类带 C 扩展的 wheel 解压时可能被 Defender 短暂锁定。如果 `uv pip install` 超时，把项目目录加入 Defender 排除项：

> Windows 安全中心 → 病毒防护 → 管理设置 → 排除项 → 添加排除项 → 文件夹 → 选择 `lcsc2inventree` 目录

### 9.6 行尾符（CRLF vs LF）

`.env`、YAML 配置文件必须是 **LF**。如果出现诡异 YAML 解析错误：

```powershell
# 用 git 自动转换
git config core.autocrlf false
# 或用 dos2unix（通过 scoop 安装：scoop install dos2unix）
dos2unix config\*.yaml .env
```

### 9.7 Ctrl+C 中断

Windows 下 `KeyboardInterrupt` 触发略晚，批量跑时按一次 Ctrl+C 即可；不要连按多次否则可能留下未写完的 StockItem（幂等性保证下次重跑会补全）。

### 9.8 StockItem 管理

默认行为：**只建器件，不建 StockItem**。

- `lcsc2inv import C28323` → 只建 Part/MfrPart/SupPart，**不建 StockItem**
- `lcsc2inv import C28323 --stock --qty 1000` → 建 StockItem.quantity=1000
- 批量导入 `lcsc2inv batch bom.csv` → 同上，默认不建 StockItem

**推荐做法**：用工具管理器件数据，库存由用户在 InvenTree Web UI 中手工管理（更灵活）。

### 9.9 图片上传

- 默认开启：`LCSC_UPLOAD_IMAGE=true`（`.env` 中可关闭）
- 幂等：Part 已有图片则跳过；`--update` 强制重传
- 缓存位置：`<cache_dir>/images/<C-code>.jpg`
- Windows 注意：Defender 误报（临时 .jpg 会立即删除），长路径（缓存目录建议浅层）

详情见 README.md "图片上传" 小节。

### 9.10 `lcsc2inv` 命令无法直接调用

如果你想直接敲 `lcsc2inv` 而不是 `python -m lcsc2inv`，需要激活 venv 后 venv 的 `Scripts\` 已加入 PATH，或者：

```powershell
uv tool install .                          # 全局安装
# 或
pipx install .                             # 备选
```

安装后任意目录可直接调用 `lcsc2inv import C28323`。

### 9.9 临时文件 / 缓存清理

```powershell
python -m lcsc2inv cache ls
python -m lcsc2inv cache clear
```

---

## 10. 故障排查速查

| 症状 | 可能原因 | 解决 |
|------|----------|------|
| `ModuleNotFoundError: inventree` | 没装依赖 / 没激活 venv | `uv pip install -e .` 后重新激活 |
| `ConnectionError` 抓 LCSC 超时 | 网络问题 / LCSC 限流 | 调高 `.env` 的 `LCSC_REQUEST_INTERVAL=2.0` |
| `403 Forbidden` | User-Agent 被识别为机器人 | 升级 `.env` 的 `LCSC_USER_AGENT` 为最新 Chrome |
| `YAML 解析错误` | 文件是 CRLF / 含 BOM | `dos2unix` 转换 |
| `无法识别为 LCSC C-code` | 输入了中文标点 / 多余空格 | 直接复制 C-code，如 `C28323` |
| `INVENTREE_TOKEN 错误` | token 过期或 RBAC 不足 | 重新 `curl -u user:pwd /api/user/me/token/` 拿 token |
| dry-run 输出乱码 | 控制台编码不是 UTF-8 | `chcp 65001` |

---

## 11. 开发 / 运行测试

```powershell
python -m pytest                         # 跑全部 20 个测试
python -m pytest tests\test_mapping.py   # 单文件
python -m pytest -k clean                # 按关键字
```

依赖已经在 `[dev]` extra 中（含 `pytest`）。

---

## 12. 已知 Windows 限制

1. **并发批量**：当前 CLI 的 `--workers > 1` 实际仍顺序执行（InvenTree SDK 非线程安全）。Windows 上同样如此。
2. **图片上传**（未来功能）：multipart 上传依赖 `requests-toolbelt`，已在依赖中预留。
3. **Jupyter Notebook**：可以在 Windows 上跑 `pip install notebook` 但本文档未覆盖。

---

## 13. 一键体检

```powershell
python -m lcsc2inv doctor
```

如果所有行都打绿色 ✓，说明：

- Python 与依赖 OK
- `.env` 配置正确
- InvenTree API 可达
- YAML 配置文件能加载

如果报红，按提示修复后重跑。

---

## 14. 图片上传

LCSC 商品页通常有 front/back 等多张图片（如 `https://assets.lcsc.com/images/lcsc/.../C28323_front.jpg`），工具会自动将 `image_urls[0]` 上传到 InvenTree `Part.image` 字段。

### 幂等行为

- **首次导入**：下载图片 → 上传 → 缓存到 `<cache_dir>/images/C28323.jpg`
- **后续导入**：发现 Part 已有 `Part.image`，跳过下载/上传
- **强制重传**：`--update` 标志会强制重新下载并上传图片

### 配置

`.env` 中设置：

```ini
LCSC_UPLOAD_IMAGE=true   # 默认 true；设为 false 关闭上传
```

### Windows 注意事项

- **Defender 误报**：图片缓存文件是临时 `.jpg`，会立即删除；如误报可把项目目录加入排除项
- **长路径**：缓存目录 `<cache_dir>/images/` 默认在用户目录下；若 `.cache` 很深，Windows MAX_PATH 可能限制，建议把 `LCSC_CACHE_DIR` 设为浅层路径（如 `C:\tools\lcsc_cache\`）
- **UTF-8 路径**：缓存目录支持中文路径；推荐用 `%USERPROFILE%` 或 `~` 表示用户目录（程序自动展开）

### 查看已上传图片

```powershell
# 确认 Part 有图片字段
python -m lcsc2inv import C28323 --dry-run
# 输出应包含 [DRY] 提示，且终端显示图片 URL

# 在 InvenTree Web UI 中：
# Parts → 找到对应 Part → 查看 image/thumbnail 字段
```

---

## 15. 国内立创商城（item.szlcsc.com）

如果你在大陆，主要使用国内站：

```powershell
# 直接传国内站 URL（必须用 numeric id，不是 C-code）
python -m lcsc2inv import "https://item.szlcsc.com/360864.html" --dry-run
```

**注意**：国内站 URL 形如 `https://item.szlcsc.com/360864.html`，**不接受 `C28323` 形式的 C-code 直链**（会 404）。如果你的 BOM 来源只有 C-code，建议：

1. 在浏览器登录国内站，搜索 C-code，把跳转后的 numeric URL 录入 CSV
2. 或者直接用国际版（`www.lcsc.com`），URL 接受 C-code，更省事

国内站的限制（详细对比见 [LCSC_CN_VS_INTL.md](LCSC_CN_VS_INTL.md)）：

- ⚠️ ld+json **不含 `additionalProperty`**：规格参数（电阻值、容值、封装等）拿不到，InvenTree PartParameter 为空
- ⚠️ 中文类别（如 `以太网连接器(RJ45 RJ11)`）无法匹配 `lcsc_categories.yaml`（英文路径），自动归类失败
- ⚠️ 阿里云 WAF 较严：缺 `Referer` 会被 403。实测默认配置勉强可用，但批量跑可能触发限流

如果只用国际版，**完全不需要读这一节**——直接跳过。

---

## 附：PowerShell vs CMD 速查

| 操作 | PowerShell | CMD |
|------|------------|-----|
| 激活 venv | `.\.venv\Scripts\Activate.ps1` | `.\.venv\Scripts\activate.bat` |
| 设环境变量 | `$env:X = "y"` | `set X=y` |
| 取消环境变量 | `Remove-Item Env:\X` | `set X=` |
| 复制文件 | `Copy-Item a b` | `copy a b` |
| 设 UTF-8 | `chcp 65001` + `$OutputEncoding=[Text.Encoding]::UTF8` | `chcp 65001` |
| 跑命令 | `python -m lcsc2inv import C28323` | 同左 |
| 编码（中文输出） | `chcp 65001` + `$OutputEncoding=[Text.Encoding]::UTF8` | `chcp 65001` |
| 升级 PowerShell | `winget install Microsoft.PowerShell` | 不支持（需手动下载） |

---

## 15. 参考链接

- [docs/LCSC_CN_VS_INTL.md](LCSC_CN_VS_INTL.md) — 国内/国际立创商城差异
- [README.md](../README.md) — 项目总览
- [inventree-python 文档](https://github.com/inventree/inventree-python) — InvenTree SDK

---

有问题先看 §10 速查表；如果还解决不了，把 `python -m lcsc2inv doctor` 的完整输出贴到 issue。