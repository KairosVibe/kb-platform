﻿# VM 时钟同步脚本（宿主机运行）
# 用途：VM 挂起/恢复后时钟漂移的修正——把宿主机 UTC 时间推送给 VM 并写硬件时钟。
# 背景：VM（192.168.62.128）未运行 chronyd 且出网 NTP 不可靠，挂起后系统时钟停在挂起时刻
#       （实测漂移 16h），影响看板聚合窗口、任务租约判定与限流——启动 VM 后跑一次本脚本。
# 前置：SSH 免密到 root@192.168.62.128（本机已配置）。
# 用法：powershell -File deploy\vm\sync-vm-clock.ps1

$ErrorActionPreference = 'Stop'
$VM = '192.168.62.128'

$hostUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')
Write-Output "宿主 UTC: $hostUtc"

ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$VM" "date -u -s '$hostUtc' && hwclock --systohc --utc && echo '--- 同步后 ---' && date -u && hwclock -u --show"

# 验证：差值应在 60 秒内（★ ssh date -u 返回 UTC 字符串，必须标注 Kind=Utc
#       再与宿主 UTC 比较——直接 Parse 会被当作本地时间，凭空差 8 小时）
$vmUtcStr = ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$VM" "date -u '+%Y-%m-%d %H:%M:%S'"
$vmUtc = [datetime]::SpecifyKind([datetime]::Parse($vmUtcStr), [datetimekind]::Utc)
$diff = ((Get-Date).ToUniversalTime() - $vmUtc).TotalSeconds
Write-Output ("VM 与宿主差值: {0:N0} 秒" -f [math]::Abs($diff))
if ([math]::Abs($diff) -gt 60) { Write-Warning '差值超过 60 秒，请人工检查'; exit 1 }
Write-Output 'VM 时钟已同步 ✓'
