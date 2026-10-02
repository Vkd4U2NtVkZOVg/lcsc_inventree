# inventree-plugin-ble-locate

InvenTree 自定义插件：在库存页点「定位项目」时，通过 **Home Assistant** 触发
**BLE 标签**的 LED 闪烁，帮你找到实物所在的货位。

```
InvenTree「定位项目」
  → POST /api/plugin/locate/ {plugin: "hablelocate", item: <库存pk>}
  → InvenTree 后台 worker 执行插件
  → 读库存项 / 货位 metadata 里的标签 ID（键默认 ble_tag，库存项优先）
  → POST {HA_URL}/api/services/script.ble_tag_locate  {"tag_id": "...", "seconds": 10}
  → 你的 HA 脚本 → BLE 写标签 → LED 闪
```

标签本身继续广播 BTHome 供 HA 读电量 / ID，与本插件互不影响。

## 插件做了什么 / 不做什么

- **做**：读 metadata 里的标签 ID → 调 HA 服务（带 `tag_id` 和 `seconds` 两个参数）；
- **不做**：BLE 通信本身。怎么把数据发给标签（ESPHome `ble_client`、蓝牙代理、
  其它桥接）全部在 HA 侧实现，以后换发送方式只改 HA 脚本，插件不动；
- HA 不可达 / 标签未配置时：只记日志、静默跳过，不影响 InvenTree 任何功能。

## 安装（InvenTree Docker 部署）

1. 确认 InvenTree 数据目录在宿主机的位置（`docker inspect inventree-server`
   看 `/home/inventree/data` 映射到哪，本机为 `.../inventree-data`）；
2. 把 `hablelocate/` 整个目录拷到 `<数据目录>/plugins/hablelocate/`；
3. 在 `<数据目录>/plugins.txt` 里加一行 `hablelocate`；
4. 重启 InvenTree 的 **server 和 worker** 容器（定位任务由 worker 异步执行）；
5. 管理界面 → 设置 → 插件设置 → 找到 **BLE 标签定位（Home Assistant 桥接）**
   → 启用，并配置：
   - `Home Assistant 地址`：如 `http://192.168.11.5:8123`
   - `HA 长期访问令牌`：HA 个人资料页最底部「长期访问令牌」创建后粘贴
   - 其余保持默认即可（服务名 `script.ble_tag_locate`、闪烁 10 秒）。

## Home Assistant 侧配置

在 HA 建一个脚本（`configuration.yaml` 或 UI 均可），接收 `tag_id` / `seconds`
两个参数，内部按你的固件协议把数据发给标签：

```yaml
script:
  ble_tag_locate:
    alias: BLE 标签定位闪烁
    fields:
      tag_id:
        description: 标签 ID
        example: "tag-A1"
      seconds:
        description: 闪烁秒数
        example: 10
    sequence:
      # ↓↓↓ 换成你自己的发送实现（示例：ESPHome 蓝牙代理向标签写特征值）
      - action: esphome.ble_writer_write
        data:
          device: ble-proxy-1
          address: !secret tag_mac  # 或按 tag_id 映射到对应 MAC
          service_uuid: "你的固件服务 UUID"
          characteristic_uuid: "你的固件特征 UUID"
          payload: "触发闪烁的指令"
```

多标签时在该脚本里按 `tag_id` 分发（choose / 映射表）。

## 货位 / 库存绑定标签

**推荐：货位参数**（库存地点详情页 → 参数 → 新建）：

- 参数模板名 = `TAG_ID`（可在插件设置里改），数据 = 标签 ID；
- 界面直接编辑，无需 API。

解析优先级：**库存项 metadata → 货位参数（TAG_ID）→ 货位 metadata**（库存项单独贴标签时优先）。

## 测试

```bash
# 离线单元测试（不需要 InvenTree 环境）
python3 -m pytest inventree-plugin-ble-locate/tests/test_plugin.py -q
```

## 排障

- 点「定位项目」毫无反应：先在 HA 里手动跑 `script.ble_tag_locate` 确认灯会闪；
  再看 InvenTree **worker 容器**日志有无 `HA 已触发标签` / `触发失败`；
- `INVENTREE_PLUGINS_ENABLED` 必须为 `True`，插件需在管理界面**启用**；
- 改过插件代码后要重启 server + worker 两个容器才会生效。
