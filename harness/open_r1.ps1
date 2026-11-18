param(
    [ValidateSet('owner', 'guest')]
    [string]$Role = 'owner',
    [switch]$Probe
)

$ErrorActionPreference = 'Stop'
$server = 'ubuntu@150.158.75.223'
$base = '/opt/giftia-harness-test/stage5-20261117'
$url = 'http://127.0.0.1:18141'
$ssh = (Get-Command ssh.exe).Source
$arguments = @('-N', '-L', '127.0.0.1:18141:127.0.0.1:18141', '-o', 'ExitOnForwardFailure=yes',
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=12', $server)
$forward = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru
try {
    $health = $null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($forward.HasExited) { throw 'SSH forwarding exited. Check the SSH connection and local port 18141.' }
        try {
            $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2
            break
        } catch {
            Start-Sleep -Milliseconds 400
        }
    }
    if (-not $health -or $health.profile -ne 'isla-test' -or $health.synthetic) {
        throw 'The live R1 candidate is unavailable.'
    }
    $secretCommand = "sudo -n -u giftia-harness-test cat $base/r1/identity-tokens.json"
    $secrets = & $ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=12 $server $secretCommand
    if ($LASTEXITCODE -ne 0 -or $forward.HasExited) { throw 'Could not read the R1 login credential.' }
    $token = ($secrets | ConvertFrom-Json)."synthetic-$Role"
    if (-not $token) { throw 'The R1 login credential is missing.' }
    if ($Probe) {
        $login = @{
            Uri = "$url/api/web/login"
            Method = 'Post'
            ContentType = 'application/json'
            Body = (@{ token = $token } | ConvertTo-Json -Compress)
            SessionVariable = 'r1Session'
        }
        Invoke-RestMethod @login | Out-Null
        $state = Invoke-RestMethod "$url/api/web/state" -WebSession $r1Session
        [pscustomobject]@{ role = $state.role; items = $state.items.Count; profile = $health.profile }
        return
    }
    Set-Clipboard -Value $token
    Start-Process $url
    Write-Host 'The trial page is open. Paste the copied credential to sign in. Keep this terminal open.'
    Wait-Process -Id $forward.Id
} finally {
    if (-not $forward.HasExited) { Stop-Process -Id $forward.Id }
}
