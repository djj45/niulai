# 约牛聊天室小程序协议分析

> 抓包环境：macOS + 微信 4.x 桌面版小程序（WeChatAppEx/Chromium 内核）
> 抓包方式：mitmproxy 12 上游串接 sing-box，系统代理切至 `127.0.0.1:8888`，CA 证书入系统钥匙串
> 分析时间：2026-09-17 21:24 – 21:47（样本房间：chatRoomId=298「知行合一交易逻辑专栏」，teacherId=328）
> 原始数据：`~/.zcode/workspace/default/data/capture/`（flows_20260917_212459.mitm 完整流量 / chat_apis.jsonl HTTP摘要 / ws_decoded*.txt WS解码 / history_messages.txt 历史消息 / images/ 图片原件）

> **⚠️ 2026-09-24 迁移：小程序接口下线，客户端全面迁到网页版（见 附3）。**
> 鉴权仍是 centraltoken 头、DES/sign 算法不变、IM 网关不变；变化集中在：
> 聊天室三接口换 `/touguServer/client/community/` 前缀、验证码场景换 `17n9bhbp`、
> UA/Referer 换浏览器身份（不再需要 accesssource）。工具侧 `niulai_api.py` 已迁移并通过在线验证。

## 0. 总体架构

```
┌────────────── 微信小程序 (appid: wx0c0819078db5e3e1) ──────────────┐
│                                                                    │
│  登录/业务/历史/发消息          图片上传                实时收消息    │
│  HTTPS REST (JSON)             阿里云OSS POST直传      腾讯云IM WS   │
│  account.zx093.cn              filecdn-prod.           wss://       │
│  touguapi.zx093.com            oss-cn-beijing.         my-imcloud   │
│  stat.zx093.cn(埋点)           aliyuncs.com                         │
│        │                             │                            │
│        └── centraltoken 鉴权头 ──→ getPolicy.htm 发凭证             │
└────────────────────────────────────────────────────────────────────┘
图片读取: fileoss.zx093.com (OSS同bucket的CDN域, 无鉴权直链)
```

- **发消息（文本/图片）走约牛自建 REST**，不直接发腾讯 IM；约牛后端落库后转投 IM 群
- **收消息走腾讯云 IM WebSocket 长连推送**
- **历史消息走 REST 游标分页**，与 IM 通道独立（同一消息两条通路都有副本）

## 1. 域名清单

| 域名 | 用途 |
|---|---|
| `account.zx093.cn` | SSO 登录/换票（ssoserver） |
| `touguapi.zx093.com` | 聊天室业务主 API（touguServer） |
| `fileoss.zx093.com` | 图片/头像 CDN 读（OSS bucket 公开域） |
| `filecdn-prod.oss-cn-beijing.aliyuncs.com` | OSS 上传直传端点 |
| `stat.zx093.cn` | 埋点上报（smartlog） |
| `<sdkAppId>w4c.my-imcloud.com` | 腾讯云 IM 接入/长连（本例 1600075223w4c） |
| `web.sdk.qcloud.com` | IM SDK 静态资源/错误码表 |
| `ynimg.zx093.cn`、`www.zx0093.com` | 静态图/官网 |

## 2. 登录与鉴权链

全部 POST，表单编码（`timestamp` 毫秒级）：

```
① account.zx093.cn/ssoserver/login/xcx/getAuthToken.htm
   timestamp=<ms>&deviceId=110110&sign=<32位HEX大写>
   → data.authToken     (长效中间票据)

② account.zx093.cn/ssoserver/login/xcx/bind/getUnionidAndStatus.htm
   timestamp&loginCode=<wx.login码,一次性>&loginSource=7&authToken
   → unionid 状态

③ account.zx093.cn/ssoserver/login/xcx/estr.htm
   authToken&timestamp&sign → data.estr (16进制串, 用途待定)

④ account.zx093.cn/ssoserver/login/xcx/bindPhone.htm
   timestamp&loginCode&authToken → 绑定手机号状态

⑤ account.zx093.cn/ssoserver/login/xcx/authTokenExchangeToken.htm
   timestamp&authToken&sign
   → data = centraltoken (三段逗号分隔长串, 业务会话令牌)

⑥ GET account.zx093.cn/ssoserver/user/getInfo.htm → 用户资料(nickName/photo/estr…)
```

- `sign`：**已逆向**（见下），盐为固定串 `asdasdsadfg`
- `deviceId` 桌面端固定 `110110`
- 登录态过期时业务接口返回 `{"status":200001,"message":"登录失效"}`，小程序自动重走①→⑤

**sign 算法**（从小程序源码 `app-service.js` 逆向，已用 6 组抓包样本验证 6/6 通过）：

```python
import hashlib

SALT = "asdasdsadfg"   # 小程序源码里的固定盐

def get_sign(params: dict) -> str:
    p = {k: v for k, v in params.items() if k != "channel"}   # 丢弃 channel 字段
    s = "&".join(sorted(f"{k}={v}" for k, v in p.items()))    # k=v 按字典序排序后 & 连接
    s += f"&key={SALT}"                                        # 追加盐
    return hashlib.md5(s.encode()).hexdigest().upper()         # MD5 后转大写
```

例：`{"timestamp":"1789651672765","deviceId":"110110"}`
→ 待签名串 `deviceId=110110&timestamp=1789651672765&key=asdasdsadfg`
→ sign `4FAF606F9DBEC7CD6C965D9080E91DA9`（与实抓一致）

**业务请求统一头：**

```
centraltoken: <⑤的返回值>
accesssource: 6
content-type: application/json
referer: https://servicewechat.com/wx0c0819078db5e3e1/<ver>/page-frame.html
```

## 3. 业务 REST API（touguapi.zx093.com/touguServer/app）

| 接口 | 方法 | 请求 | 响应要点 |
|---|---|---|---|
| `chatroom/getByTeacherId.htm` | POST | `{"teacherId":328}` | 房间信息：`id`(=chatRoomId 298)、`imGroupId`(@TGS#…)、`name`、`teacherName/realName`、`certificateNum`(投顾证书)、`enableDm`、`visibleDays` |
| `order/isHasPermission.htm` | POST | `{"teacherId":328}` | `orderStatus`(2=无有效订单) |
| `chatroom/getSdkAppId.htm` | GET | — | `sdkAppId:"1600075223"` |
| `chatroom/getUserSig.htm` | GET | — | `sdkUserId:"cu_<数字>"` + `userSig`(腾讯IM签名, base64-ish) |
| `chatrecord/getChatRecordList.htm` | POST | 见 §4 | 历史消息数组 |
| `chatrecord/sendMessage.htm` | POST | 见 §5 | `{"status":1,"data":{"result":true}}` |
| `oss/common/getPolicy.htm` | GET | `?directory=imgchat` | OSS 上传凭证，见 §6 |
| `user/getInfo.htm` | GET | — | 用户资料 |

## 4. 历史消息（游标分页）

```
POST /touguServer/app/chatrecord/getChatRecordList.htm
{"chatRoomId":298, "cursorId":"", "userType":"", "direction":1, "pageSize":15, "sourceType":1}
```

- `cursorId=""`：拉最新一页；否则传当前页最旧一条的 `id`，`direction:1` 向前翻页
- `sourceType`: `1`=房间消息流，`4`=老师私聊回复流（两路并行请求）
- `pageSize:15`，实测每次上滑触发一次请求

**消息记录 schema**（REST 与 IM 推送的 `CloudCustomData` 同构）：

| 字段 | 说明 |
|---|---|
| `id` | 消息 id（游标就是它，全局递增） |
| `msgType` | `0`文本 `1`图片（2/3 疑似语音/视频，未验证） |
| `msgContent` | 文本=原文字符串；图片=JSON `{"width":W,"height":H,"imgUrl":"/tougu/imgchat/..."}` |
| `nickName` / `ynNo` / `userId` | 昵称 / 约牛编号 / 用户 id |
| `userType` | `1`普通用户 `4`老师(审核后) |
| `quoteContent` | 引用回复的完整被引消息对象 |
| `privateMessageFlag` | 私聊回复标记 |
| `imGroupId` / `imMsgSeq` | 对应 IM 群与消息序号 |
| `auditStatus` / `feeStatus` / `sendStatus` | 审核/付费可见/发送状态 |
| `teacherId` / `toUserId` | 老师回复时带目标用户 |

## 5. 发送消息

```
POST /touguServer/app/chatrecord/sendMessage.htm
文本: {"chatRoomId":298,"msgType":0,"msgContent":"美股开得好高"}
图片: {"chatRoomId":298,"msgType":1,"msgContent":"{\"width\":1998,\"height\":1304,\"imgUrl\":\"/tougu/imgchat/20260917/<uuid32>.png\"}"}
```

宽高由客户端测量上报；响应 `{"status":1,"data":{"result":true}}`。消息随后由服务端转投腾讯 IM 群（`imGroupId`）。

## 6. 图片收发全链路

**上传三步：**

```
① GET touguapi…/oss/common/getPolicy.htm?directory=imgchat        [centraltoken]
   → { accessId:"LTAI5t…", dir:"tougu/imgchat/", expire:<ms约1h>,
       host:"https://filecdn-prod.oss-cn-beijing.aliyuncs.com",
       policy:<base64>, signature:<base64> }

   policy(base64解码) = {"expiration":"…Z",
     "conditions":[["content-length-range",0,2097152],
                   ["starts-with","$key","tougu/imgchat/"]]}

② POST https://filecdn-prod.oss-cn-beijing.aliyuncs.com/   (multipart/form-data)
   字段: name, policy, OSSAccessKeyId, success_action_status,
         signature, key="tougu/imgchat/20260917/<客户端生成uuid32>.png", file
   限制: 单文件 ≤2MB；key 前缀锁定目录；凭证 1 小时有效

③ POST …/chatrecord/sendMessage.htm  (msgType=1, 见 §5)
```

**隐私细节**：OSS 对象名是客户端生成的 UUID，本地原始文件名只出现在 multipart 的
`filename` 属性里，不会成为 URL 的一部分。

**读取：** `http(s)://fileoss.zx093.com/tougu/imgchat/20260917/<uuid>.png`
——**完全无鉴权**（连 `centraltoken` 都不要），拿到 URL 即可下载；`/tougu/privatechat/`
路径（私聊图）同样无鉴权。URL 不可枚举（UUID）。

## 7. 腾讯云 IM 实时通道（收消息）

**接入**：先 `GET https://1600075223w4c.my-imcloud.com/binfo?sdkappid=…&instanceid=…&platform=8&host=mac&sdkversion=4.4.3&compress=gzip` 拿接入点，然后建 **wss 长连**（同域名）。

**帧格式（关键）：**

| 帧类型 | 格式 |
|---|---|
| 文本帧 | UTF-8 JSON：`{"head":{"servcmd":…,"seq":…},"body":{…}}` |
| 二进制帧 | `b"COMP"` 4 字节标记 + **流式 gzip（Z_SYNC_FLUSH，无标准 trailer）** |

解码代码（标准 `gzip.decompress` 会报 EOFError，必须用 decompressobj）：

```python
import zlib
def decode_frame(c: bytes) -> str:
    if c[:4] == b"COMP":
        return zlib.decompressobj(31).decompress(c[4:]).decode("utf-8", "replace")
    return c.decode("utf-8", "replace")
```

**主要指令（head.servcmd）：**

| 指令 | 方向 | 说明 |
|---|---|---|
| `im_open_status.wshello` / `wslogin` | C→S | 握手/登录（identifier=cu_xxx + usersig）→ A2Key/TinyId/InstId |
| `heartbeat.alive` | 双向 | 30 秒心跳 |
| `openim.getmsg` / `getroammsg` | C→S | 单聊漫游/同步 |
| `group_open_http_svc.get_joined_group_list` | C→S | 普通群列表 |
| `million_group_open_http_svc.get_joined_group_list` / `apply_join_group` | C→S | 百万群（直播间群）列表/进群 |
| `profile.portrait_get_all` | C→S | 拉资料（昵称头像） |
| `recentcontact.page_get` | C→S | 最近会话 |
| `im_open_push.msg_push` | S→C | **消息推送** |
| `openim.ws_msg_push_ack` | C→S | 推送确认（回传 SessionData） |

**消息推送结构**（`im_open_push.msg_push`）：

```
body.EventArray[]:
  Event=3  聊天消息  → GroupMsgArray[]
      ├─ GroupInfo.From_AccountNick   "昵称 (YN编号)"
      ├─ MsgBody[].MsgType            "TIMTextElem"(文本) / "TIMImageElem"(图片)
      ├─ MsgBody[].MsgContent.Text    文本内容
      ├─ CloudCustomData              约牛业务JSON（与 §4 REST 记录同构）
      └─ MsgSeq / MsgTimeStamp
  Event=4  系统提示  → 进出场/踢人（含 MemberNum 在线人数）
```

- **每条消息推送两遍**（sync + push 双副本），按 `MsgSeq` 去重
- 发送方自己在群里发消息，也通过该推送收到回显

## 8. 抓包环境复现步骤（macOS）

```bash
# 1. mitmproxy 串在现有代理(sing-box 127.0.0.1:20122)前面
mitmdump -p 8888 --mode upstream:http://127.0.0.1:20122 \
  -s log_addon.py -w flows.mitm

# 2. 证书(~/.mitmproxy/ 导入系统钥匙串并信任)后切系统代理
networksetup -setwebproxy "Wi-Fi" 127.0.0.1 8888
networksetup -setsecurewebproxy "Wi-Fi" 127.0.0.1 8888
networksetup -setsocksfirewallproxystate "Wi-Fi" off

# 3. 关键: 把小程序窗口彻底关掉重开(已建立的长连不会自动迁移到新代理)

# 4. 结束后恢复
networksetup -setwebproxy "Wi-Fi" 127.0.0.1 20122
networksetup -setsecurewebproxy "Wi-Fi" 127.0.0.1 20122
networksetup -setsocksfirewallproxy "Wi-Fi" 127.0.0.1 20122
```

离线分析：`mitmdump -q -nr flows.mitm -s <脚本>`（HTTP 响应是 gzip 的要用
`flow.response.content` 而非 `raw_content`；WS 长连消息**延迟落盘**，连接关闭或
定期 flush 后才完整可见）。

## 9. 未覆盖 / 待办

- [x] ~~`getAuthToken` / `authTokenExchangeToken` 的 `sign` 拼接算法~~ **已破解**（见 §2，盐 `asdasdsadfg`，MD5 大写；已用 7 组样本验证）
- [x] ~~账号密码登录的账密加密~~ **已破解并抓包验证**（见 附1.5：DES 密钥 `T137SRpGil0=`）
- [ ] 语音/视频消息格式未验证
- [x] ~~`msgType=2`~~ **已确认不是语音**：是**文章推送卡片**（老师发的付费文章 `早盘预案` / `知识点小结`），载荷 `{title, brief, sourceId, sourceTime, sourceUrl, mainImageUrl}`，七天抓到 9 条（每天盘前 + 盘后各一条），`feeStatus=1`；`sourceUrl` 为小程序内 H5 路径，浏览器打不开
- [ ] `msgType=3`（若存在）仍未遇到样本
- [ ] `estr` 字段用途未明
- [ ] centraltoken 有效期未测（实测同一张 token 用了 ≥3.6 天仍有效，未见过期样本；小程序端重新登录也不会吊销旧 token）
- [x] ~~`getUserSig` 拿到的 userSig 理论上可独立连腾讯 IM 收消息（sdkAppId+userId+userSig），未实测~~ **已实测**：sdkAppId+sdkUserId+userSig 直连 wss 收推没问题；**有效期很短（约几十分钟）**，过期后 wslogin 必 `70402 Invalid parameters`。`getUserSig.htm` 可随时重取、新旧 sig 并存不互踢（小程序就是每次启动重取）；app 侧已做 login_failed 自动续签重连
- [ ] 微信主进程私有协议（消息同步等）不走系统代理，未在本次范围

## 附1.5：账号密码登录与滑块验证（源码逆向 + 抓包验证通过）

滑块 = **阿里云验证码官方小程序插件** `AliyunCaptcha v3.0.0`（provider `wxbe275ff84246f1a4`），
线上 sceneId=`f374igpl`（测试环境 `c613ekby`）。插件代码也解包出来了，在 `plugin.dec.wxapkg`。

```
点登录 → 弹滑块(插件渲染) → 插件内部与阿里云验证服务交互(滑块图/轨迹上报)
       → 通过后回调 codeSuccess(captchaVerifyParam)

POST account.zx093.cn/stoneserver/v1/account/accountPwdVerifyLogin.htm
  accountName = encryptByDES(账号)
  pwd         = encryptByDES(密码)
  loginVersion = ""    deviceId = ""      ← 空串，但**要参与签名**
  loginSource = 7      sceneId  = f374igpl
  captchaVerifyParam   timestamp
  sign        = upperCase(getSign(其余字段))     ← 算法见 §2
→ resp.data **就是 centraltoken**，不需要再走 §2 那五步
```

### DES 密钥（模块级默认参数，不是服务端密钥）

```js
function g(e, t = "T137SRpGil0=") {                   // base64 → 8 字节 4f5dfb491a468a5d
  var r = CryptoJS.enc.Base64.parse(t),
      n = CryptoJS.DES.encrypt(e, r, {mode: ECB, padding: Pkcs7});
  return n.ciphertext.toString(CryptoJS.enc.Base64)
          .replace(/\//g, ",").replace(/=/g, "_").replace(/\+/g, ".");
}
```

Python 复刻见 `niulai_api.encrypt_by_des` / `decrypt_by_des`。已验证：
与 crypto-js **逐字节对比 6/6 一致**；而且用抓到的真实密文解出账号正确、
重算 `sign` 与抓到的**逐字节相同**。

### captchaVerifyParam 的确切形状

**`Base64(JSON)`**，实测 280 字符：

```json
{"certifyId":"T0AbC1D2Ef","sceneId":"f374igpl","isSign":true,"securityToken":"<128 字符>"}
```

形状由插件的 `verifyType` 决定 —— 插件初始化时看调用方传了什么回调：

```js
// plugin/appservice.js
x.success && typeof x.success === "function"
  ? (_Config._extend({verifyType: "3.0"}), delete _Config.captchaVerifyCallback, ...)
  : (_Config._extend({verifyType: "2.0"}), ...)

// 成功路径
r = "1.0" === _Config.verifyType ? n
    : Ot.stringify(Rt.Utf8.parse(JSON.stringify({
        certifyId: n, sceneId: _Config.SceneId, isSign: !0,
        securityToken: _Config.securityToken            // init 时阿里云下发
      })))
```

约牛传了 `success` 回调（页面里的 `codeSuccess`）→ 故为 **verifyType 3.0 / isSign 形态**。

> ⚠️ **验证码 Web SDK 走不通（已实测堵死）**。 实测 SDK 会把 `prefix` **拼进请求域名当
> userTag 校验**（不是官方文档说的“自定义前缀避全局名冲突”）：
>
> | prefix | 实际请求主机 | 阿里云答复 |
> |---|---|---|
> | `"nl"`（编的） | `nl.captcha-open.aliyuncs.com` | `IllegalUserTag` |
> | 不发送 | `undefined.captcha-open.aliyuncs.com` | `IllegalUserTag` |
>
> 不发送时 SDK 直接拼 `undefined` 进域名 → 这个 SDK 里 prefix 是**强制**的。
> 而正确的 prefix 是**配 Web 接入时生成的**：小程序插件声明里没有 prefix，
> 5 个网页站也无任何验证码痕迹 → 他们没配过 Web 接入，**这个值不存在**。
> 另外两边票据形状也不同（插件 `Base64({certifyId,sceneId,isSign,securityToken})`
> vs Web `{sceneId,certifyId,deviceToken,failover}`）。所以账密登录走浏览器这条路
> **只有约牛自己建 Web 接入场景才能通**，客户端这边无法绕过。

### 其它已核实细节

- **sceneId 是构建期常量，不是运行时动态值**。它是 config 模块里的字面量，与环境域名写在同一块：

  ```js
  // 线上环境
  exports.stat = x = "https://stat.zx093.cn", exports.statLog = c = "https://stat-log.zx093.com",
  exports.WS_URL = n = "wss://product.zx093.com", exports.sceneId = r = "f374igpl",
  exports.errReportEvent = U = "err_report"

  // 测试环境（各自一个独立场景，不是同一个场景在变）
  exports.stat = x = "https://stat-test.zx093.cn", exports.WS_URL = n = "wss://product-test.zx093.com",
  exports.sceneId = r = "c613ekby", exports.errReportEvent = U = "test_err_report"
  ```

  扫全部 `.js` 里的 `sceneId`：**没有任何一处是从网络响应赋值的** —— 要么是
  `exports.sceneId = r = "..."`（定义），要么是 `i.sceneId` / `k._Config.SceneId`（读取）。
  所以运行期不变，**只在约牛发新版本、换场景时变**（版本级，不是动态）。

- **双重自证**：`sceneId` 会出现在**登录请求体**里（`sceneId: this.data.sceneId`），
  也会出现在**票据里**（Base64 解开后有 `sceneId`）。实测抓包两者都是 `f374igpl`，
  与源码常量一致 → 到 2026-09-19 为止它没变；万一日后变了，抓包会立刻暴露。

- 本工具把它做成了可配置项（`captcha_scene_id` 设置 > `api.CAPTCHA_SCENE_ID` 常量），
  `tools/capture_login.py` 抓到真实登录流量时会**自动写回新值**，不需要手改。

- 请求层：`if (!ignoreToken) { f[tokenName] = wx.getStorageSync("token") }`，而登录成功
  会 `wx.setStorageSync("token", resp.data)` → 两者是同一个键，故 **resp.data 即 centraltoken**。
- `getSign` 只 `delete channel`，**不过滤空值**（`loginVersion=""`/`deviceId=""` 要参与签名）；
返回**小写** MD5，由调用方 `upperCase()`。
- 约牛后端收到 ticket 后会**服务端到服务端**再调阿里云核验（抓包看不到）。
  `certifyId` 一次性、短时效 → **抓包/重放无法免除滑块**，每次账密登录都要重新过。
- 抓包器：`tools/capture_login.py`（抓到即解账密、重算 sign 比对、并把响应 token 喂给应用）。

另：源码 config 模块泄露了全套测试环境域名（`test-account.zx093.cn`、`touguapi-test.zx093.com` 等）。

## 附1.6：为什么不“从 web 模拟小程序接入”（结论：不值得）

小程序插件能跑、Web SDK 跑不起来，自然想到“那就在浏览器里模拟小程序”。
结论是：**不是做不到，而是代价与收益完全不成比例**。

### 1. 插件和 Web SDK 是两套完全不同的 API 面

| | Web SDK | 小程序插件 |
|---|---|---|
| 端点 | `https://<prefix>.captcha-open.aliyuncs.com/`，明文规范 | **字符串表混淆**：`p(450)+p(478)+p(479)+p(564)+p(517)` 这种索引拼接，还带 `.split("").reverse().join("")` 反转 |
| 端点常量 | 固定几个 | `ENDPOINTS` / `CN_ENDPOINTS` / `INTL_ENDPOINTS` / `WAF_ENDPOINTS` / `apiServers` / `apiDevServers` / `cdnServers` —— 一套带故障转移的服务器池 |
| 身份标识 | **`userTag`（即 prefix）** | 微信插件 provider appid `wxbe275ff84246f1a4` |
| 请求形态 | 直接 JSON | 带 `ACCESS_KEY` / `WEB_AES_SECRET_KEY` / `AES_IV` / `SALT` / `ALGO_TYPE` / `API_VERSION` / **`PLATFORM`** / **`DEVICE_TYPE`** |

### 2. 关键差异：为什么插件不需要 userTag

- Web 接入需要 `userTag`，因为“一个网页”没有天然身份 —— 得让约牛去阿里云控制台**注册一个**
  （他们没做，所以报 `IllegalUserTag`）。
- 小程序插件**不需要**，初始化只传 `{SceneId, mode, success}`，因为**微信本身就是身份** ——
  阿里云通过微信插件 provider appid + 微信运行环境认它。

所以“从 web 模拟小程序接入”的实际含义是**冒充微信小程序环境**。而 `PLATFORM` / `DEVICE_TYPE`
这些字段，加上 `cloudauth-device-*.aliyuncs.com`（阿里云**安全设备指纹**服务，实测返回 `DeviceConfig`），
存在的目的就是识别这件事。

### 3. 就算硬做，收益也是零

| 步骤 | 可行性 |
|---|---|
| 反混淆字符串表（标准 webpack 字符串表，`webcrack` 之类可做） | 可以 |
| 实现它的 AES 加密 + 签名（密钥也在表里） | 可以 |
| **通过阿里云风控**（非微客户端声称自己是小程序） | 不是“实现”，是“赌” |
| **手动拖滑块** | 逃不掉 |

最后一行是决定性的：就算全打通，也只是把“开一次小程序窗口”换成“在自写页面里拖滑块”，
**用户操作量没减少**，中间多了几百行随时会被版本更新打断的逆向代码。

### 4. 该怎么做

`tools/proxy_capture.sh on` → 小程序窗口关掉重开 → 进任意老师聊天室 → `off`。
全程约 10 秒，不涉及冒充平台，也不对抗风控。

---

## 附1.7：centraltoken 寿命追踪

token 失效时约牛**不会主动告知**（只在业务接口返回 `200001`），所以以前只能等后台同步
静默中断才发现。现在把它记成数据：

- 表 `token_log`：`prefix`（只存前 12 位）/ `source` / `set_at` / `set_ts` /
  `ended_at` / `ended_ts` / `lifetime` / `reason`
- **签发**：`probe`（`/api/probe`）、`login`（`/api/login/password`）、`settings`（页面手填）、
  `migrated`（老库迁移，用 `token_saved_at` 回填真实起点）
- **失效**：`_on_token_expired()` —— 挂在 `TouguClient(on_token_expired=...)` 上，
  任何业务请求遇到 `200001` 都会触发（同步 / 发消息 / 上传全覆盖），幂等
- `reason` 区分 `expired`（真的过期）与 `replaced`（被手动换掉）。**统计只算 expired**，
  否则“换了 token”会被误当成“寿命这么短”
- `/api/status` 的 `token` 字段给出 `age_sec` / `eta_sec`（中位数 − 已用）/ `warn`
  （已用 > 中位 80%）/ `lifetime_min|median|max` / 最近 6 条 `history`
- 界面：顶栏 `🕐 已用 N`（悬停看历史与推算）、超 80% 时变黄并进黄色横幅、
  统计弹窗里也列一行

有了几个过期样本后，就能预判“大概什么时候该刷新”，不用等断了才发现。

---

## 附2：Mac 微信 4.x 小程序包（wxapkg）解密方法

路径：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/users/<hash>/applet/packages/<appid>/<ver>/*.wxapkg`（wxid 见 `…/Documents/xwechat_files/` 下目录名）

加密头 `V1MMWX`，4.x 方案（与老 Windows 方案不同，参照 wedecode 项目实现）：

```python
import hashlib  # key = PBKDF2-SHA1(口令=小程序appid, 盐="saltiest", 1000次, 32B)
# AES-256-CBC(iv=b"the iv: 16 bytes", nopad) 解密 raw[6:6+1024]
# 明文 = 解密结果[:1023] + (raw[6+1024:] 每字节 XOR ord(appid[-2]))
# 结果为标准 wxapkg: 0xBE头, offset13=0xED, offset14=u32BE文件数,
#   每项: u32BE名称长度 + 名称 + u32BE偏移 + u32BE长度
```

本次已解包至 `~/.zcode/workspace/default/data/wxapkg/{app,pages_chat}/`（356 个文件）。

## 附：示例会话身份（本次抓包，已过期/截断）

```
小程序 appid : wx0c0819078db5e3e1
IM sdkAppId  : 1600075223
我的 IM 账号 : cu_1234567 (nickName "示例用户 (YCC00000)", TinyId 1441152XXXXXXXXXXXX)
房间         : chatRoomId=298, imGroupId=@TGS#_@TGS#XXXXXXXXXXXXXXXX (Community/百万群, 721+人)
centraltoken : SXQdXXXXXXXXXXXX,…(三段, 登录失效需重换)
```

## 附3：网页版协议（2026-09-24 起，小程序接口下线后的新主通道）

> 入口：`https://tougu.zx093.cn/touguapp/webChatRoom/index.html#/?teacherId=VzPD.Y8fIGs_`
> （URL 里的 teacherId 是邀请码形态；`/app/teacher/getInviteCodeByTeacherId.htm` 可把数字 id 328 换成码 `H8X9`，接口内部仍是数字 328）
> 抓包：Edge DevTools 导出 HAR（`data/capture/web_harr_登录抓包_20260924.har`）+ 前端 JS 静态分析（`data/capture/webjs/`）
> 登录方式：微信扫码 或 账号密码（本工具走账密，与小程序时代同一端点同一套加密）

### 与小程序版的差异（就这几点，其余全部沿用）

| 项 | 小程序（旧） | 网页版（新） |
|---|---|---|
| 聊天室接口前缀 | `/touguServer/app/chatroom|chatrecord/…` | `/touguServer/client/community/chatroom|chatrecord/…` |
| 验证码 sceneId | `f374igpl` | `17n9bhbp` |
| 账密登录体 | 含 `deviceId:""` | 不带 deviceId（其余字段同，sign 同算法） |
| getInfo | GET + centraltoken 头 | POST 表单 `{centralToken}`（头照带） |
| UA/Referer | 微信小程序身份 + `accesssource:6` 头 | 浏览器 UA + `https://tougu.zx093.cn/`，不发 accesssource |

**不变**：centraltoken 头鉴权与三段式格式、DES 账密加密（密钥 `T137SRpGil0=`）、sign（盐 `asdasdsadfg` 排序 MD5 大写）、`accountPwdVerifyLogin` 端点与「响应 data 即 centraltoken」、房间/消息 DTO（新增 `isPinned/bizType/sourceType/sendTime` 等字段，旧字段全兼容）、游标分页（`cursorId`+`direction=1`，网页版固定 pageSize=50）、IM 网关与 userSig（`/client/community/chatroom/getUserSig.htm`，仍是 cu_ 账号体系）。

### 网页版新能力（工具暂未用）

- `GET /client/community/chatrecord/pinnedListById.htm?chatRoomId=…` — 置顶消息（已接入工具：`/api/pinned` + 列表头下方置顶条，点图片开灯箱/文章开阅读弹窗/文本开全文弹窗）
- `/client/article/queryArticleListByPage.htm` / `queryArticleDetail.htm` — 文章（msgType=2 卡片的正文）
- 微信扫码登录：`/ssoserver/login/xcx/qrcode.htm`（multipart，返回二维码图）+ 轮询换 token；跳转小程序用的 appid 是 `wxd188f9c9ac3c6641`（与小程序版 appid `wx0c0819078db5e3e1` 不同，网页版带了另一套）
- 网页版前端发图走 TIM SDK 的 COS 上传，JS 里没有 OSS policy 调用；工具仍用 `/app/oss/common/getPolicy.htm` 直传——**网页版自己就是这条链**（2026-09-24 抓包实测：网页版发图同样 getPolicy→OSS 直传→sendMessage，与工具实现逐字段一致）

### 前端源（可直接读，无需反编译）

```
https://tougu.zx093.cn/touguapp/webChatRoom/js/webChatRoom.<hash>.js      # 加密/签名工具（DES/sign 的 web 实现）
https://tougu.zx093.cn/touguapp/webChatRoom/js/chunk-01b98d02.<hash>.js   # API 路径表 + sendMessage 封装
https://tougu.zx093.cn/touguapp/webChatRoom/js/chunk-2d16c9f6.<hash>.js   # 登录流 + 分页逻辑 + TIM SDK
版本指纹：version.json?time=YYYYMMDDHH；上滚加载在 loadHistoryMessages()（cursorId=列表最旧一条 id）
```

### 验证记录（全部离线 + 单次在线收尾）

- getAuthToken / 登录 sign：离线按盐算法复算，与 HAR 抓包值**逐字符一致**
- 账密 DES：抓包密文解密→重加密往返**逐字节一致**
- 响应结构：用 HAR 内 centraltoken 逐字回放（浏览器几分钟前发过的原样请求）读 DTO
- 迁移后客户端单次在线验证：getByTeacherId / getChatRecordList（含 cursorId 翻页）/ getUserSig / getInfo / pinnedListById 全部通过

### 附3.1：研选文章（msgType=2 卡片的正文，2026-09-24 实测）

- 官方链路（网页版）：点卡片 → `POST account.zx093.cn/ssoserver/share/at.htm`（表单 `{token, rc}`，rc=页面随机 uuid）→ 返回 `{data: oc, st}` → 拼 `#/articleDetail?articleId=…&teacherId=…&oc=…&rc=…&st=…` 新窗口打开。oc/rc/st 只服务于分享/免登录场景。
- **正文本体与 oc/rc/st 无关**：`POST /touguServer/client/article/queryArticleDetail.htm`，体 `{articleId, teacherId}`（= 消息卡片 msgContent.sourceId + 房间 teacherId）+ centraltoken 头。
- 返回：`{articleTitle, articleContent, articleType, feeStatus, serviceStatus, createTime, userName, avatarPath, …}`。`articleContent` 是 Word 粘贴的富文本 HTML（图片为 fileoss 绝对地址，7 万字符级）；`articleType=2` 时为 JSON `{filePath}`，需再 `GET /client/article/preview.htm?path=…` 换 PDF 可读 URL（未见样本，按前端源实现）。
- 工具实现：`TouguClient.get_article()` / `GET /api/article/<id>` → 前端文章卡片可点，弹窗内白底 iframe 渲染正文（srcdoc + 限宽/图片自适应样式），PDF 型给外链。

### 附3.2：视频课（栏目/回放 + 免签直链播放，2026-09-24 实测）

官方链路：网页版「视频」tab → `POST /client/live/column/queryListByTeacherId.htm {teacherId}`（栏目）
→ `POST /client/live/playback/queryList.htm {teacherId, columnId, pageNum, pageSize:10}`（回放列表）
→ 点条目经 at.htm 换 oc 跳 `client.zx093.com/webktc` 播放页（另一套 Vue SPA「天龙博弈」，用腾讯云 VOD/TIM 播放器）。

**关键发现：不需要走 webktc。** `GET /touguServer/app/live/playback/queryById.htm?id=<videoId>` 直接返回：
- `adaptiveUrl`：**免签自适应 m3u8**（tg.tlby0093.cn，三档 720p/480p/240p，实测 `Access-Control-Allow-Origin: *`，任何页面可直接拉流）
- `playbackLink`：腾讯云 VOD FileId；`playerSign`：psign（走 webktc getplayinfo v4 时才需要）
- `title/pubTime/feeStatus`

工具实现：`GET /api/videos`（栏目+回放）+ `GET /api/video_play/<id>`（m3u8 直链）→ 弹窗内
`<video controls>` + 本地 hls.js（static/hls.min.js，Safari 走原生 HLS）直接播放，
原生控件自带播放/进度/音量/**全屏**；保留「官方页」外链兜底。

### 附3.3：微信扫码登录（2026-09-24 破解 + 实测成功）

```
① POST /ssoserver/login/xcx/getAuthToken.htm    {deviceId:"12332", timestamp, sign} → authToken
② POST /ssoserver/login/xcx/qrcode.htm          multipart {authToken, invitationCode(邀请码,328→H8X9),
     invitationChannel:"h5_live", os:"0", loginVersion:"11", loginSource:7, loginFunction:1,
     timestamp, sign} → 二维码 jpg（~110KB）
③ 每 2.5s POST /ssoserver/login/xcx/authTokenExchangeToken.htm?c=<md5(authToken)>&ts=live3
     form {authToken, timestamp, c, ts, sign} → 未扫码=status 101005「小程序没有完成登陆操作」；
     扫码确认后 data = centraltoken（200001=二维码过期）
```
- `c = md5(authToken)`（网页版 timerFunc，sign/qrcode/轮询三处全部离线逐字符对拍）
- invitationCode 从 `/app/teacher/getInviteCodeByTeacherId.htm {teacherId}` 拿
- 工具实现：`POST /api/login/qrcode`（生成，返回 base64 图 + auth_token）+
  `POST /api/login/qrcode/poll`（轮询；确认后自动走 apply_centraltoken 全套 probe）。
  /login 页扫码为主入口，**用户实扫验证通过**（djj45 登录成功、token/IM 全自动写入）。

### 附3.4：账密滑块在本页复活（当年「prefix 不存在」结论作废）

> **2026-09-24 追记：prefix 其实存在，值是 `v98goc`。** 当天官方登录抓包（Edge HAR）里
> 初始化/验证请求走 `v98goc.captcha-open.aliyuncs.com` / `v98goc-verify.captcha-open…`。
> 之前「无 prefix」是从 webChatRoom 的 bundle 反推的，漏了账密登录页（account.zx093.cn）
> 的初始化。不传 prefix 时 SDK 拼 `undefined.captcha-open…`，Edge 上直接 Network Error、
> 滑块不渲染——/login 已补 `prefix:"v98goc"`。`IllegalUserTag` telemetry 警告
> （userTag 由域名推导，127.0.0.1 无解）仍会出现，但不影响出票。

网页版 webChatRoom 的初始化就是 `window.initAliyunCaptcha({SceneId:"17n9bhbp", mode:"embed",
element, slideStyle:{width:313,height:44}, language:"cn", success, fail, getInstance})`——
无 prefix、无 AliyunCaptchaConfig。/login 页照抄后：在 127.0.0.1 下 SDK 的 telemetry 请求仍报
`undefined.captcha-open… IllegalUserTag`（userTag 由域名推导，localhost 无解），
但滑块数据上传（upload.captcha-open）成功、**success 回调正常出票**（76 字符
`Base64({certifyId,sceneId,isSign})`）。兜底：官方页过滑块粘票据同样保留。
