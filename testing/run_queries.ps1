# run_queries.ps1
# Reads queries from prompts.xlsx (sheet: Queries), fires each at the backend,
# and APPENDS results (including model name) to the "Results" sheet on prompts.xlsx.
# Every run adds new rows on top of previous runs - nothing is overwritten.
#
# Accuracy is measured as RETRIEVAL ACCURACY: each question was generated from
# a specific real UCD policy document (the "topic" column). We check whether
# the backend's citations (the documents it actually retrieved and cited)
# include that same document. This directly measures retrieval correctness,
# not just whether a word happens to appear in the generated answer text.

$BackendUrl   = "http://localhost:8000/chat/query"
$HealthLlmUrl = "http://localhost:8000/health/llm"
$ExcelFile    = "prompts.xlsx"

if (-not (Get-Module -ListAvailable -Name ImportExcel)) {
    Write-Host "ImportExcel module not found. Installing (one-time setup)..."
    Install-Module -Name ImportExcel -Scope CurrentUser -Force -AllowClobber
}
Import-Module ImportExcel

if (-not (Test-Path $ExcelFile)) {
    Write-Host "ERROR: $ExcelFile not found. Keep it in the same folder as this script."
    Pause
    exit
}

# --- Get current model name from the backend ---
$modelName = "unknown"
try {
    $health = Invoke-RestMethod -Uri $HealthLlmUrl -Method Get
    if ($health.message -match "^(.*?) is available") {
        $modelName = $Matches[1]
    }
} catch {
    Write-Host "WARNING: Could not reach $HealthLlmUrl to detect model name. Logging as 'unknown'."
}

Write-Host "Detected model: $modelName"
Write-Host ""

$rows = Import-Excel -Path $ExcelFile -WorksheetName "Queries"
$available = $rows.Count

Write-Host "There are $available prompts available."
$requested = Read-Host "How many prompts do you want to run? (press Enter for all $available)"

if ([string]::IsNullOrWhiteSpace($requested)) {
    $requested = $available
} else {
    $requested = [int]$requested
}

$runList = New-Object System.Collections.Generic.List[object]
for ($n = 0; $n -lt $requested; $n++) {
    $runList.Add($rows[$n % $available])
}
$rows = $runList
$total = $rows.Count

Write-Host "Running $total prompt(s)$(if ($requested -gt $available) { ' (cycling through the ' + $available + ' available prompts)' })."
Write-Host ""

$refusalPhrases = @("i do not know", "i don't know", "cannot be confidently derived", "does not contain", "no information", "unable to answer")

# Words too generic to use for matching a document (would false-match almost anything)
$genericWords = @("policy", "procedure", "procedures", "guidelines", "guideline", "framework",
                   "regulations", "statement", "code", "practice", "student", "students",
                   "staff", "employee", "employees", "faculty", "university", "college", "ucd")

function Get-CoreWords($text) {
    $words = [regex]::Matches($text.ToLower(), "[a-z]+") | ForEach-Object { $_.Value }
    return $words | Where-Object { $_.Length -gt 3 -and $genericWords -notcontains $_ }
}

$results = @()
$latencies = @()
$retrievalHits = 0
$answerRelevantCount = 0
$refusedCount = 0
$errorCount = 0

$runTimestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

$i = 0
foreach ($row in $rows) {
    $i++
    $question = $row.question
    $topic    = $row.topic      # the real document this question was generated from
    $keyword  = $row.keyword

    Write-Host "[$i/$total] Query: $question"

    $body = @{ question = $question } | ConvertTo-Json

    try {
        $r = Invoke-RestMethod -Uri $BackendUrl -Method Post -ContentType "application/json" -Body $body
        $latSec = [math]::Round($r.latency_ms / 1000, 2)
        $latencies += $latSec

        # --- Retrieval accuracy: does any citation match the expected source doc? ---
        $topicCoreWords = Get-CoreWords($topic)
        $citationTitles = @()
        if ($r.citations) {
            $citationTitles = $r.citations | ForEach-Object { $_.title }
        }
        $citationTextLower = ($citationTitles -join " ").ToLower()

        $retrievedCorrectDoc = $false
        foreach ($w in $topicCoreWords) {
            if ($citationTextLower.Contains($w)) { $retrievedCorrectDoc = $true; break }
        }

        # --- Secondary signal: does the generated answer text mention the topic (weaker check) ---
        $answerLower = $r.answer.ToLower()
        $isRefusal = $false
        foreach ($phrase in $refusalPhrases) {
            if ($answerLower.Contains($phrase)) { $isRefusal = $true; break }
        }
        $answerMentionsKeyword = $answerLower.Contains($keyword.ToLower())

        if ($retrievedCorrectDoc) { $retrievalHits++ }
        if ($answerMentionsKeyword) { $answerRelevantCount++ }
        if ($isRefusal) { $refusedCount++ }

        if ($isRefusal) {
            $status = "REFUSED"
        } elseif ($retrievedCorrectDoc) {
            $status = "CORRECT RETRIEVAL"
        } else {
            $status = "WRONG/MISSING RETRIEVAL"
        }

        $citationsSummary = if ($citationTitles.Count -gt 0) { $citationTitles -join "; " } else { "(none)" }

        Write-Host "  Latency: $latSec s | $status | Cited: $citationsSummary"

        $results += [PSCustomObject]@{
            Timestamp          = $runTimestamp
            Model              = $modelName
            Question           = $question
            ExpectedSourceDoc  = $topic
            Keyword            = $keyword
            LatencySeconds     = $latSec
            CitedDocuments     = $citationsSummary
            RetrievalCorrect   = $retrievedCorrectDoc
            AnswerMentionsWord = $answerMentionsKeyword
            Status             = $status
        }

    } catch {
        $errorCount++
        Write-Host "  ERROR: $($_.Exception.Message)"

        $results += [PSCustomObject]@{
            Timestamp          = $runTimestamp
            Model              = $modelName
            Question           = $question
            ExpectedSourceDoc  = $topic
            Keyword            = $keyword
            LatencySeconds     = $null
            CitedDocuments     = ""
            RetrievalCorrect   = $false
            AnswerMentionsWord = $false
            Status             = "ERROR: $($_.Exception.Message)"
        }
    }
}

$results | Export-Excel -Path $ExcelFile -WorksheetName "Results" -Append

Write-Host ""
Write-Host "===================================================="

if ($latencies.Count -gt 0) {
    $avg = [math]::Round(($latencies | Measure-Object -Average).Average, 2)
    $min = [math]::Round(($latencies | Measure-Object -Minimum).Minimum, 2)
    $max = [math]::Round(($latencies | Measure-Object -Maximum).Maximum, 2)
    $retrievalPct = [math]::Round(($retrievalHits / $total) * 100, 1)
    $answerRelPct = [math]::Round(($answerRelevantCount / $total) * 100, 1)
    $refusalPct   = [math]::Round(($refusedCount / $total) * 100, 1)

    Write-Host "Model: $modelName"
    Write-Host "Completed: $($latencies.Count) / $total queries (errors: $errorCount)"
    Write-Host "Average latency: $avg s"
    Write-Host "Min latency: $min s"
    Write-Host "Max latency: $max s"
    Write-Host ""
    Write-Host "Retrieval accuracy: $retrievalPct% ($retrievalHits / $total)  <- correct source document was cited"
    Write-Host "Answer keyword rate: $answerRelPct% ($answerRelevantCount / $total)  <- secondary/weaker signal"
    Write-Host "Refusal rate: $refusalPct% ($refusedCount / $total)"
} else {
    Write-Host "No successful queries recorded."
}

Write-Host ""
Write-Host "Results appended to $ExcelFile (Results sheet)."
Write-Host ""
Pause
