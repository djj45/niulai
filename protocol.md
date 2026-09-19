# 约牛聊天室小程序协议分析

> 抓包环境：macOS + 微信 4.x 桌面版小程序（WeChatAppEx/Chromium 内核）
> 抓包方式：mitmproxy 12 上游串接 sing-box，系统代理切至 `127.0.0.1:8888`，CA 证书入系统钥匙串
> 分析时间：2026-09-17 21:24 – 21:47（样本房间：chatRoomId=298「知行合一交易逻辑专栏」，teacherId=328）
> 原始数据：`~/.zcode/workspace/default/data/capture/`（flows_20260917_212459.mitm 完整流量 / chat_apis.jsonl HTTP摘要 / ws_decoded*.txt WS解码 / history_messages.txt 历史消息 / images/ 图片原件）

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
- [ ] centraltoken 有效期未测
- [ ] `getUserSig` 拿到的 userSig 理论上可独立连腾讯 IM 收消息（sdkAppId+userId+userSig），未实测
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
{"certifyId":"T7CxI9I4gu","sceneId":"f374igpl","isSign":true,"securityToken":"<128 字符>"}
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

> ⚠️ **验证码 Web SDK 的票据形状不同**（实测其 `captchaVerifyCallback` 收到的是
> `JSON{sceneId, certifyId, deviceToken, failover}`，没有 `isSign`/`securityToken`）。
> 所以想用浏览器 Web SDK 代替小程序插件过滑块，**票据对不上、会被服务端核验拒**。

### 其它已核实细节

- 请求层：`if (!ignoreToken) { f[tokenName] = wx.getStorageSync("token") }`，而登录成功
do `wx.setStorageSync("token", resp.data)` → 两者是同一个键，故 **resp.data 即 centraltoken**。
- `getSign` 只 `delete channel`，**不过滤空值**（`loginVersion=""`/`deviceId=""` 要参与签名）；
返回**小写** MD5，由调用方 `upperCase()`。
- 约牛后端收到 ticket 后会**服务端到服务端**再调阿里云核验（抓包看不到）。
  `certifyId` 一次性、短时效 → **抓包/重放无法免除滑块**，每次账密登录都要重新过。
- 抓包器：`tools/capture_login.py`（抓到即解账密、重算 sign 比对、并把响应 token 喂给应用）。

另：源码 config 模块泄露了全套测试环境域名（`test-account.zx093.cn`、`touguapi-test.zx093.com` 等）。

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
