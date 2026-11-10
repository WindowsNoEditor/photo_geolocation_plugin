# 照片定位识别（MaiBot 插件）

> 作者：WindowsNoEditor　·　版本 1.0.0　·　License: MIT

注：**插件由 Claude Opus 4.8 开发**，本人实际使用（使用高德 API）无故障。

群友在群里发照片时，插件读取图片 **EXIF 里的 GPS 定位**，逆地理编码成中文地址，然后 **@ 发图人**
回一句「请问是在 <地址> 的 <昵称> 群友吗？」。全程通过 `chat.receive.before_process` 钩子实现，
**不改动 MaiBot 核心源码**。

效果示例（截图已征得群友同意）：

![效果演示](https://raw.githubusercontent.com/WindowsNoEditor/file/main/8eeebb4af5924949c746463c6ac5e695.png)

> ⚠️ **重要现实**：QQ 发**普通（压缩）图**通常会**抹掉 EXIF/GPS**，一般只有发**原图**或以**文件**
> 形式发送的图片才带 GPS。所以本插件大多数时候是静默的——只有真读到 GPS 才会回复。这是 QQ 的行为，
> 不是插件的问题。

## 功能特性

- **零核心侵入**：挂 `chat.receive.before_process` 钩子，在最早阶段拿到图片原始字节，不动核心代码。
- **两种地理编码后端**：
  - `osm`（默认）：OpenStreetMap / Nominatim，**免费、无需 key**。
  - `amap`：高德 Web 服务，**中国境内最准**，需要一个高德「Web服务」Key。
- **坐标系自动纠偏**：EXIF 的 GPS 是 WGS-84；走高德前会自动做 **WGS-84 → GCJ-02（火星坐标）**
  转换，避免地址偏移。
- **含蓄模式**：高德结果可只显示到「市 + 区 + 街道」（`amap_township_only`），不暴露具体小区/门牌。
- **原图 & 文件双读**：内联图片读不到时，可尝试读以「文件」形式发送的图片（GPS 存活率更高）。
- **防刷屏**：同一个人两次定位回复之间有冷却；一条消息只处理第一张候选图；已处理过的图片按 hash 去重。
- **可自定义回复**：`{address}`、`{nick}` 模板占位符，可选是否先 @ 对方、是否仅群聊生效。

## 工作原理

1. `before_process` 钩子拿到入站消息的 `raw_message`，挑出 `image`（可选 `file`）组件。
2. 优先用组件里的 `binary_data_base64`（压缩前原始字节），兜底按 `hash` 到图片目录里找文件。
3. 用 Pillow 读 EXIF 的 GPSInfo（IFD 34853），把度分秒（DMS）转成十进制经纬度。
4. 逆地理编码成地址（OSM 直接用 WGS-84；高德先转 GCJ-02）。
5. 读到地址就 @ 发图人并回复；读不到 GPS 就静默跳过。

网络请求与 EXIF 解析放在后台任务里跑，不阻塞入站主链。

## 安装

**方式一：插件市场**
在 MaiBot WebUI 的插件市场里搜索「照片定位识别」安装。

**方式二：手动**
把本仓库整个目录放进 MaiBot 的插件目录（容器内通常是 `/MaiMBot/plugins/`），
文件监听会自动热加载。确保运行环境里装了依赖：

```bash
pip install "httpx>=0.24.0" "Pillow>=9.0.0"
```

（MaiBot 官方容器一般自带 Pillow；httpx 若缺就按上面装。）

## 配置

首次加载会在插件数据目录生成 `config.toml`，也可在 WebUI 配置页里改：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `geocoder` | `osm` | 逆地理编码服务：`osm`（免费无需 key）/ `amap`（高德，需 key，中国最准） |
| `amap_key` | `""` | 高德「Web服务」API Key（`geocoder=amap` 时必填；osm 不需要） |
| `amap_township_only` | `true` | 高德地址只显示到「市+区+街道」，不暴露具体小区/门牌 |
| `reply_template` | `请问是在 {address} 的 {nick} 群友吗？` | 回复模板，`{address}`=地址，`{nick}`=发图人昵称 |
| `at_user` | `true` | 回复时先 @ 一下发图的群友 |
| `group_only` | `true` | 只在群聊生效；设为 `false` 后私聊才可能生效（还需白名单里有对应 `private:` 条目，若开了白名单） |
| `whitelist_enabled` | `false` | 启用会话白名单：开启后只处理 `whitelist_chats` 里登记的会话；开启但无有效条目时全部静默 |
| `whitelist_chats` | `[]` | 白名单条目，格式 `group:<群号>` / `private:<QQ号>`，如 `["group:123456789", "private:987654321"]` |
| `scan_files` | `true` | 除内联图片外，也尝试读取以「文件」形式发送的图片 |
| `cooldown_seconds` | `20` | 同一个人两次定位回复的最小间隔（秒），防刷屏 |
| `address_max_len` | `60` | 地址最长显示字数，`0` 表示不截断 |
| `osm_language` | `zh` | OSM 返回地址的语言 |
| `request_timeout` | `12` | 逆地理编码请求超时（秒） |
| `images_dir` | `/MaiMBot/data/images` | 收到的图片文件目录（按 hash 兜底读取），一般无需修改 |

> 高德 Key 申请：高德开放平台 console.amap.com → 应用 → 添加 Key → 服务平台选 **「Web服务」**
> （不是 JS/Android/iOS/小程序）。个人免费额度日常够用。

## 会话白名单

bot 同时挂在多个群/私聊时，可以只在指定会话里生效。默认**关闭**
（`whitelist_enabled = false`、`whitelist_chats = []`），老配置升级后行为不变，
仍按 `group_only` 等原有设置工作。

条目必须带前缀，**群号与 QQ 号分开登记**（以下号码均为虚构示例）：

- `group:<群号>` —— 群聊条目，群消息只按群号匹配；
- `private:<QQ号>` —— 私聊条目，私聊只按对方 QQ 号匹配。

**仅群聊生效**（默认 `group_only = true` 即可）：

```toml
[plugin]
group_only = true
whitelist_enabled = true
whitelist_chats = ["group:123456789", "group:987654321"]
```

**允许指定私聊**（必须显式 `group_only = false`，否则私聊一律不生效）：

```toml
[plugin]
group_only = false
whitelist_enabled = true
whitelist_chats = ["group:123456789", "private:11112222"]
```

行为约定：

- **开启白名单但列表为空（或全是无效条目）= 所有会话静默**。宁可「配漏了不工作」，
  也不做「以为限制了其实没限」；加载/配置更新时会提示一次「无有效条目」。
- 裸号码（如 `"123456789"`）、未知前缀、非数字号码都算**无效条目**，会被忽略。
- 群号与 QQ 号即使数字相同也互不误放行：`group:123` 只匹配群 123，
  `private:123` 只匹配 QQ 号 123 的私聊。
- 群消息缺失群号、私聊缺失对方 QQ 号时直接跳过。
- 日志只记录白名单开关状态与有效条目数，不输出群号/QQ 号/会话 ID。

## 使用

装好后无需任何命令：群友发**带 GPS 的原图/文件**时，插件会自动 @ 对方并报出识别到的大致位置。
发普通压缩图则不会有反应（因为 GPS 已被 QQ 抹掉）。

## 隐私与合规说明

- 本插件**只读取用户主动发到群里的图片**自带的 EXIF GPS，不访问任何其它数据；读不到 GPS 就静默。
- 这是一个「提醒大家注意照片会泄露位置」性质的功能，请在**成员知情**的群里使用，不要用于骚扰或跟踪。
- 默认开启「地址只到街道」的含蓄模式，避免直接暴露精确门牌。
- 使用 OSM/Nominatim 时请遵守其
  [使用政策](https://operations.osmfoundation.org/policies/nominatim/)（已设置 User-Agent、请勿高频请求）；
  使用高德时请遵守高德开放平台服务条款。

## 依赖

- `httpx >= 0.24.0`（逆地理编码 HTTP 请求）
- `Pillow >= 9.0.0`（读取 EXIF GPS）
- `maibot_sdk >= 2.5.1`（宿主提供）

## 许可证

[MIT](LICENSE) © 2026 WindowsNoEditor
