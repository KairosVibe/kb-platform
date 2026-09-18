# Sync VM clock from host (run on the Windows dev machine).
# Purpose: fix clock drift after VM suspend/resume (observed up to 16h drift).
# The VM has no working chronyd and NTP egress is unreliable, so we push the
# host's UTC time over SSH and write it to the hardware clock.
# Prerequisite: passwordless SSH to root@192.168.62.128.
# Usage: powershell -File deploy\vm\sync-vm-clock.ps1
# Note: keep this file ASCII-only (Windows PowerShell 5.1 reads BOM-less
#       scripts as ANSI; see WORKLOG #39 for the GBK lesson).

$ErrorActionPreference = 'Stop'
$VM = '192.168.62.128'

$hostUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')
Write-Output "Host UTC: $hostUtc"

ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$VM" "date -u -s '$hostUtc' && hwclock --systohc --utc && echo '--- after sync ---' && date -u && hwclock -u --show"

# Verify: drift should be within 60 seconds. The `date -u` output is UTC -
# mark Kind=Utc before comparing, or parsing treats it as local time
# (adds a phantom 8-hour offset on UTC+8 hosts).
$vmUtcStr = ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$VM" "date -u '+%Y-%m-%d %H:%M:%S'"
$vmUtc = [datetime]::SpecifyKind([datetime]::Parse($vmUtcStr), [datetimekind]::Utc)
$diff = ((Get-Date).ToUniversalTime() - $vmUtc).TotalSeconds
Write-Output ("Drift vs host: {0:N0} seconds" -f [math]::Abs($diff))
if ([math]::Abs($diff) -gt 60) { Write-Warning 'Drift exceeds 60 seconds - inspect manually'; exit 1 }
Write-Output 'VM clock synced OK'
