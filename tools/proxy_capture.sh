#!/usr/bin/env bash
# 一键切换抓包代理（macOS）
# ============================
# 原来抓一次 token 要敲 3 条 networksetup + 记住 SOCKS 的坑 + 忘了恢复就没网。
# 现在：
#
#   tools/proxy_capture.sh on       # 挂上 mitmdump 并切换系统代理
#   tools/proxy_capture.sh off      # 一键恢复（精确还原你原来的设置）
#   tools/proxy_capture.sh status   # 看当前状态
#
# `on` 会把**原始代理设置存到 /tmp/niulai_proxy_state**，`off` 按它精确还原，
# 不需要你记原来的端口，也不会把 SOCKS 开成关/关成开。
#
# 环境变量：
#   NIULAI_WIFI            网络服务名，默认 "Wi-Fi"（`networksetup -listallnetworkservices`）
#   NIULAI_UPSTREAM_PORT   你平时的代理端口，默认 20122（sing-box）
#   NIULAI_CAPTURE_PORT    mitmproxy 监听端口，默认 8888
#   NIULAI_ADDON           要加载的 addon，默认 tools/capture_token.py
set -uo pipefail

IFACE="${NIULAI_WIFI:-Wi-Fi}"
UPSTREAM_PORT="${NIULAI_UPSTREAM_PORT:-20122}"
CAPTURE_PORT="${NIULAI_CAPTURE_PORT:-8888}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADDON="${NIULAI_ADDON:-$HERE/tools/capture_token.py}"
STATE="/tmp/niulai_proxy_state"
LOG="/tmp/niulai_capture.log"

die() { printf '  ❌ %s\n' "$*" >&2; exit 1; }
info() { printf '  %s\n' "$*"; }

[ "$(uname)" = "Darwin" ] || die "这个脚本只适用于 macOS"

net_service_exists() {
  networksetup -listallnetworkservices 2>/dev/null | tail -n +2 | grep -Fxq "$IFACE"
}

# 读某个代理的当前设置 → "Enabled|Server|Port"
proxy_state() {
  local kind="$1" out
  out="$(networksetup "-get${kind}proxy" "$IFACE" 2>/dev/null || true)"
  printf '%s|%s|%s\n' \
    "$(printf '%s\n' "$out" | awk -F': ' '/^Enabled/{print $2}')" \
    "$(printf '%s\n' "$out" | awk -F': ' '/^Server/{print $2}')" \
    "$(printf '%s\n' "$out" | awk -F': ' '/^Port/{print $2}')"
}

# 应用代理设置：proxy_apply <web|secureweb|socksfirewall> <Enabled> <Server> <Port>
proxy_apply() {
  local kind="$1" en="$2" srv="$3" port="$4" flag
  [ -n "$srv" ] && [ -n "$port" ] && networksetup "-set${kind}proxy" "$IFACE" "$srv" "$port" >/dev/null
  [ "$en" = "Yes" ] && flag=on || flag=off
  # SOCKS 的「端口」和「开关」是两个参数：-setsocksfirewallproxystate 只吃 on/off
  # （写成 -setsocksfirewallproxystate "Wi-Fi" 20122 会报 The parameters were not valid.）
  networksetup "-set${kind}proxystate" "$IFACE" "$flag" >/dev/null
}

mitm_running() { lsof -nP -iTCP:"$CAPTURE_PORT" -sTCP:LISTEN >/dev/null 2>&1; }
mitm_pids() { pgrep -f "mitmdump .*-p $CAPTURE_PORT" 2>/dev/null || true; }

need_ca() {
  security find-certificate -c "mitmproxy" -a /Library/Keychains/System.keychain >/dev/null 2>&1 \
    || security find-certificate -c "mitmproxy" -a ~/Library/Keychains/login.keychain-db >/dev/null 2>&1
}

cmd_status() {
  printf '\n  网络服务 : %s\n' "$IFACE"
  printf '  Web 代理 : %s\n' "$(proxy_state web | tr '|' ' ')"
  printf '  安全Web  : %s\n' "$(proxy_state secureweb | tr '|' ' ')"
  printf '  SOCKS    : %s\n' "$(proxy_state socksfirewall | tr '|' ' ')"
  if mitm_running; then
    printf '  mitmdump : ✅ 监听 %s（pid %s）\n' "$CAPTURE_PORT" "$(mitm_pids | tr '\n' ' ')"
    printf '             addon: %s\n' "$(ps -p "$(mitm_pids | head -1)" -o command= 2>/dev/null | sed 's/.*-s //')"
  else
    printf '  mitmdump : ⚪ 没在跑\n'
  fi
  printf '  CA 证书  : %s\n' "$(need_ca && echo '✅ 已信任' || echo '⚠️  没找到（首次需要信任 ~/.mitmproxy/mitmproxy-ca-cert.pem）')"
  if [ -f "$STATE" ]; then
    printf '  已存原始 : %s\n' "$(tr '\n' ' ' < "$STATE")"
  fi
  printf '\n'
}

cmd_on() {
  net_service_exists || die "找不到网络服务「${IFACE}」，用 networksetup -listallnetworkservices 看一下"

  # 只在第一次记录原始状态，免得重复 on 把 8888 自己存成"原始值"
  if [ -f "$STATE" ]; then
    info "已存在原始状态记录（$(tr '\n' ' ' < "$STATE")），不覆盖"
  else
    printf '%s\n%s\n%s\n' "$(proxy_state web)" "$(proxy_state secureweb)" \
      "$(proxy_state socksfirewall)" > "$STATE"
    info "已记录你的原始代理设置 → $STATE"
  fi

  if mitm_running; then
    info "mitmdump 已在监听 ${CAPTURE_PORT}，复用"
  else
    [ -f "$ADDON" ] || die "addon 不存在：$ADDON"
    info "启动 mitmdump（addon=$(basename "$ADDON")，上游 127.0.0.1:${UPSTREAM_PORT}）"
    # --mode upstream: 把流量转给你平时的代理；日志顺手 tee 到 ${LOG}，方便事后查
    ( cd "$HERE" && nohup mitmdump -p "$CAPTURE_PORT" \
        --mode "upstream:http://127.0.0.1:$UPSTREAM_PORT" \
        -s "$ADDON" > "$LOG" 2>&1 & )
    for _ in $(seq 1 20); do
      mitm_running && break
      sleep 0.5
    done
    mitm_running || die "mitmdump 没起来，看 $LOG"
    info "mitmdump 就绪，日志：$LOG"
  fi

  need_ca || info "⚠️  没找到 mitmproxy 证书：把 ~/.mitmproxy/mitmproxy-ca-cert.pem 拖进「钥匙串访问」并设为始终信任"

  info "切换系统代理 → 127.0.0.1:$CAPTURE_PORT"
  networksetup -setwebproxy       "$IFACE" 127.0.0.1 "$CAPTURE_PORT" >/dev/null
  networksetup -setsecurewebproxy "$IFACE" 127.0.0.1 "$CAPTURE_PORT" >/dev/null
  # 关键：SOCKS 关掉。否则部分连接会走 SOCKS 直达上游代理，绕过 mitmdump，
  # 表现就是"有些请求抓得到、有些抓不到"，最难查。
  networksetup -setsocksfirewallproxystate "$IFACE" off >/dev/null

  cat <<EOF

  ✅ 抓包已就绪。现在：
     1) 把微信里的小程序窗口【彻底关掉再重开】← 漏了这步就什么都抓不到
     2) 打开约牛，进任意老师聊天室（或走账号密码登录）

  完事跑：  tools/proxy_capture.sh off

EOF
}

cmd_off() {
  net_service_exists || die "找不到网络服务「${IFACE}」"
  if [ -f "$STATE" ]; then
    local w s k
    w="$(sed -n 1p "$STATE")"; s="$(sed -n 2p "$STATE")"; k="$(sed -n 3p "$STATE")"
    IFS='|' read -r e1 h1 p1 <<<"$w"; proxy_apply web         "$e1" "$h1" "$p1"
    IFS='|' read -r e2 h2 p2 <<<"$s"; proxy_apply secureweb   "$e2" "$h2" "$p2"
    IFS='|' read -r e3 h3 p3 <<<"$k"; proxy_apply socksfirewall "$e3" "$h3" "$p3"
    info "已按原始记录恢复代理"
    rm -f "$STATE"
  else
    info "没有原始记录，按默认上游端口 $UPSTREAM_PORT 恢复"
    networksetup -setwebproxy       "$IFACE" 127.0.0.1 "$UPSTREAM_PORT" >/dev/null
    networksetup -setsecurewebproxy "$IFACE" 127.0.0.1 "$UPSTREAM_PORT" >/dev/null
    networksetup -setsocksfirewallproxy "$IFACE" 127.0.0.1 "$UPSTREAM_PORT" >/dev/null
    networksetup -setsocksfirewallproxystate "$IFACE" on >/dev/null
  fi
  cmd_status
}

case "${1:-status}" in
  on|up)      cmd_on ;;
  off|down)   cmd_off ;;
  status|st)  cmd_status ;;
  *) cat <<EOF
用法：tools/proxy_capture.sh {on|off|status}

  on      启动 mitmdump（如未在跑）+ 把系统代理切到 8888 + 关掉 SOCKS
  off     精确还原原始代理设置
  status  看当前代理 / mitmdump / 证书状态

环境变量：NIULAI_WIFI / NIULAI_UPSTREAM_PORT / NIULAI_CAPTURE_PORT / NIULAI_ADDON
EOF
     exit 1 ;;
esac
