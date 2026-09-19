# 约牛聊天室（niulai）

把微信小程序 **约牛**（appid `wx0c0819078db5e3e1`）的聊天室搬到浏览器里：
**历史消息 + 实时消息 + 图片收发 + 本地归档**，按 [`protocol.md`](./protocol.md) 从零复现全部链路。

前端参考同目录的 `../hexun`（和讯直播室工具）：双栏布局、关键词搜索、快捷时段定位、
图片放大/粘贴上传、头像与曾用名、实时推送与「N 条新消息」提示。

```
┌────────────── 微信小程序约牛 ──────────────┐
│  登录/历史/发消息       图片           实时收消息  │
│  HTTPS REST        OSS直传      腾讯云IM WS   │
│  account.zx093.cn  aliyuncs    my-imcloud.com │
└──────────────────────────────────────────────┘
        ↓ 协议复现（本工具）
┌──────────────────────────────────────────────┐
│  SQLite 本地库  →  Quart(HTTP/2)  →  浏览器    │
│  历史游标分页       WebSocket 广播   双栏实时界面  │
└──────────────────────────────────────────────┘
```

---

## 目录

- [快速开始](#快速开始)
- [两条通道，各自独立](#两条通道各自独立)
- [拿到凭证的三种办法](#拿到凭证的三种办法)
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

# 4.（可选）把之前抓包的数据导入本地库，首屏立刻有内容 + IM 实时可跑
mitmdump -q -nr <你的>.mitm -s tools/extract_capture.py
uv run python tools/import_seed.py

# 5. 之后想拉线上历史/发消息时，一键刷新 centraltoken（详见下文「拿到凭证」）
mitmdump -p 8888 -s tools/capture_token.py
```

> 没有凭证也能跑：界面照常打开，浏览本地已归档的消息、图片、搜索、统计都可用；
> 只有「拉取线上历史 / 发消息 / 收实时」需要对应凭证。

---

## 两条通道，各自独立

约牛把「消息」拆成了两条互不相干的通路 —— 这是本工具设计的核心：

| | 历史消息 | 实时消息 |
|---|---|---|
| 通道 | HTTPS REST `chatrecord/getChatRecordList.htm` | 腾讯云 IM WebSocket `my-imcloud.com` |
| 凭证 | **centraltoken**（小时级） | **userSig**（约 60 天） |
| 方向 | 游标分页往回翻 | 服务端主动推送 |
| 发消息 | `chatrecord/sendMessage.htm`（同一凭证） | — |

**因此：`centraltoken` 过期后，只要 `userSig` 还有效，实时消息照收不误。**
反之 `userSig` 过期也能靠轮询 REST 同步。本工具两条路都实现了，任一条可用就不算瘫。

### 为什么要同时跑「IM 推送」和「REST 轮询」

IM 是主通道（秒级），REST 只负责**补漏 + 校正**，不能关：

1. **IM 客户端没有实现离线补拉** —— 断开/重启窗口里的消息，重连后服务器不会补发，
   只能靠 REST 找回来。而这个连接实测真的会断（服务端 `HelloInterval=120`）。
2. **IM 推送的字段是审核前占位值** —— `privateMessageFlag`/`vipUser`/`auditStatus`
   恒定 `true/true/0`，REST 记录才是权威（实测 14,299 行全 `0/0/1`），靠它回填修正。
3. 系统/房间事件、`quoteContent`（引用回复）的完整形态只走 REST。
4. 7 天历史本来就只有 REST 有。

所以后台轮询的间隔是**自适应**的（`_sync_interval()`）：

| IM 状态 | 间隔 | 理由 |
|---|---|---|
| `online` | **300 秒** | IM 正常工作，REST 只是核对，不必浪费请求 |
| 正在连 / 刚断（`connecting` 等） | **30 秒** | 很可能刚漏消息，积极补漏 |
| 没配 IM / `login_failed` | **60 秒** | REST 是唯一通道，但也不必太急 |
| **刚重连成功** | **立即一次** | `_im_on_status` 发现进入 `online` 就 `_sync_wake.set()`，这一刻最需要补漏 |

两个省请求的细节：
- 增量同步**在第 1 页发现「库里已有」的 id 就停**（原来写的是 `i > 1`，
  固定白跑一页）。消息按时间倒序回，边界在页内，更旧的页必然全是已有的；
  真正的补空档交给「⏬ 全量同步」。
- 只有 `added > 0` 才广播，所以追平时不会造成任何 UI 变化。

当前节奏可以在「📊 统计」里看到（“后台核对节奏”），也能在「📥 同步最新」按钮的
tooltip 上悬停查看。`/api/status` 的 `sync.interval` / `sync.interval_reason` 也带了。

---

## 拿到凭证的三种办法

### ① 一键抓取（推荐，全自动）

不需要重新登录微信。只要小程序里还有有效会话，**进一次聊天室**就会带上 `centraltoken` 请求头。

```bash
# 终端 A：启动抓包（自动把抓到的 token 交给本地应用校验并写入）
cd /Users/djj45/code/niulai
mitmdump -p 8888 --mode upstream:http://127.0.0.1:20122 -s tools/capture_token.py
#   ↑ 如果你平时不用代理，去掉 --mode upstream:...；
#     如果代理不是 20122，换成你实际的（见 protocol.md §8）
```

```bash
# 终端 B：把系统代理指向 mitmdump
networksetup -setwebproxy "Wi-Fi" 127.0.0.1 8888
networksetup -setsecurewebproxy "Wi-Fi" 127.0.0.1 8888
networksetup -setsocksfirewallproxystate "Wi-Fi" off
```

然后：**把微信里的小程序窗口彻底关掉再重开** → 打开约牛 → 进任意一个老师的聊天室。
终端出现 `✅ centraltoken 已自动写入并验证通过` 就完成了（应用会自动带出房间信息、
刷新 `userSig`、重启实时通道，**不需要在网页里手工填任何东西**）。

```bash
# 收尾：恢复系统代理
networksetup -setwebproxy "Wi-Fi" 127.0.0.1 20122
networksetup -setsecurewebproxy "Wi-Fi" 127.0.0.1 20122
# SOCKS 的「端口」与「开关」是两个参数：
#   -setsocksfirewallproxystate 只接受 on/off（写 -setsocksfirewallproxystate "Wi-Fi" 20122 会报
#   "The parameters were not valid."），端口要用 -setsocksfirewallproxy 设。
# 原来 SOCKS 就是关的话，下面两行可以不做。
networksetup -setsocksfirewallproxy "Wi-Fi" 127.0.0.1 20122
networksetup -setsocksfirewallproxystate "Wi-Fi" on
# 检查三条代理是否都回到 20122
networksetup -getwebproxy "Wi-Fi"; networksetup -getsecurewebproxy "Wi-Fi"; networksetup -getsocksfirewallproxy "Wi-Fi"
```

> 漏抓了？终端会提示检查项（系统代理、小程序窗口是否重开、是否进了聊天室）。
> 只读流量，不修改/不重放任何请求，不涉及账号密码。

### ② 从已有抓包文件导出（适合事后补）

```bash
# 导出房间信息 + 历史消息 + centraltoken + userSig + 我的资料
mitmdump -q -nr ~/.zcode/workspace/default/data/capture/flows_xxx.mitm -s tools/extract_capture.py
# 生成 data/seed/capture_seed.json

# 导入本地库并写入设置
uv run python tools/import_seed.py
```

### ④ 账号密码登录（过滑块，不用抓包）

只要你能在浏览器里过一下阿里云滑块，就不用抓包了：

1. 打开 <https://127.0.0.1:5002/login>（设置弹窗里也有入口）
2. 填账号密码 → 点登录 → 过滑块
3. 后端自动完成 `accountPwdVerifyLogin.htm`，拿到的 `data` 就是 **centraltoken**，
   并自动带出房间信息 / IM 凭证（复用 `/api/probe`）

链路（源码逆向 + 已交叉验证）：

```
POST account.zx093.cn/stoneserver/v1/account/accountPwdVerifyLogin.htm
  accountName = encryptByDES(账号)      DES/ECB/PKCS7，密钥硬编码在小程序里
  pwd         = encryptByDES(密码)
  loginVersion = ""   deviceId = ""     空串，但**要参与签名**
  loginSource = 7     sceneId = f374igpl
  captchaVerifyParam = 阿里云滑块通过后的票据（一次性、短时效）
  timestamp   = 毫秒
  sign        = upperCase(getSign(其余字段))
→ resp.data 就是 centraltoken
```

**DES 密钥**（源码里的默认参数，不是服务端密钥）：

```js
function g(e, t = "T137SRpGil0=") {                      // base64 → 8字节 4f5dfb491a468a5d
  var r = CryptoJS.enc.Base64.parse(t),
      n = CryptoJS.DES.encrypt(e, r, {mode: ECB, padding: Pkcs7});
  return n.ciphertext.toString(CryptoJS.enc.Base64)
          .replace(/\//g, ",").replace(/=/g, "_").replace(/\+/g, ".");
}
```

Python 侧复刻见 `niulai_api.encrypt_by_des` / `decrypt_by_des`（已与 crypto-js
**逐字节对比 6/6 一致**，而且拿抓到的真实密文解出账号正确、重算 `sign` 也与抓到的
逐字节相同）。

`captchaVerifyParam` 的确切形状（实测 280 字符，**`Base64(JSON)`**）：

```json
{"certifyId":"T0AbC1D2Ef","sceneId":"f374igpl","isSign":true,"securityToken":"<128 字符>"}
```

形状由插件的 `verifyType` 决定（见 `plugin.dec.wxapkg`），约牛传了 `success` 回调
→ 故为 **3.0 / isSign 形态**。

> ⚠️ **验证码 Web SDK 的票据形状不一样**（它给的是
> `JSON{sceneId, certifyId, deviceToken, failover}`，没有 `isSign`/`securityToken`）。
> 所以 `/login` 页里的 Web SDK **大概率过不了**约牛后端的核验——试一下就知道，
> 报「验证失败」是预期结果，不影响任何东西。真正可靠的是下面这个抓包器。

> **为什么不能“抓包重放”登录**：`captchaVerifyParam` 是硬前置（没它代码根本
> 不发请求），且 `certifyId` **一次性**，后端还会服务端到服务端再调阿里云核验。
> 所以每次登录都得重新过滑块 —— 这也是不推荐折腾账密登录的原因：
> 用 `tools/capture_token.py` 抓现成的 token 更省事（token 是登录的产物，能管很久）。

想反向验证我们的复刻对不对，就跑抓包器：

```bash
mitmdump -p 8888 --mode upstream:http://127.0.0.1:20122 -s tools/capture_login.py
# 然后在小程序里真点一次账号密码登录，终端会当场打印：
#   ✅ DES 解密成功 → accountName = '...'
#   ✅ sign 复刻正确（重算 = 抓到的）
#   ✅ 拿到 token → 喂给应用
```

该工具**不把明文密码写盘**（样本里连 pwd 的密文都剔除），只把其余字段存到
`data/seed/login_sample.json`。

### ③ 页面里手填

打开界面 → 右上角 **⚙️ 鉴权/设置**：

| 字段 | 从哪来 |
|---|---|
| `centraltoken` | 请求头 `centraltoken`（协议 §2 ⑤） |
| `userSig` | `chatroom/getUserSig.htm` 返回的 `data.userSig` |
| `IM identifier` | 同一个接口的 `data.sdkUserId`，形如 `cu_1234567` |
| `IM sdkAppId` | `chatroom/getSdkAppId.htm`，已知为 `1600075223` |
| `imGroupId` | `chatroom/getByTeacherId.htm`，形如 `@TGS#_@TGS#...` |

填完点 **保存并探测**：会自动调 `getByTeacherId` 校验 token、带出房间信息、
再自动拉 `getSdkAppId` / `getUserSig` / `user/info`，一次填好全部。
不确定 userSig 是否还能用就点 **测试 IM 凭证**（真的连一次腾讯云 IM 做 wslogin）。

### ③ .env 预置
```bash
cp .env.example .env    # 填 NIULAI_CENTRALTOKEN / NIULAI_IM_USER_SIG …
```

首次启动写入数据库；之后以数据库为准（页面里改）。

> **为什么不做自动登录？** 协议 §2 的 SSO 链需要微信 `wx.login` 的一次性 `loginCode`，
> 只能在微信小程序里取得（`niulai_api.SsoClient` 已实现完整链路，可传 loginCode 调用）；
> 附 1.5 的账密登录又要过阿里云滑块（服务端二次核验，重放无效）。
> 所以本地工具以「粘贴 centraltoken」为主路径，这也和 hexun 工具的做法一致。

---

## Web 界面

打开 `https://127.0.0.1:5002`。

| 功能 | 说明 |
|------|------|
| **日历（顶部栏）** | `日期 [2026-09-18] ◀ 前一天 今天 后一天 ▶`，默认就是**今天**。选任意一天 → 该天消息**时间正序**全量载入，并停在**当天最新处**（底部） |
| **日期分隔条** | 列表里跨天会插一条 `2026 年 9 月 18 日 · 星期五`，只有时刻看不出是哪天的问题不再有 |
| **顶部计数会报实际范围** | `2026-09-18（日历） · 已加载 3136 / 3136 条 · 09-18 06:31 → 09-18 23:29`，一眼确认在看哪天 |
| **范围模式** | 近3天 / 近7天 / 全部：取最新的 1200 条并停在最新，**滚到列表顶部自动接更早的 1000 条**（保持阅读位置不跳） |
| **双栏布局** | 左栏「🎓 老师观点」（`userType=4`），右栏全部消息 |
| **标记只有两种** | `老师`（user_type=4）与 `回复`（老师回复了具体某人，`toUserId>0`）。~~实时/VIP/私聊回复~~ 已移除，原因见下方协议表 |
| **实时消息** | 腾讯云 IM 推送 → 落库 → WebSocket 广播到浏览器；贴底时自动追加，离开底部显示「N 条新消息」浮动条 |
| **时间范围** | 今天 / 近 3 天 / 近 7 天 / 全部，一键切换 |
| **关键词搜索** | 跨日期搜索并**高亮**命中词；可按用户 ID 搜索（点昵称即「只看 TA」） |
| **快捷定位** | 9:15 集合竞价 / 9:30 早盘 / 11:30 午间 / 13:00 午后 / 14:30 尾盘 / 回到顶部 / 回到底部 |
| **发送消息** | Enter 发送、Shift+Enter 换行，长文本自动增高 |
| **上传图片** | 📎 选择或 ⌘V/Ctrl+V 粘贴截图，先上传预览（可点 ✕ 移除），再发送；限 2MB（约牛 OSS 限制）|
| **图片显示** | 消息里的截图按原宽高比占位、懒加载，点击进入**可缩放/拖动/双击复位**的大图模式 |
| **头像与曾用名** | 头像走本地缓存代理，离线可看；悬停昵称显示该用户历史昵称与发言数，别名显示「曾」标记 |
| **引用回复** | 约牛老师的回复消息带 `quoteContent`，渲染成可点击跳转的引用条 |
| **同步历史** | 「📥 同步最新」只拉最新几页（日常增量，遇到已有消息即停）；「⏬ 全量同步」从最新一直翻到最早，顶部进度条实时显示「N 次请求 / M 条」，幂等不会重复 |
| **只看今天 / 看某天** | 默认就落在**最新消息**（不再从头播）；顶部进度条下面有日期选择器，可选任意一天**时间正序通读**全天 |
| **滚动加载更早** | 一天 3000+ 条，首屏只载最新的 1200 条；滚到列表顶部会**自动接更早的 1000 条**，且保持当前阅读位置不跳 |
| **图片/头像本地化** | 点「🖼 补齐图片」把库里引用到的**全部图片与头像**下载到 `data/cache/`，之后**断网也能翻完整历史**。启动时会自动补齐（可用 `MEDIA_AUTOFETCH=0` 关掉），按钮上随时显示还差几张 |
| **数据概览 / 备份** | 各天消息数与老师消息分类统计，并显示媒体本地化进度；`💾 备份` 存 SQLite 到 `data/backups/` |

### 状态提示

顶部圆点：🟢 实时已连接 / 🟡 连接中·断开重连 / 🔴 凭证失效 / ⚪ 未启用。
任一凭证缺失或失效时，顶部出现黄色横幅提示并给出「去设置」入口。

### 纯离线查看历史

图片和头像默认是**浏览到才下载**（懒加载），所以刚同步完只能看到一小部分。
点顶部的 **🖼 补齐图片** 会把库里引用到的媒体全部抓到本地：

```
data/cache/images/    463 张  ─ md5(url)[:16]_原名.png
data/cache/avatars/   199 张  ─ 同上
```

- 目标取自数据库（`messages.msg_type in (1,2)` 里的 `imgUrl`/`mainImageUrl` + `users.avatar_url` 去重），
  不是靠“你点过什么”，所以**重跑幂等、已下载的会跳过**，中断重启接着补。
- **启动后 20 秒自动补齐一次**（等到首屏加载完）。注意是**每次启动只跑一次**，不是定时轮询；
  运行中新增的图片由 `_schedule_prefetch()` 顺手下载（内存队列上限 400，重启即丢），
  而重启后那一次自动补齐正好把丢掉的那些找回来 —— 两者互补。不想让它自动跑就设 `MEDIA_AUTOFETCH=0`。
- 按钮文案就是进度：`🖼 补齐图片 (421)` → `🖼 补齐中 120/662` → `🖼 图片已离线`。
- 实测：662 个目标（图片 463 + 头像去重后 199）、共 **267 MB**、4 并发、约 4 分钟下完，0 失败。
- 命中本地后 `/api/media` 直接读磁盘（实测 814KB 的图 13ms），**根本不发网络请求**。

> 媒体目录 **不在** `💾 备份` 的范围内（那个只拷 SQLite）。图片理论上能从 CDN 重下，
> 但它们属于服务端资源、可能被清理，想长期留存就自己把 `data/cache/` 拷走。

### 贴底只做一次

“贴到底部”只应该在**刚打开 / 切换日期（或范围）**时执行一次；之后你自由浏览，
不要再有任何自动滚动。另外**两栏的滚动位置完全独立**：你翻左栏时右栏来新消息，
不应该把左栏也拉下去。规则：

- 加载 → `renderMessages(..., followBottom=true)` → 两栏各贴底**一次**。
- 实时新消息 / 图片撑开 → 只调 `followList(那一栏)`，而且只在该栏本来就贴着底部时才动。
- `scrollToBottom()`（同时滚两栏）**只给用户主动动作**：回到底部按钮、新消息提示条。
- 你已滚上去时，后台同步完成不重载视图，只累加「N 条新消息」提示。

---

## 命令行工具

```bash
# 1) 一键抓 centraltoken（mitmproxy addon，抓到一个就自动写入应用）
mitmdump -p 8888 --mode upstream:http://127.0.0.1:20122 -s tools/capture_token.py

# 2) 命令行全量同步（不跟浏览器并行跑；命令行为主）
uv run python tools/sync_all.py                 # 全量，翻到最早
uv run python tools/sync_all.py --days 3        # 只同步近 3 天
uv run python tools/sync_all.py --source 4      # 同步老师私聊回复流
uv run python tools/sync_all.py --status        # 看库里现有情况

# 3) 从抓包文件导出种子（离线，只读）
mitmdump -q -nr <flows.mitm> -s tools/extract_capture.py

# 4) 种子入库
uv run python tools/import_seed.py [seed.json]

# 5) IM 消息解析 + 入库全链路自检（用抓包里的真实 msg_push 帧，不联网）
uv run python tools/test_im_parse.py

# 6) 端到端自检：真实推送帧 → 解析 → 去重入库 → WebSocket 广播（起临时服务，不联网）
uv run python tools/test_e2e.py

# 7) IM 握手/收消息探针（联网，只读：登录 + 进群 + 打印推送，不发消息）
uv run python tools/im_probe.py --identifier cu_1234567 --usersig "<userSig>" --seconds 60
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

print(api.get_sign({"timestamp": "1789651672765", "deviceId": "110110"}))
# → 4FAF606F9DBEC7CD6C965D9080E91DA9
```

```python
import asyncio
from niulai_im import TencentImClient

async def main():
    c = TencentImClient(1600075223, "cu_1234567", "<userSig>",
                        ["@TGS#_@TGS#XXXXXXXXXXXXXXXX"],
                        on_message=lambda m: print(m["nick_name"], m["msg_content"]))
    await c.run_forever()
asyncio.run(main())
```

---

## 项目结构

```
niulai/
├── protocol.md              协议分析文档（本项目的实现依据）
├── niulai_api.py            协议客户端：sign / SSO 登录链 / 业务 REST / OSS 直传
├── niulai_im.py             腾讯云 IM WebSocket 客户端（登录/心跳/进群/收推送/回执）
├── db.py                    SQLite 存储层（消息/用户/曾用名/头像/媒体/房间/设置）
├── app.py                   Quart 后端：REST API + WebSocket 广播 + IM 常驻监听 + 媒体缓存
├── run.py                   Hypercorn 启动（HTTP/2 + HTTPS）
├── templates/index.html     前端（单文件，零依赖）
├── tools/
│   ├── capture_token.py     一键抓 centraltoken（mitmproxy addon，抓到即自动写入应用）
│   ├── sync_all.py          命令行全量/按天同步
│   ├── extract_capture.py   从抓包文件导出种子（只读）
│   ├── import_seed.py       种子入库
│   ├── im_probe.py          IM 通道探针（登录/进群/打印推送）
│   ├── test_im_parse.py     用真实推送帧验证解析+入库
│   └── test_e2e.py          端到端自检（推送→入库→WS 广播）
├── data/                    运行期数据（gitignore）
│   ├── niulai.db            SQLite 主库
│   ├── cache/images|avatars 图片/头像本地缓存
│   ├── backups/             数据库备份
│   ├── seed/                抓包导出的种子
└── _ref/                    开发期探针历史版本（gitignore，仅备查）
```

---

## 协议实现要点

实现过程中踩到的坑与关键结论（都在代码注释里标了）：

| 点 | 结论 |
|---|---|
| `sign` 算法 | 盐 `asdasdsadfg`；丢 `channel` → `k=v` 字典序 → `&key=盐` → MD5 大写。已用 6 组抓包样本验证 |
| 业务鉴权 | 请求头 `centraltoken` + `accesssource: 6`；失效返回 `{"status":200001}` |
| 历史分页 | `direction:1` + `cursorId=当前页最旧一条的 id`，向历史翻页；`direction:0` 则是向新翻。`sourceType` 1=房间 4=老师私聊。**翻到最早会返回空数组**，就是到底了 |
| **`pageSize` 无上限** | 实测 15/50/200/500/1000 都按需返回（1000 条约 0.95s，2000 开始不稳）。默认 15 是客户端自选的，服务端并未限制 → 全量同步用 1000/页，比 15/页 快 60 倍 |
| **历史只给 7 天** | 房间信息的 `visibleDays: -7` 就是历史可见窗口。实测翻到第 7 天（2026-09-12 00:00:58）就返回空，**共 14,422 条 / 409 人** —— 这就是能拿到的全部，不是同步没跑完 |
| 单日量级 | 一天 2600~3200 条（交易时段密集）。所以单次 `size` 必然不够看，前端要「先取最新 + 滚到顶再补更早」 |
| 消息去重 | REST 记录有 `id`；IM 推送有 `imMsgSeq`；**同一条消息 IM 会推两遍**（sync + push 双副本），按 `MsgSeq` 去重 |
| 图片直传 | `getPolicy.htm` 拿凭证 → multipart 直传 OSS，字段：`name/policy/OSSAccessKeyId/success_action_status=200/signature/key/file`；单文件 ≤2MB；对象名为客户端生成 UUID |
| 图片读取 | `https://fileoss.zx093.com` + `imgUrl`，**完全无鉴权**，拿到 URL 即可下载 |
| IM 端点 | `wss://<sdkAppId>w4c.my-imcloud.com/binfo?sdkappid=…&instanceid=<32hex>&random=…&platform=8&host=mac&version=-1&sdkversion=4.4.3&compress=gzip`（binfo 本身就是 WebSocket 升级请求） |
| **IM 帧方向性** | **客户端必须发二进制帧**（UTF-8 JSON 的 bytes）。发同内容的 **text 帧服务端会静默丢弃**（表现为连上后毫无响应）——这是最容易卡住的一点 |
| IM 服务端帧 | 二进制帧，前 4 字节 `b"COMP"` 表示流式 gzip（`Z_SYNC_FLUSH` 无 trailer）。`gzip.decompress` 会 EOFError，必须 `zlib.decompressobj(31)` |
| IM 握手顺序 | 连上 → `heartbeat.alive` →（收到 ack 后）`im_open_status.wslogin`（带 `identifier`/`usersig`，`cs:0`）→ 拿 `A2Key`/`TinyId`/`InstId`，之后所有帧头都要带 |
| **IM 心跳间隔** | 实测真实客户端**每 ~26s** 发一次 `heartbeat.alive`（样本 29,28,26,11,11,10,27,14…）。服务端在下发的 `HelloInterval` 是 **120**，但真按 120s 发会被网关当空闲连接踢掉（`1006`）→ 按实测 25s 发，200s 无断开 |
| IM 登录频率 | 两次 `wslogin` 至少隔 **15s**（SDK 源码 `_isLoginFrequencyExceeded` 里的 `15e3`）。违反后报误导性的 `70402 Invaild parameters` 或直接 1006，容易被误判成参数错 |
| IM `cs` 字段 | 除 `heartbeat.alive`/`wslogin` 等白名单指令外，`cs = CRC32(JSON(body))`；白名单内固定 0 |
| IM 收推送 | `im_open_push.msg_push`，`EventArray[].Event`：3=聊天消息、4=群提示（进出场/在线人数）；**必须回 `openim.ws_msg_push_ack` 带 `SessionData`**，否则重复推 |
| 消息体来源 | 推送里 `CloudCustomData` 是约牛业务 JSON（与 REST 记录**同构**），优先用它；缺失时回退 `MsgBody`（`TIMTextElem`/`TIMImageElem`） |
| 推送去重 | 推送里同一条消息会出现 **`id` 相同、`MsgSeq` 不同** 的两份副本；落库以 `id` 为主键即可幂等（实测 38 个推送帧 → 24 条消息 → 12 条唯一） |
| IM 推送体积 | 群提示（`Event=4`）带 `MemberNum` 在线人数，可用于展示实时人数 |
| 头像 | `avatarUrl` 形如 `/yzt/public/upload/user/...`，同样走 `fileoss.zx093.com` 无鉴权读取 |
| **IM 推送不能信标记字段** | 同一条消息：IM `CloudCustomData` 给的是 `privateMessageFlag=true / vipUser=true / auditStatus=0`，而 REST 历史里是 `false / false / 1` —— **IM 推的是「审核前」版本，这几个字段是占位值**（14,299 条 REST 记录里这三个字段全是 0/0/1）。所以入库时取中立值、等 REST 回填，前端不用它们做标记 |
| **消息类型（实测修正）** | `msgType`：`0`=文本、`1`=图片、**`2`=文章推送卡片** —— 即老师发的「早盘预案 / 知识点小结」付费文章，**不是语音**。载荷 JSON：`{title, brief, sourceId, sourceTime, sourceUrl, mainImageUrl}`；`sourceId` = 文章 ID（等于 `sourceUrl` 里的 `articleId`）；`sourceUrl` 是小程序内 H5 路径，实测浏览器打不开 |
| **msgType=2 = 内参卡片** | 老师发的付费内参文章（盘前「早盘预案」+ 盘后「知识点小结」）。载荷 `{title, brief, sourceId, sourceTime, sourceUrl, mainImageUrl}`，`checkCode` 是房间级固定校验码 |
| **内参卡片打不开（约牛自己也不行）** | 小程序的卡片点击处理只认 `msgContent.url / .link / .href`，而卡片里叫 `sourceUrl` → 落在 else 分支上弹 toast **「内参详情接入中」**。另：`product.zx093.com/yngp/yngp_app/article/queryArticleDetail.htm?articleId=<sourceId>` 无需鉴权可调，但**不是同一个 ID 空间**（拿 10064 查到的是 2023 年另一位老师的文章且正文为空）。所以前端只做展示卡片 |
| **`contain-intrinsic-size` 的坑** | `.msg` 用了 `content-visibility:auto` + `contain-intrinsic-size: 0 60px`，滚动时估算高度被真实高度替换 → `scrollHeight` 反复变 → 滚动条抽搐。改成先写 `0 64px` 再写 `auto 64px`（不支持 `auto` 的浏览器自动回退） |
| **自动贴底必须“分栏”且“只一次”** | `onRealtimeMessage` 里写着 `if (stickToBottom) scrollToBottom(true)` —— 而 `stickToBottom` **只跟踪右栏**、`scrollToBottom()` **同时滚两栏**。于是你在左栏翻历史时，右栏每来一条实时消息就把左栏也拽回底部（IM 是活的 → “隔一会儿自动贴底”）。现在自动跟随一律走 `followList(那一栏)`；`scrollToBottom()` 只给用户主动动作 |
| **分批渲染结束后的贴底要听 `followBottom`** | `_renderBatch` 渲染完无条件 `pinToBottom(allList)`：`st.followBottom` 算了却没用，把你已滚上去的状态又改回“跟随”；而且写死 `allList`，左栏分批完从不贴底。现在只在 `followBottom` 为真时贴，且两栏一致 |
| **快捷定位要两栏一起跳** | `jumpTo()` 原来只对 `allList` 里的节点调 `scrollIntoView`，左栏纹丝不动；而且基准日用了 `toISOString()`（UTC，比本地早 8 小时会错一天），那段“同一天优先”永远不命中。现在两栏都跳，基准日取本地日期，目标时间直接算成 ts（不再对每条节点 `allMessages.find` 一次，那是 O(n²)） |
| **媒体缓存必须“以库为准”才行** | `_prefetch_queue` 是内存队列 + `PREFETCH_LIMIT=400`，只在同步时触发、重启就丢 → 图片只补到 107/463、头像 133/410。改成从数据库扫出全部引用（`media_targets()`）+ 单飞幂等补齐后，一次跑完 662/662 |
| **`media_status()` 不能连实时状态一起缓存** | 它算计数要遍历 662 个目标（慢，所以要缓存 30s），但把 `running/finished/task_*` 一起缓存后，**新任务启动后 30s 内会返回上一次的 `finished=True`**，UI 会显示错状态（我自己就被这个骗过一次，以为任务没跑）。现在只缓存计数，实时字段每次现取 |
| **`loadToday()` 是同步函数** | 启动代码写的 `loadToday().then(loadStatus)` 会直接抛 `TypeError: reading 'then'`，把紧跟其后的 `loadMediaStatus()` 和 `setInterval` 全带崩（“补齐图片”按钮永远是初始文案就这个原因）。现在 `loadToday` 返回 Promise，且链尾挂 `.catch().finally()` |
| **`/api/media` 不能在事件循环里下载** | `cache_media` 内部是 `requests`（最长 30s 超时），直接写在 `async def` 里会卡住整个事件循环。已改 `await asyncio.to_thread(...)`（同理 `/api/backup` 打包 267MB 也丢线程池） |
| 真正的「老师回复某人」 | 信号是 `user_type=4 且 toUserId>0`（实测 396 条），不是 `privateMessageFlag`（恒为 0）。引用快照在 `quoteContent` 里，其 `id` 才是引用目标（`/api/messages` 的 `type=reply` 就是这个筛选） |
| **API 排序约定** | `/api/messages?order=desc` 服务端会**把结果反转为时间正序**再返回（前端好直接 append + 贴底），调用方**不要再 reverse**。踩过：左栏老师栏多反了一次 → 变新→旧，贴底后底部是早上 08:11 的文章卡片，最新那条反而被顶到看不见的顶部 |
| 写入性能 | 逐条 `SELECT+INSERT` 时，1000 条消息要跑 ~5000 条 SQL，全程占着全局锁 → asyncio 事件循环被冻住，网页要 **20~30 秒**才响应（TLS 握手都超时）。改成每批一次 `SELECT` + `executemany`（6 条语句/批）+ 250 条一批让出 GIL 后，同步期间 `/api/status` 稳定 **30~45ms** |

---

## 常见问题

**Q：页面打开只有"暂无消息"？**
A：本地库还是空的。先 `tools/import_seed.py` 导入抓包数据，或配好 `centraltoken` 后点「📥 同步历史」。

**Q：提示"登录失效，请更新 centraltoken"？**
A：`centraltoken` 是小时级令牌（协议 §9 未测出确切有效期）。重新抓一次包，
或从抓包文件里重新导出：`mitmdump -q -nr flows.mitm -s tools/extract_capture.py`，
然后在设置里粘贴 → 保存并探测。

**Q：点「全量同步」很快就说"完成"，是不是没同步完？**
A：是同步完了。约牛只开放**最近 7 天**（房间信息里的 `visibleDays: -7`），
翻到第 7 天服务端就返回空数组。进度条会显示「已到最早（共 N 条）」。
想看单日全量就在上面的日期框选那天 → 「看这天」。

**Q：点「今天」后停在了早盘（比如 06:31），而不是最新的消息？**
A：已修。原因很隐蔽：清空列表时浏览器会把 `scrollTop` 夹回 0 并**触发一次 scroll 事件**，
贴底前的滚动监听会把 `stickToBottom` 置 false，于是后续的“贴到底部”第一步就被拦住了。
现在“落地到底部”不再依赖 `stickToBottom`（并用 `_pinning` 屏蔽清空/追加引发的伪滚动事件），
且在懒渲染与图片撑开后会重试几次。验证：模拟该场景后 `scrollTop` 会被赋值 4 次并停在底部。

> 模板改动后需要**重启服务**（Quart 缓存 Jinja 模板），并让浏览器强制刷新（⌘⇧R）。

**Q：老师栏（左栏）为什么底部不是最新一条？**
A：已修。两个原因叠在一起：① API 对 `order=desc` 已返回时间正序，前端有多 `.reverse()` 了一次，
变成新→旧，所以贴底后看到的“最后一条”实际上是当天最早的；② 最新那条被顶到了看不见的顶部。
现在左右栏都是时间正序、贴底即最新（实测：首条 08:11 早盘预案，末条 15:25 最新回复）。

**Q：用中文输入法打字，按回车确认候选词时消息直接被发出去了？**
A：已修。原来的 `onSendKeydown` 只判断 `ev.key === "Enter"`，而“回车确认候选词”
也是一次 Enter，于是被当成发送；更糟的是那个 `preventDefault()` 还会连带吃掉
输入法的确认动作（你打的字根本没落进输入框）。

坑在事件顺序：**Chromium（Edge/Chrome）是 `compositionend` → `keydown`**，
所以在 keydown 里 `ev.isComposing` 已经是 `false`，单靠它挡不住；
Safari/Firefox 的顺序又不一样。现在三种信号一起用（`isImeEnter()`）：

1. `imeComposing` —— `compositionstart` 到 `compositionend` 之间
2. `ev.isComposing` / `ev.keyCode === 229` —— 标准与旧版信号
3. `compositionend` 之后 **80ms** 宽限窗口 —— 专门挡 Chromium 那次确认回车

确认候选词的回车**不** `preventDefault()`，交给输入法正常提交；
发送框（`onSendKeydown`）和搜索框（`onSearchKeydown`）都走这套判断。

**Q：老师栏往上翻时滚动条抽搐、停一会又自动贴底？**
A：有两个来源，都修了。

① **自动贴底**：`onImgLoad()` 和 `onRealtimeMessage()` 以前都调 `scrollToBottom()`，
而那是**同时滚左右两栏**的。所以你翻左栏时，右栏每来一条实时消息（IM 是活的）
就把两栏又拽到底 —— 图像加载完也会触发一次。
现在自动跟随一律走 `followList(那一栏)`：只动「本来就贴着底部」的那一栏。
`scrollToBottom()` 只保留给用户主动动作（回到底部按钮、新消息提示条）。
另外你已滚上去时，后台同步完成不再重载当前视图，只累加「N 条新消息」提示。
“贴到底部”只在**加载/切天/切范围**时执行一次。

② **滚动条抽搐**：`.msg` 的 `content-visibility:auto` 配合固定的
`contain-intrinsic-size: 0 60px`，滚动时估算高度被真实高度替换导致 `scrollHeight` 反复变。
现改为 `auto 64px`（现代浏览器会记住真实高度，旧的自动回退到固定值）。

**Q：老师栏里 `{"brief":"早盘预案",...}` 是什么？是文件吗？**
A：不是文件，是 **`msgType=2` 文章推送卡片** —— 老师每天发的两篇付费文章：
盘前 `早盘预案`、盘后 `知识点小结`（七天共 9 条）。载荷 `{title, brief, sourceId,
sourceTime, sourceUrl, mainImageUrl}`，`sourceUrl` 是小程序内 H5 路径
（`/touguapp/tougu/index.html?path=/articleDetail&articleId=10064&...`），
实测在 `www.zx0093.com` / `ht.zx0093.com` / `app.zx093.cn` 都是 404，**只能在小程序里打开**。
现在渲染成卡片（标签 + 标题 + 时间 + 文章号），悬停可以看到原始路径。

> 能不能点开？**不能，约牛自己也不能**：小程序里卡片点击只认 `msgContent.url / .link / .href`，
> 而这个卡片里叫 `sourceUrl` → 落到 else 分支弹 toast「内参详情接入中」。
> 我也试了 `product.zx093.com/yngp/yngp_app/article/queryArticleDetail.htm?articleId=<sourceId>`
> （无需鉴权可调），但它**不是同一个 ID 空间**，拿 10064 查到的是 2023 年另一位老师的文章、正文还是空串。

**Q：为什么看不到"VIP / 私聊回复 / 实时"标记了？**
A：因为那几个标记是错的。它们来自消息记录里的 `privateMessageFlag`/`vipUser` 字段，
而 IM 实时推送给的是**审核前**版本（这两个字段是占位值 `true`），REST 历史里 14,299 条全是 `false`。
现在只显示数据上站得住的 `老师` 和 `回复`；"实时"标签直接去掉了（没有信息量）。

**Q：怎么看到某一天的全部消息？**
A：顶部栏的**日历**选那天（或用 ◀ 前一天 / 今天 / 后一天 ▶），该天消息会时间正序全部载入
并停在最新处，往上就是当天的早盘。注意约牛只开放最近 7 天，往前翻到 09-12 就到底了
（点"后一天"不会超过今天）。

**Q：为什么打开只加载 1200 条，而不是全部？**
A：在**范围模式**（近3天/近7天/全部）下，一天就 3000+ 条，全量渲染会让浏览器卡，
所以首屏先取最新的 1200 条并停在底部，**滚到列表顶部会自动接着加载更早的 1000 条**。
想看一整天就用顶部日历选那天，会把当天全部载入。

**Q：顶部圆点红色 / 黄色？**
A：红色 = `userSig` 过期（IM 登录被拒），重新调 `getUserSig` 拿新的；
黄色 = 网络抖动，客户端会自动重连（指数退避，最长 60 秒）。

**Q：能回复某条消息吗？**
A：**不能**。协议 §5 的 `sendMessage.htm` 只接受 `chatRoomId/msgType/msgContent` 三个字段，
没有引用字段；老师那侧的 `quoteContent`/`toUserId` 是服务端在审核回复时写入的，
客户端没有对应入口。界面上只做引用**展示**与跳转。

**Q：服务为什么用 HTTPS + HTTP/2？**
A：和 hexun 一致，多路复用下大量图片并发不互相阻塞。自签证书浏览器首次会提示，点继续即可。

**Q：图片会重复下载吗？**
A：不会。`/api/media` 按 URL 的 md5 命名缓存到 `data/cache/`，带一年强缓存头；
新消息入库时还会后台预取头像与图片。

---

## 免责声明

本项目仅用于**个人学习与对自己有权访问的数据做本地归档**：
所有接口、签名算法与实时协议均来自对**本人账号**流量的分析。

- 不含任何破解、绕过付费或越权能力：付费可见字段（`feeStatus`）由服务端控制；
- 请遵守约牛用户协议与相关法律法规，不要用于批量采集、骚扰他人或商业传播；
- 抓取频率已做退避（每页 0.35s），请勿调高；
- 因使用本工具产生的一切后果由使用者自行承担。
