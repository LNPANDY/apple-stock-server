#!/bin/bash
# 完整卸载本程序部署的所有内容。
#
#   bash uninstall.sh          # 基础卸载（服务 + 程序文件）
#   bash uninstall.sh --full   # 深度卸载（连 apt 库和 Playwright 缓存一起删）
#
# 只删本程序自己的东西，绝不触碰宝塔/nginx/lp-monitor/wormhole-bridge/aethir。
set -u

FULL="${1:-}"

echo "==> 停止并移除 systemd 服务"
for svc in apple-stock mihomo; do
  systemctl disable --now "$svc" 2>/dev/null
  rm -f "/etc/systemd/system/${svc}.service"
done
systemctl daemon-reload 2>/dev/null
echo "   完成"

echo "==> 删除程序文件"
rm -rf /opt/apple-stock
rm -rf /opt/mihomo
echo "   /opt/apple-stock、/opt/mihomo 已删除"

echo "==> 移除防火墙规则（部署时加的 8788）"
ufw delete allow 8788/tcp 2>/dev/null && echo "   已移除" || echo "   无此规则"

echo "==> 删除临时文件"
rm -f /tmp/app.tar.gz /tmp/clash.yaml /tmp/deploy_*.log /tmp/step_*.log 2>/dev/null
echo "   完成"

# Playwright 的 Chromium 缓存约 400MB。
# --full 时才删：如果机器上还有别的程序用 Playwright，删了会影响它们。
if [[ "$FULL" == "--full" ]]; then
  echo "==> 删除 Playwright Chromium 缓存（~400MB）"
  rm -rf /root/.cache/ms-playwright
  echo "   完成"

  echo "==> 移除 apt 库"
  echo "   这些库可能被其他程序共用。逐个确认没有其他依赖后删除："
  V=$(. /etc/os-release && echo $VERSION_ID)
  S=""; [[ "$V" == 24.* ]] && S="t64"
  PKGS="python3-venv python3-pip python3-yaml libnss3 libnspr4 \
        libatk1.0-0$S libatk-bridge2.0-0$S libcups2$S libasound2$S libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 fonts-liberation"
  for pkg in $PKGS; do
    rdepends=$(apt-cache rdepends --installed "$pkg" 2>/dev/null \
      | grep -v "Reverse Depends:" | grep -cv "^$")
    if [[ "$rdepends" -le 1 ]]; then
      apt-get remove -y -qq "$pkg" 2>/dev/null && echo "   已删 $pkg"
    else
      echo "   跳过 $pkg（有 $rdepends 个程序依赖它）"
    fi
  done
fi

echo
echo "卸载完成。"
echo "未删除的内容（确认不需要再手动清理）："
echo "  /root/.ssh/authorized_keys 里的部署公钥（见下）"
echo
echo "如需移除部署公钥："
echo "  sed -i '/zcode-test/d' /root/.ssh/authorized_keys"
