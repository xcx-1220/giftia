# 服务器同步检查：逐文件比对本地仓库与服务器上的代码
#
# 用法（在项目根目录）：
#   .\tools\check_sync.ps1
#   .\tools\check_sync.ps1 -Server 1.2.3.4 -User ubuntu
#
# 只读操作：不改服务器上的任何东西，也不碰她的 data/。
# 注意：这个文件必须保存为「UTF-8 with BOM」——Windows PowerShell 5.1 会把
# 无 BOM 的 UTF-8 当成本地代码页读，中文注释会乱码并让整个脚本解析失败。
param(
    [string]$Server = "150.158.75.223",
    [string]$User = "ubuntu",
    [string]$AppDir = "/opt/giftia"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not (Test-Path ".git")) { throw "这里不是 git 仓库：$root" }

    $head = (git rev-parse --short HEAD).Trim()
    $subject = (git log -1 --format=%s).Trim()
    Write-Host "本地 HEAD   $head  ($subject)"

    # 只比对**会被部署的文件**。`.review/` 是代码审查的证据链（报告、探针、快照），
    # 本来就不上服务器——以前拿 git ls-files 的全部去服务器算哈希，
    # 审查一加文件（99 个）这个脚本就整个报错，等于同步检查失效了。
    $files = @(git ls-files | Where-Object { $_ -notmatch '^\.review/' })
    if ($files.Count -eq 0) { throw "git 里没有任何文件" }

    Write-Host "本地文件    $($files.Count) 个，逐个算 SHA256…"
    $local = @{}
    foreach ($f in $files) {
        if (Test-Path $f) { $local[$f] = (Get-FileHash -Algorithm SHA256 -Path $f).Hash.ToLower() }
        else { $local[$f] = "MISSING" }
    }

    # 远端一次性算出同一批文件的哈希（路径没有空格，所以能直接拼）
    # config/*.toml 是 600 权限（里面有她的称呼、群号这类东西），普通用户读不到，
    # 要 sudo。ubuntu 用户在 setup.sh 里已经配了免密 sudo。
    $raw = ssh -o BatchMode=yes -o ConnectTimeout=15 "$User@$Server" "cd $AppDir && sudo sha256sum $($files -join ' ')" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "连不上服务器或路径不对：" -ForegroundColor Red
        $raw | Select-Object -First 3 | ForEach-Object { Write-Host "  $_" }
        exit 2
    }
    $remote = @{}
    foreach ($line in $raw) {
        if ("$line" -match '^([0-9a-f]{64})\s+(.+)$') { $remote[$Matches[2].Trim()] = $Matches[1] }
    }

    Write-Host "远端文件    $($remote.Count) 个`n"
    $diff = 0
    foreach ($f in $files) {
        if (-not $remote.ContainsKey($f)) { Write-Host "  远端缺文件  $f" -ForegroundColor Yellow; $diff++; continue }
        if ($local[$f] -ne $remote[$f]) { Write-Host "  内容不同    $f" -ForegroundColor Yellow; $diff++ }
    }

    Write-Host ""
    if ($diff -eq 0) {
        Write-Host "同步 ✓  服务器 $AppDir 的代码与本地 $head 完全一致（$($files.Count) 个文件）" -ForegroundColor Green
    } else {
        Write-Host "$diff 处不一致 —— 重新部署一遍即可：" -ForegroundColor Yellow
        Write-Host "  ssh $User@$Server 'rm -rf /tmp/giftia'"
        Write-Host "  scp -r . ${User}@${Server}:/tmp/giftia"
        Write-Host "  ssh $User@$Server 'sudo bash /tmp/giftia/deploy/setup.sh'"
    }

    Write-Host "`n── 服务状态 ──"
    ssh -o BatchMode=yes "$User@$Server" "systemctl is-active giftia-qq; systemctl show giftia-qq -p ActiveEnterTimestamp --value" 2>&1 |
        ForEach-Object { Write-Host "  $_" }

    exit $(if ($diff -eq 0) { 0 } else { 1 })
}
finally {
    Pop-Location
}
