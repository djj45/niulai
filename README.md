# 约牛聊天室（niulai）

把约牛投顾直播间的聊天室搬到浏览器里：**历史消息 + 实时消息 + 图片收发 + 文章阅读 +
视频回放 + 本地归档**，按 [`protocol.md`](./protocol.md) 从零复现全部链路。

> **2026-09-24 起小程序接口已停用**，本工具整体迁移到**网页版**
> （`tougu.zx093.cn/touguapp/webChatRoom`）的接口：登录有**微信扫码**和**账号密码**
> 两种方式，都在 `/login` 页完成，凭证全自动写入，不再依赖抓小程序包。
> 小程序时代的协议分析与踩坑记录保留在 `protocol.md` 附录里。

```
┌─────────────── 约牛网页版 ───────────────┐
│  扫码/账密登录     历史/发消息      实时收消息    │
│  account.zx093.cn  /client/community  腾讯云IM WS  │
│  阿里云滑块验证码   touguServer      my-imcloud.com │
└──────────────────────────────────────────┘
        ↓ 协议复现（本工具）
┌──────────────────────────────────────────┐
│  SQLite 本地库  →  Quart(HTTP/2)  →  浏览器    │
│  历史游标分页       WebSocket 广播   双栏实时界面  │
└──────────────────────────────────────────┘
```

---

## 目录

- [快速开始](#快速开始)
- [登录与凭证](#登录与凭证)
- [两条通道，各自独立](#两条通道各自独立)
- [Web 界面](#web-界面)
- [命令行工具](#命令行工具)
- [项目结构](#项目结构)
- [协议实现要点](#协议实现要点)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 快速开始

```bash
# 1. 安装依赖（uv 会自动建虚拟环境）
uv sync

# 2. 生成自签证书（HTTP/2 用，图片多也不卡）
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"

# 3. 启动
uv run python run.py
#   浏览器打开 https://127.0.0.1:5002  （首次提示证书不受信任，点继续）

# 4. 登录：自动跳到 /login —— 扫微信二维码，或账号密码过一次滑块
#    成功后自动回到主页，centraltoken / IM 凭证 / 房间信息全部自动写入，
#    首次打开时后台自动拉取当天消息
```

> 没有凭证也能跑：界面照常打开，浏览本地已归档的消息、图片、搜索、统计都可用；
> 只有「拉取线上历史 / 发消息 / 收实时」需要登录。

---

## 登录与凭证

`/login` 页两条路，都保留：

### ① 微信扫码（推荐，免验证码）

打开页面自动生成二维码（约 2 分钟有效），微信扫一扫确认，成功后自动跳回主页。
链路（详见 `protocol.md` 附3.3）：

```
getAuthToken（deviceId=12332）→ qrcode.htm 出码（multipart，~110KB jpg）
→ 每 2.5s 轮询 authTokenExchangeToken.htm?c=md5(authToken)&ts=live3
   101005=未扫码  200001=过期  data=centraltoken（成功）
```

### ② 账号密码（阿里云滑块）

- 账号密码本机 DES 加密后直发 `account.zx093.cn`，不经过任何第三方；
- 滑块是网页版同款阿里云组件：`sceneId=17n9bhbp`、`prefix=v98goc`（缺 prefix 会
  拼出 `undefined.captcha-open…` 直接 Network Error，这是踩过的坑）、`loginSource=7`；
- 过完滑块自动登录，票据只在内存里用完即弃，不需要人工填任何值。

### 历史账号（多账户）

账密登录成功后账号自动保存到本机 `.env` 的 `NIULAI_SAVED_LOGINS`（JSON 数组）：
默认勾选「保存密码」（可取消，取消则只记账号）；下拉列表按最近登录排序，
**打开页面自动选中并填充最新用过的那个账号**。删除在页面上点「删除选中」。

### 凭证寿命（实测）

| 凭证 | 寿命 | 过期后的行为 |
|---|---|---|
| `centraltoken` | 实测 ≥3.6 天（存疑，继续追踪在 `token_log` 表） | 业务接口返回 `200001`，去 `/login` 重新登一次 |
| `userSig` | **几十分钟级**（比文档短得多） | 后台自动用 centraltoken 现换新的并重连 IM，无需人工 |

---

## 两条通道，各自独立

约牛把「消息」拆成了两条互不相干的通路 —— 这是本工具设计的核心：

| | 历史消息 | 实时消息 |
|---|---|---|
| 通道 | HTTPS REST `client/community/getChatRecordList.htm` | 腾讯云 IM WebSocket `my-imcloud.com` |
| 凭证 | **centraltoken** | **userSig**（自动刷新） |
| 方向 | 游标分页往回翻 | 服务端主动推送 |
| 发消息 | `client/community/sendMessage.htm`（同一凭证） | — |

**因此：`centraltoken` 过期后，只要 IM 还连着，实时消息照收不误。**
反之 IM 断了也能靠轮询 REST 同步。本工具两条路都实现了，任一条可用就不算瘫。

### 为什么要同时跑「IM 推送」和「REST 轮询」

IM 是主通道（秒级），REST 只负责**补漏 + 校正**，不能关：

1. **IM 客户端没有实现离线补拉** —— 断开/重启窗口里的消息，重连后服务器不会补发，
   只能靠 REST 找回来。而这个连接实测真的会断（服务端 `HelloInterval=120`）。
2. **IM 推送的字段是审核前占位值** —— `privateMessageFlag`/`vipUser`/`auditStatus`
   恒定 `true/true/0`，REST 记录才是权威，靠它回填修正。
3. 系统/房间事件、`quoteContent`（引用回复）的完整形态只走 REST。
4. 7 天历史本来就只有 REST 有。

后台轮询间隔自适应（`_sync_interval()`）：IM 在线 300s、连接中 30s、
仅 REST 60s、**IM 刚重连成功立即一次**（这一刻最需要补漏）。
增量同步在库里见到已有 id 就停；只有 `added > 0` 才广播，不打扰 UI。

---

## Web 界面

打开 `https://127.0.0.1:5002`。

| 功能 | 说明 |
|------|------|
| **顶栏一行放下** | `日期选择 ◀ 今天 ▶ · 近3天 近7天 全部 · 实时状态 · ⏬ 全量 📺 视频 📚 文章 💾 备份 📊 统计 🚪 退出` |
| **打开自动拉当天** | 首次打开后台自动同步今天的消息，不需要点任何同步按钮 |
| **双栏布局** | 左栏「🎓 老师观点」（`user_type IN (3,4)`，网页版老师是 3），右栏全部消息 |
| **日历通读** | 选任意一天 → 该天消息时间正序全量载入，停在最新处；跨天有日期分隔条 |
| **范围模式** | 近3天 / 近7天 / 全部：取最新一批停在底部，滚到顶部自动接更早（阅读位置不跳） |
| **实时消息** | IM 推送 → 落库 → WebSocket 广播；贴底时自动追加。**「N 条新消息」提示只在「最新/今天」视图弹**，翻历史/范围视图保持安静（消息仍静默入库） |
| **📚 文章** | 老师的研选文章全列表（分页触底自动加载），点开弹窗内阅读：正文是 Word 粘贴的富文本，白底 iframe（blob URL）渲染，图片可直读；聊天里的文章卡片（早盘预案/知识点小结）点开同源 |
| **📺 视频** | 视频课栏目 + 回放列表，点条目**弹窗内直接播放**（hls.js + 原生 controls，含全屏；m3u8 免签直连） |
| **📌 置顶** | 房间置顶消息/置顶文章显示在列表头部的置顶条，点开看全文 |
| **关键词搜索** | 跨日期搜索并高亮；点昵称「只看 TA」 |
| **快捷定位** | 9:15 / 9:30 / 11:30 / 13:00 / 14:30 / 回顶部 / 回底部 |
| **发送消息 / 图片** | Enter 发送（输入法回车不误发）、📎 或 ⌘V 粘贴截图，OSS 直传（≤2MB） |
| **图片本地化** | 后台自动补齐（启动一次 + 新消息顺手下载），断网也能翻完整历史；`/api/media` 命中本地直接读盘 |
| **📊 统计 / 💾 备份** | 各天消息数与老师消息分类、媒体进度；备份 SQLite 到 `data/backups/` |
| **🚪 退出登录** | 清掉本机凭证（不影响消息库），回到 `/login` |

### 状态提示

顶栏圆点：🟢 实时已连接（文字同步变绿）/ 🟡 连接中 / 🔴 断开·凭证失效·未启用。

### 贴底只做一次

「贴到底部」只在刚打开/切换日期（或范围）时执行一次；之后自由浏览，
两栏滚动位置完全独立，实时消息只跟随「本来就贴底」的那一栏。

---

## 命令行工具

```bash
# 1) 命令行全量同步（不跟浏览器并行跑；命令行为主）
uv run python tools/sync_all.py                 # 全量，翻到最早
uv run python tools/sync_all.py --days 3        # 只同步近 3 天
uv run python tools/sync_all.py --status        # 看库里现有情况

# 2) IM 握手/收消息探针（联网，只读：登录 + 进群 + 打印推送，不发消息）
uv run python tools/im_probe.py --identifier cu_1234567 --usersig "<userSig>" --seconds 60

# 3) IM 消息解析 + 入库全链路自检（用抓包里的真实 msg_push 帧，不联网）
uv run python tools/test_im_parse.py
uv run python tools/test_e2e.py
```

模块也可以单独用：

```python
import niulai_api as api

c = api.TouguClient(central_token="...")
room = c.get_room_by_teacher(328)                 # 房间信息
for page in c.iter_records(room["id"], max_pages=5):
    print(page[0]["nickName"], page[0]["msgContent"])

c.send_text(room["id"], "hello")                  # 发文本
c.upload_and_send_image(room["id"], open("a.png","rb").read())   # 发图片

arts = c.get_articles(328, page=1)                # 研选文章列表
detail = c.get_article(arts[0]["id"], 328)        # 文章正文（Word HTML）
cols = c.get_video_columns(328)                   # 视频栏目
plays = c.get_video_playbacks(328, cols[0]["columnId"])
m3u8 = c.get_video_play(plays[0]["id"])["adaptiveUrl"]   # 可直接播放
```

---

## 项目结构

```
niulai/
├── protocol.md              协议分析文档（实现依据；小程序时代的结论在附录）
├── niulai_api.py            协议客户端：sign / DES / 扫码登录链 / 业务 REST / 文章 / 视频 / OSS 直传
├── niulai_im.py             腾讯云 IM WebSocket 客户端（登录/心跳/进群/收推送/回执）
├── db.py                    SQLite 存储层（消息/用户/曾用名/头像/媒体/房间/设置/token 寿命）
├── app.py                   Quart 后端：REST API + WebSocket 广播 + IM 常驻监听 + 登录/媒体缓存
├── run.py                   Hypercorn 启动（HTTP/2 + HTTPS）
├── templates/index.html     主界面（单文件，零依赖）
├── templates/login.html     登录页（扫码 + 账密滑块 + 历史账号）
├── static/hls.min.js        hls.js 本地副本（视频回放用）
├── tools/                   同步 / 探针 / 自检脚本
├── data/                    运行期数据（gitignore：库/缓存/备份/抓包）
└── .env                     凭证与历史账号（gitignore，含密码，绝不提交）
```

---

## 协议实现要点

实现过程中踩到的坑与关键结论（都在代码注释里标了，完整版见 `protocol.md`）：

| 点 | 结论 |
|---|---|
| `sign` 算法 | 盐 `asdasdsadfg`；`k=v` 字典序 → `&key=盐` → MD5 大写（抓包样本验证过） |
| 业务鉴权 | 请求头 `centraltoken`；失效返回 `{"status":200001}` |
| 网页版接口 | 业务路径前缀 `/touguServer/client/community/…`（历史/发送/IM 凭证）、`/client/article/…`（文章）、`/client/live/…`（视频）；与小程序版的差异表在 `protocol.md` 附3 |
| **老师 user_type 变了** | 网页版老师是 **3**（小程序时代是 4）→ 所有判断用 `IN (3,4)`，老库新库通吃 |
| 历史分页 | `direction:1` + `cursorId` 向历史翻页；房间流带 `bizType:1`；**翻到最早返回空数组**即到底；`pageSize` 实测可到 1000（全量同步用大页快 ~60 倍） |
| **历史只给 7 天** | 房间信息 `visibleDays: -7`，翻到第 7 天就返回空 —— 这是能拿到的全部，不是同步没跑完 |
| 消息去重 | REST 有 `id`；IM 推送有 `imMsgSeq`；同一条 IM 会推两遍（sync+push），按 `MsgSeq` 去重 |
| 文章 | `queryArticleDetail.htm {articleId, teacherId}` 返回 Word 粘贴 HTML（图片绝对地址可直读）；`articleType=2` 是 PDF（载荷 JSON `{filePath}`，走 `preview.htm` 换 URL）；列表 `queryArticleListByPage.htm` 15/页 |
| 视频 | `playback/queryById.htm` 的 `adaptiveUrl` 是**免签 m3u8**（CORS 全开），本地 hls.js 直接放；官方播放页 `client.zx093.com/webktc/tougu/index.html?path=/video&id=<id>&teacherId=<tid>` |
| 图片直传 | `getPolicy.htm` → multipart 直传 OSS（≤2MB）；读取走 `fileoss.zx093.com` 无鉴权 |
| 扫码登录 | `getAuthToken` → `qrcode.htm` → 轮询 `authTokenExchangeToken.htm`；`101005`=未扫码、`200001`=过期 |
| 账密滑块 | `prefix=v98goc` 必传（否则拼 `undefined.captcha-open…`）；票据一次性，服务端会二次核验 |
| IM 端点 | `wss://<sdkAppId>w4c.my-imcloud.com/binfo?…`，与小程序时代**同一个网关** |
| **IM 帧方向性** | 客户端必须发**二进制帧**（UTF-8 JSON bytes）；发 text 帧服务端静默丢弃 |
| IM 服务端帧 | 前 4 字节 `b"COMP"` 是流式 gzip（`Z_SYNC_FLUSH` 无 trailer），用 `zlib.decompressobj(31)` |
| IM 心跳 | 真实客户端 ~26s 一次（服务端 `HelloInterval=120` 不可信，按 120 发会被踢 1006） |
| IM 登录频率 | 两次 `wslogin` 至少隔 15s，违反报误导性的 `70402` |
| **userSig 短命** | 网页版实测**几十分钟**就过期（远短于文档的 60 天）→ 后台自动刷新 + 重连，用户无感 |
| IM 推送不能信标记字段 | `privateMessageFlag`/`vipUser`/`auditStatus` 是审核前占位值，入库取中立值等 REST 回填 |
| 「老师回复某人」 | `user_type IN (3,4) 且 toUserId>0`，引用快照在 `quoteContent` |
| `/api/messages?order=desc` | 服务端已反转为时间正序，调用方**不要**再 reverse（踩过：多反一次老师栏贴底错位） |
| 出站直连 | 四条出站路径（requests/httpx/websockets/媒体下载）全部 `trust_env=False`/`proxies=None`，本机 sing-box 开关不影响工具 |
| 写入性能 | 每批一次 `SELECT` + `executemany` + 250 条一批让出 GIL，同步期间 `/api/status` 稳定 30~45ms |

---

## 常见问题

**Q：页面打开只有“暂无消息”？**
A：本地库还是空的。去 `/login` 登录一次，打开主页会自动拉取当天消息；
想补更早的就点「⏬ 全量」。

**Q：提示“登录失效”？**
A：centraltoken 过期了，去 `/login` 扫个码（或账密登一次），回来即恢复。

**Q：点「全量」很快就说完成？**
A：是同步完了。约牛只开放**最近 7 天**（`visibleDays: -7`），翻到第 7 天服务端
就返回空。进度条会显示总条数。

**Q：文章点开空白？**
A：已修。原因是 Chromium 对 sandbox+srcdoc 在同一 iframe 上反复赋值有偶发不绘制
的引擎 bug（同一篇第二次点开白屏）；现在每次打开重建 iframe 并走 blob URL 导航。

**Q：userSig 不是 60 天有效吗？**
A：网页版实测几十分钟就过期。后台会自动换新并重连（顶栏状态会短暂变黄），
无需人工干预；只有 centraltoken 也同时过期时才需要重新登录。

**Q：看历史时还会弹「N 条新消息」吗？**
A：不会。提示只在「最新/今天」视图且你往上翻时出现；近3天/近7天/全部、
日历历史日期、搜索结果里新消息一律静默入库。

**Q：老师栏（左栏）怎么没有消息了？**
A：网页版把老师的 `user_type` 从 4 改成了 3，老判断漏掉了。现在全部 `IN (3,4)`。

**Q：能回复某条消息吗？**
A：不能。`sendMessage.htm` 只接受房间/类型/内容，没有引用字段；
老师的 `quoteContent` 是服务端审核时写入的，客户端没有入口。

**Q：为什么用 HTTPS + HTTP/2？**
A：多路复用下大量图片并发不互相阻塞。自签证书首次会提示，点继续即可。

**Q：模板改动后不生效？**
A：Quart 缓存 Jinja 模板，需要**重启服务**，浏览器再强制刷新（⌘⇧R）。

---

## 免责声明

本项目仅用于**个人学习与对自己有权访问的数据做本地归档**：
所有接口、签名算法与实时协议均来自对**本人账号**流量的分析。

- 不含任何破解、绕过付费或越权能力：付费可见字段（`feeStatus`）由服务端控制；
- 请遵守约牛用户协议与相关法律法规，不要用于批量采集、骚扰他人或商业传播；
- 抓取频率已做退避（每页 0.35s），请勿调高；
- 因使用本工具产生的一切后果由使用者自行承担。
