#!/bin/bash
# Cloudflare Tunnel 配置脚本
# 将本地 8099 端口通过 tunnel 映射到 goodfoodiron.cn

# 1. 修改 tunnel 配置，把服务端口从 3000 改为 8099
sudo sed -i '' 's|http://127.0.0.1:3000|http://127.0.0.1:8099|' /etc/cloudflared/config.yml

# 2. 查看修改后的配置
echo "=== 当前 tunnel 配置 ==="
cat /etc/cloudflared/config.yml

# 3. 重启 cloudflared 系统服务（已注册为 LaunchDaemon）
sudo killall cloudflared 2>/dev/null
sleep 1
sudo launchctl start com.cloudflare.cloudflared

echo "=== cloudflared 已重启 ==="
echo "访问 https://goodfoodiron.cn 即可打开 MyAgent 服务"
