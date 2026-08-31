<#
    Полный прогон одной командой: обновление кода, сертификат, расписание, сбор
    данных, метрики очереди, прогон торговой политики. Ничего спрашивать не будет.

        powershell -ExecutionPolicy Bypass -File .\run.ps1 -Minutes 15

    Результат складывается в report.txt — этот файл и надо прислать.
#>
param(
    [double]$Minutes = 15,
    [int]$Sleeve = 6000,
    [string]$Data = "data\tmon.jsonl"
)

$base = "https://raw.githubusercontent.com/Deennz0123/erra-gedera-creatives/" +
        "claude/tmon-tick-trading-automation-ur95a1/tmon-tick-scalper"
$report = "report.txt"

function Say($text) { $text | Tee-Object -FilePath $report -Append }

if (-not (Test-Path "research")) {
    Write-Host "Запускать надо из папки tmon-tick-scalper — здесь её файлов нет." -ForegroundColor Red
    exit 1
}
Remove-Item $report -ErrorAction SilentlyContinue
Say "=== TMON, прогон $(Get-Date -Format 'yyyy-MM-dd HH:mm') ==="

# --- код -------------------------------------------------------------------------
Say "`n--- обновление кода ---"
foreach ($f in @("research/tinvest_collect.py", "research/queue_probe.py",
                 "research/simulate.py", "tmon_bot/__init__.py", "tmon_bot/market.py",
                 "tmon_bot/policy.py", "tmon_bot/simulator.py")) {
    $dst = $f -replace "/", "\"
    $dir = Split-Path $dst
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    try { Invoke-WebRequest "$base/$f" -OutFile $dst -UseBasicParsing; Say "  ok      $f" }
    catch { Say "  ОШИБКА  $f — $_" }
}

# --- сертификат Минцифры ---------------------------------------------------------
# Корня этого центра нет в Windows. Доверяем ему только в пределах проекта.
if (-not (Test-Path "ca.pem")) {
    Say "`n--- сертификат ---"
    try {
        $tcp = New-Object Net.Sockets.TcpClient("invest-public-api.tinkoff.ru", 443)
        $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, { $true })
        $ssl.AuthenticateAsClient("invest-public-api.tinkoff.ru")
        $leaf = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
        $tcp.Close()
        $chain = New-Object Security.Cryptography.X509Certificates.X509Chain
        $chain.ChainPolicy.RevocationMode = "NoCheck"
        $null = $chain.Build($leaf)
        $pem = @()
        foreach ($e in $chain.ChainElements) {
            if ($e.Certificate.Thumbprint -ne $leaf.Thumbprint) {
                $pem += "-----BEGIN CERTIFICATE-----"
                $pem += [Convert]::ToBase64String($e.Certificate.RawData, 'InsertLineBreaks')
                $pem += "-----END CERTIFICATE-----"
            }
        }
        Set-Content ca.pem ($pem -join "`n") -Encoding ascii
        Say "  ca.pem создан, сертификатов: $($pem.Count / 3)"
    } catch { Say "  не удалось получить сертификат — $_" }
}

# --- инструмент и расписание -----------------------------------------------------
Say "`n--- инструмент и расписание торгов ---"
py research\tinvest_collect.py schedule 2>&1 | Tee-Object -FilePath $report -Append

# --- сбор ------------------------------------------------------------------------
Say "`n--- сбор данных, $Minutes мин ---"
$dir = Split-Path $Data
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
py research\tinvest_collect.py collect $Data --minutes $Minutes 2>&1 | Tee-Object -FilePath "collect.log"
Say "(последние строки сбора)"
Get-Content "collect.log" -Tail 15 | Tee-Object -FilePath $report -Append

# --- анализ ----------------------------------------------------------------------
Say "`n--- метрики очереди ---"
py research\queue_probe.py analyze $Data --size $Sleeve 2>&1 | Tee-Object -FilePath $report -Append

Say "`n--- прогон торговой политики ---"
py research\simulate.py $Data --sleeve $Sleeve 2>&1 | Tee-Object -FilePath $report -Append

Say "`n=== готово. Прислать файл report.txt ==="
