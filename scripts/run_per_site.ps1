param(
    [string]$CsvFile = "resources/adLikelyUrls.csv",
    [string]$OutputDir = "results",
    [string]$TrackerCsv = "results/crawled_sites.csv",
    [int]$MaxRuns = 0
)

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

if (-not (Test-Path $CsvFile)) {
    Write-Error "CSV file not found: $CsvFile"
    exit 1
}

# Build a set of domains we've already processed (tracker CSV)
$seen = @{}
if (Test-Path $TrackerCsv) {
    Import-Csv -Path $TrackerCsv | ForEach-Object {
        $d = "$($_.inputDomain)".Trim().ToLower()
        if (-not [string]::IsNullOrWhiteSpace($d)) {
            $seen[$d] = $true
        }
    }
}

$runCount = 0
$csv = Import-Csv -Path $CsvFile

foreach ($row in $csv) {
    if ($MaxRuns -gt 0 -and $runCount -ge $MaxRuns) { break }

    $domain = $row.inputDomain
    if ([string]::IsNullOrWhiteSpace($domain)) { continue }

    $adsTxt = "$($row.adsTxt)".Trim()
    $adLikely = "$($row.adLikely)".Trim().ToLower()
    if ($adsTxt -eq "0" -and $adLikely -eq "false") { continue }

    $domain = $domain.Trim()

    $lcDomain = $domain.ToLower()
    if ($seen.ContainsKey($lcDomain)) { continue }

    $fullUrl = $domain
    if (-not ($fullUrl.StartsWith("http://") -or $fullUrl.StartsWith("https://"))) {
        $fullUrl = "https://$fullUrl"
    }

    $cmd = "python cli.py --url $fullUrl --timeout 60 --output-dir $OutputDir -d ads,requests,cookies,screenshot,fingerprints,cmp --cmp-action in --anti-bot"
    Write-Host ">> $cmd"

    try {
        Invoke-Expression $cmd
        $status = "success"
    } catch {
        Write-Warning "Failed for $domain: $_"
        $status = "fail"
    }

    # Record this run to tracker CSV (so it can be skipped next time)
    $record = [PSCustomObject]@{
        inputDomain = $domain
        status      = $status
        finishedAt  = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    }
    $append = Test-Path $TrackerCsv
    $record | Export-Csv -Path $TrackerCsv -NoTypeInformation -Append:$append

    $seen[$lcDomain] = $true
    $runCount++
}

Write-Host "Ran $runCount sites (limit: $MaxRuns)."