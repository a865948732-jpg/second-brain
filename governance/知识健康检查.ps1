param(
    [string]$Vault = $(if ($env:SECOND_BRAIN_VAULT) { $env:SECOND_BRAIN_VAULT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../.local/vault')) }),
    [string]$PythonExecutable = $(if ($env:SECOND_BRAIN_PYTHON) { $env:SECOND_BRAIN_PYTHON } else { 'python' }),
    [int]$StaleDays = 45,
    [ValidateSet('daily', 'weekly')][string]$Phase = 'daily',
    [string[]]$DependencyPaths = @(),
    [switch]$NoWrite,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$gateScript = Join-Path $PSScriptRoot 'self_growing_distillation.py'
$policyArgs = @('-B', (Join-Path $PSScriptRoot 'health_gate_policy.py'), '--vault', $Vault, '--phase', $Phase, '--stale-days', "$StaleDays")
foreach ($dependency in $DependencyPaths) { $policyArgs += @('--dependency', $dependency) }
$policyOutput = @(& $PythonExecutable @policyArgs 2>&1)
if ($LASTEXITCODE -ne 0) { throw '健康分层策略校验失败，健康检查停止' }
try { $healthPolicy = ($policyOutput -join "`n") | ConvertFrom-Json }
catch { throw '健康分层策略校验失败，健康检查停止' }
if ($healthPolicy.schema -ne 'second-brain-health-policy-v1' -or $healthPolicy.sha256 -notmatch '^[0-9a-f]{64}$') {
    throw '健康分层策略校验失败，健康检查停止'
}
if ([string]::IsNullOrWhiteSpace([string]$healthPolicy.vault) -or
    -not [IO.Path]::IsPathRooted([string]$healthPolicy.vault) -or
    -not [IO.Directory]::Exists([string]$healthPolicy.vault)) {
    throw '健康分层策略校验失败，健康检查停止'
}
$Vault = [IO.Path]::GetFullPath([string]$healthPolicy.vault)
$knowledgeDir = Join-Path $Vault '02_Knowledge'
$topicDir = Join-Path $Vault '03_Topics'
$outputDir = Join-Path $Vault '04_Outputs'
$reviewDir = Join-Path $Vault '05_Review/Weekly'
$reviewRegistry = [IO.Path]::GetFullPath((Join-Path $Vault '05_Review/自生长蒸馏/敏感源人工复核与放行清单.md'))

if (-not (Test-Path -LiteralPath $gateScript)) {
    throw '敏感源资格门禁脚本不存在，健康检查停止'
}
$gateOutput = @(& $PythonExecutable -B $gateScript --vault $Vault --check-all 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw '敏感源资格门禁执行失败，健康检查停止'
}
try {
    $gate = ($gateOutput -join "`n") | ConvertFrom-Json
}
catch {
    throw '敏感源资格门禁返回无效 JSON，健康检查停止'
}
if ($gate.mode -ne 'gate-check-all' -or $null -eq $gate.files) {
    throw '敏感源资格门禁结果不完整，健康检查停止'
}

function Get-Sha256Text([string]$Value) {
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        ([BitConverter]::ToString($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

$gateByPath = @{}
foreach ($item in @($gate.files)) {
    if ([string]::IsNullOrWhiteSpace([string]$item.path_key)) {
        throw '敏感源资格门禁缺少路径指纹，健康检查停止'
    }
    $gateByPath[[string]$item.path_key] = $item
}
$quarantinedItems = @($gate.files | Where-Object { -not [bool]$_.eligible })
$quarantinedFileCount = $quarantinedItems.Count
$quarantinedHashCount = [int]$gate.quarantined_hash_count
$quarantinedTypes = @(
    $quarantinedItems |
        ForEach-Object { @($_.sensitive_types) } |
        Sort-Object -Unique
)
$discoveredMarkdown = @(Get-ChildItem -LiteralPath $Vault -Recurse -File -Filter '*.md' |
    Where-Object { $_.FullName -notmatch '[\\/](\.obsidian|\.tools|\.claude|\.claudian)[\\/]' }
)
$discoveredMarkdown = @(
    $discoveredMarkdown |
        Where-Object {
            $_.FullName -notmatch '[\\/]\.git[\\/]' -and
            -not [string]::Equals($_.FullName, $reviewRegistry, [StringComparison]::OrdinalIgnoreCase)
        }
)

$utf8Strict = [Text.UTF8Encoding]::new($false, $true)
$rawByPath = @{}
$eligibleMarkdown = [Collections.Generic.List[IO.FileInfo]]::new()
foreach ($file in $discoveredMarkdown) {
    $relative = [IO.Path]::GetRelativePath($Vault, $file.FullName) -replace '\\', '/'
    $isDataFile = $relative -ne 'AGENTS.md'
    $expectedHash = $null
    if ($isDataFile) {
        $relativeKey = Get-Sha256Text $relative
        if (-not $gateByPath.ContainsKey($relativeKey)) {
            throw '敏感源资格门禁缺少受管文件结果，健康检查停止'
        }
        $gateItem = $gateByPath[$relativeKey]
        if (-not [bool]$gateItem.eligible) {
            continue
        }
        $expectedHash = ([string]$gateItem.sha256).ToLowerInvariant()
    }

    $bytes = [IO.File]::ReadAllBytes($file.FullName)
    if ($expectedHash) {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $actualHash = ([BitConverter]::ToString($sha256.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
        }
        finally {
            $sha256.Dispose()
        }
        if ($actualHash -ne $expectedHash) {
            throw '文件在资格检查后发生变化，健康检查停止'
        }
    }
    try {
        $raw = $utf8Strict.GetString($bytes)
    }
    catch {
        throw '文件无法按 UTF-8 安全解码，健康检查停止'
    }
    if ($raw.Length -gt 0 -and $raw[0] -eq [char]0xFEFF) {
        $raw = $raw.Substring(1)
    }
    $rawByPath[$file.FullName] = $raw
    $eligibleMarkdown.Add($file)
}
$allMarkdown = @($eligibleMarkdown)
$knowledgePrefix = $knowledgeDir.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$knowledgeFiles = @(
    $allMarkdown |
        Where-Object {
            $_.FullName.StartsWith($knowledgePrefix, [StringComparison]::OrdinalIgnoreCase) -and
            $_.Name -notin @('README.md', '_知识索引.md')
        }
)
$criticalPaths = @{}
$dependencySet = @{}
foreach ($relative in @($healthPolicy.critical_paths)) {
    $criticalPaths[[IO.Path]::GetFullPath((Join-Path $Vault $relative))] = $true
}
foreach ($relative in @($healthPolicy.dependency_paths)) {
    $dependencySet[[IO.Path]::GetFullPath((Join-Path $Vault $relative))] = $true
}
$availableCards = @{}
foreach ($file in $knowledgeFiles) { $availableCards[$file.FullName] = $true }
foreach ($requiredPath in @($criticalPaths.Keys) + @($dependencySet.Keys)) {
    if (-not $availableCards.ContainsKey($requiredPath)) {
        throw '关键或依赖卡不可用，健康检查停止'
    }
}
$topicPrefix = $topicDir.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$outputPrefix = $outputDir.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$topicFiles = @(
    $allMarkdown |
        Where-Object { $_.FullName.StartsWith($topicPrefix, [StringComparison]::OrdinalIgnoreCase) }
)
$outputFiles = @(
    $allMarkdown |
        Where-Object { $_.FullName.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase) }
)
$discoveredOutputFiles = @(
    $discoveredMarkdown |
        Where-Object { $_.FullName.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase) }
)

function Get-NormalizedTarget([string]$Value) {
    (($Value -replace '\\', '/' -replace '\.md$', '').Trim('/')).ToLowerInvariant()
}

$knowledgeTargets = @{}
foreach ($file in $knowledgeFiles) {
    $relative = [IO.Path]::GetRelativePath($Vault, $file.FullName) -replace '\\', '/'
    $relativeTarget = Get-NormalizedTarget ([IO.Path]::ChangeExtension($relative, $null))
    $knowledgeTargets[$relativeTarget] = $file.FullName
    $baseTarget = $file.BaseName.ToLowerInvariant()
    if (-not $knowledgeTargets.ContainsKey($baseTarget)) {
        $knowledgeTargets[$baseTarget] = $file.FullName
    }
}

function Resolve-KnowledgeTarget([string]$Value) {
    $target = Get-NormalizedTarget $Value
    if ($knowledgeTargets.ContainsKey($target)) {
        return [string]$knowledgeTargets[$target]
    }
    $baseTarget = ($target -split '/')[-1]
    if ($knowledgeTargets.ContainsKey($baseTarget)) {
        return [string]$knowledgeTargets[$baseTarget]
    }
    return $null
}

$vaultFullPath = [IO.Path]::GetFullPath($Vault).TrimEnd('\', '/')
$vaultPrefix = $vaultFullPath + [IO.Path]::DirectorySeparatorChar

function Test-VaultFileTarget([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }

    $candidate = ($Value -replace '\\', '/').Trim()
    if ([IO.Path]::IsPathRooted($candidate) -or $candidate -match '^[A-Za-z][A-Za-z0-9+.-]*:') {
        return $false
    }

    try {
        $relativePath = $candidate -replace '/', [IO.Path]::DirectorySeparatorChar
        $targetPath = [IO.Path]::GetFullPath((Join-Path $vaultFullPath $relativePath))
    }
    catch {
        return $false
    }

    if (-not $targetPath.StartsWith($vaultPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    return [IO.File]::Exists($targetPath)
}

$knownTargets = @{}
foreach ($file in $discoveredMarkdown) {
    $relative = [IO.Path]::GetRelativePath($Vault, $file.FullName) -replace '\\', '/'
    $knownTargets[(Get-NormalizedTarget ([IO.Path]::ChangeExtension($relative, $null)))] = $true
    $knownTargets[$file.BaseName.ToLowerInvariant()] = $true
}

# 别名仅来自通过资格与哈希校验的内容，不能重新读取隔离文件。
foreach ($file in $allMarkdown) {
    $raw2 = $rawByPath[$file.FullName]
    $fmMatch = [regex]::Match($raw2, '(?s)^---\s*(.*?)\s*---')
    if ($fmMatch.Success) {
        $aliasMatch = [regex]::Match($fmMatch.Groups[1].Value, '(?m)^aliases\s*:\s*(\[[^\]]*\]|[^\r\n]*(?:\r?\n[ \t]+-[^\r\n]+)*)')
        if ($aliasMatch.Success) {
            $aliasBlock = $aliasMatch.Groups[1].Value
            if ($aliasBlock -match '^\s*\[') {
                $aliasItems = $aliasBlock.Trim('[', ']')
            } else {
                $aliasItems = ($aliasBlock -split "`n" | Where-Object { $_ -match '^\s*-\s*' } | ForEach-Object { $_ -replace '^\s*-\s*', '' }) -join ','
            }
            foreach ($alias in ($aliasItems -split ',')) {
                $aliasName = ($alias.Trim().Trim('"', "'")).ToLowerInvariant()
                if ($aliasName -ne '') { $knownTargets[$aliasName] = $true }
            }
        }
    }
}

$inbound = @{}
foreach ($file in $allMarkdown) {
    $raw = $rawByPath[$file.FullName]
    foreach ($match in [regex]::Matches($raw, '\[\[([^\]|#]+)')) {
        $target = Get-NormalizedTarget $match.Groups[1].Value
        $baseName = ($target -split '/')[-1]
        if (-not $inbound.ContainsKey($baseName)) { $inbound[$baseName] = 0 }
        $inbound[$baseName]++
    }
}

# 语义入链只统计 Knowledge / Topics / Outputs 的真实正文关系，排除全量索引造成的虚假“人人有入链”。
$semanticInbound = @{}
foreach ($file in $knowledgeFiles) {
    $semanticInbound[$file.FullName] = $false
}
foreach ($sourceFile in @($knowledgeFiles + $topicFiles + $outputFiles)) {
    $raw = $rawByPath[$sourceFile.FullName]
    $seenTargets = @{}
    foreach ($match in [regex]::Matches($raw, '\[\[([^\]|#]+)')) {
        $targetPath = Resolve-KnowledgeTarget $match.Groups[1].Value
        if (
            $targetPath -and
            -not [string]::Equals($sourceFile.FullName, $targetPath, [StringComparison]::OrdinalIgnoreCase)
        ) {
            $seenTargets[$targetPath] = $true
        }
    }
    foreach ($targetPath in $seenTargets.Keys) {
        $semanticInbound[$targetPath] = $true
    }
}
$semanticOrphanCount = @($knowledgeFiles | Where-Object { -not [bool]$semanticInbound[$_.FullName] }).Count

$outputsWithKnowledgeContext = 0
foreach ($file in $outputFiles) {
    $linkedKnowledge = @{}
    $raw = $rawByPath[$file.FullName]
    foreach ($match in [regex]::Matches($raw, '\[\[([^\]|#]+)')) {
        $targetPath = Resolve-KnowledgeTarget $match.Groups[1].Value
        if ($targetPath) { $linkedKnowledge[$targetPath] = $true }
    }
    if ($linkedKnowledge.Count -ge 2) { $outputsWithKnowledgeContext++ }
}

$issues = [Collections.Generic.List[string]]::new()
$orphanCount = 0
$brokenLinks = 0
$staleCount = 0
$reminderCount = 0
$criticalOverdueCount = 0
$dependencyOverdueCount = 0
$invalidDateCount = 0
$sourcedCount = 0
$disputedCount = 0
$missingCurrentCognitionCount = 0
$missingEvidenceTimelineCount = 0
$canonicalStatuses = @('active', 'draft', 'disputed', 'retired')
$canonicalConfidences = @('高', '中', '低')

foreach ($file in $knowledgeFiles) {
    $raw = $rawByPath[$file.FullName]
    $relative = [IO.Path]::GetRelativePath($Vault, $file.FullName)
    $frontmatter = if ($raw -match '(?s)^---\s*(.*?)\s*---') { $Matches[1] } else { '' }
    $isCritical = $criticalPaths.ContainsKey($file.FullName)
    $isDependency = $dependencySet.ContainsKey($file.FullName)
    if (($isCritical -or $isDependency) -and $frontmatter -notmatch '(?m)^status\s*:\s*["'']?active["'']?\s*$') {
        $issues.Add("- 关键或依赖卡认知未确认（必须 active）：$relative")
    }

    foreach ($field in @('title', 'type', 'status', 'confidence', 'updated', 'last_reviewed')) {
        if ($frontmatter -notmatch "(?m)^$field\s*:") {
            $issues.Add("- 缺少属性 $field：$relative")
        }
    }

    if ($frontmatter -match '(?m)^status\s*:\s*["'']?([^\r\n"'']+)' ) {
        $statusValue = $Matches[1].Trim()
        if ($statusValue -notin $canonicalStatuses) {
            $issues.Add("- 非标准 status（允许：$($canonicalStatuses -join ', ')）：$relative")
        }
    }
    if ($frontmatter -match '(?m)^confidence\s*:\s*["'']?([^\r\n"'']+)' ) {
        $confidenceValue = $Matches[1].Trim()
        if ($confidenceValue -notin $canonicalConfidences) {
            $issues.Add("- 非标准 confidence（允许：$($canonicalConfidences -join ', ')）：$relative")
        }
    }

    $hasSources = $false
    if ($frontmatter -match '(?ms)^sources\s*:\s*(?<value>\[[^\]]*\]|[^\r\n]*(?:\r?\n[ \t]+-[^\r\n]+)*)') {
        $sourceValue = $Matches['value'].Trim()
        $hasSources = $sourceValue -notin @('', '[]')
    }
    elseif ($frontmatter -match '(?m)^source\s*:\s*(?<value>[^\r\n]+)') {
        $sourceValue = $Matches['value'].Trim().Trim('"', "'")
        $hasSources = $sourceValue -notin @('', '[]', 'null', '~')
    }
    if ($frontmatter -notmatch '(?m)^sources?\s*:') {
        $issues.Add("- 缺少属性 source/sources：$relative")
    }
    if ($hasSources) { $sourcedCount++ } else { $issues.Add("- 没有可追溯来源：$relative") }

    if ($frontmatter -match '(?m)^status\s*:\s*disputed\s*$') { $disputedCount++ }

    $baseName = $file.BaseName.ToLowerInvariant()
    if (-not $inbound.ContainsKey($baseName)) {
        $orphanCount++
        $issues.Add("- 没有入链的孤立知识卡：$relative")
    }

    $bodyText = $raw
    if ($frontmatter -ne '') {
        $bodyText = $raw -replace '(?s)^---\s*.*?\s*---', '' -replace '^\s*\r?\n', ''
    }

    if ($bodyText -notmatch '(?m)^[ \t]*##[ \t]+当前认知[ \t]*\r?$') {
        $missingCurrentCognitionCount++
    }
    if ($bodyText -notmatch '(?m)^[ \t]*##[ \t]+证据时间线[ \t]*\r?$') {
        $missingEvidenceTimelineCount++
    }

    foreach ($match in [regex]::Matches($bodyText, '\[\[([^\]|#]+)')) {
        $target = Get-NormalizedTarget $match.Groups[1].Value
        $baseTarget = ($target -split '/')[-1]
        if (
            -not $knownTargets.ContainsKey($target) -and
            -not $knownTargets.ContainsKey($baseTarget) -and
            -not (Test-VaultFileTarget $match.Groups[1].Value)
        ) {
            $brokenLinks++
            $issues.Add("- 断链 $($match.Groups[1].Value)：$relative")
        }
    }

    $dateMatches = [regex]::Matches($frontmatter, '(?m)^last_reviewed[ \t]*:[ \t]*([^\r\n]*)')
    $reviewed = [datetime]::MinValue
    $dateValue = if ($dateMatches.Count -eq 1) { $dateMatches[0].Groups[1].Value.Trim() } else { '' }
    if ($dateValue -match '^(["''])(\d{4}-\d{2}-\d{2})\1$') { $dateValue = $Matches[2] }
    if ($dateMatches.Count -ne 1 -or $dateValue -notmatch '^\d{4}-\d{2}-\d{2}$' -or
        -not [datetime]::TryParseExact($dateValue, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$reviewed) -or
        $reviewed -gt (Get-Date).Date) {
        $invalidDateCount++
        $issues.Add("- 复核日期缺失、重复、非法或在未来：$relative")
    }
    else {
        if ($reviewed -lt (Get-Date).Date.AddDays(-$StaleDays)) {
            $staleCount++
            if ($isCritical) { $criticalOverdueCount++ }
            elseif ($isDependency) { $dependencyOverdueCount++ }
            else { $reminderCount++ }
            $issues.Add("- 超过 $StaleDays 天未复核：$relative")
        }
    }
}

$decisions = @{ accepted = 0; rejected = 0; deferred = 0; pending = 0 }
$dailyReviewDir = Join-Path $Vault '05_Review\Daily'
$candidateFiles = @(
    $allMarkdown |
        Where-Object {
            $_.Directory.FullName -eq $dailyReviewDir -and $_.Name -like '*知识编译候选.md'
        }
)
foreach ($file in $candidateFiles) {
    $raw = $rawByPath[$file.FullName]
    foreach ($match in [regex]::Matches($raw, '人工决定：\s*`?(accepted|rejected|deferred|pending)?`?')) {
        $decision = $match.Groups[1].Value
        if ([string]::IsNullOrWhiteSpace($decision)) { $decisions.pending++ } else { $decisions[$decision]++ }
    }
}
$decided = $decisions.accepted + $decisions.rejected
$acceptanceRate = if ($decided -gt 0) { '{0:P0}' -f ($decisions.accepted / $decided) } else { '暂无已决样本' }
$sourceCoverage = if ($knowledgeFiles.Count -gt 0) { '{0:P0}' -f ($sourcedCount / $knowledgeFiles.Count) } else { '不适用' }
$orphanRate = if ($knowledgeFiles.Count -gt 0) { '{0:P0}' -f ($orphanCount / $knowledgeFiles.Count) } else { '不适用' }
$semanticOrphanRate = if ($knowledgeFiles.Count -gt 0) { '{0:P1}' -f ($semanticOrphanCount / $knowledgeFiles.Count) } else { '不适用' }
$outputKnowledgeContextRate = if ($discoveredOutputFiles.Count -gt 0) { '{0:P1}' -f ($outputsWithKnowledgeContext / $discoveredOutputFiles.Count) } else { '不适用' }

$policyRecheck = @(& $PythonExecutable @policyArgs 2>&1)
if ($LASTEXITCODE -ne 0 -or ($policyRecheck -join "`n") -ne ($policyOutput -join "`n")) {
    throw '健康分层策略校验失败，健康检查停止'
}

$date = Get-Date -Format 'yyyy-MM-dd'
$issueText = if ($issues.Count) { $issues -join "`n" } else { '- 未发现问题。' }
$content = @"
---
title: "$date 知识健康报告"
type: knowledge-health
status: generated
date: $date
---

# $date 知识健康报告

## 知识质量

- 知识卡：$($knowledgeFiles.Count)
- 来源覆盖率：$sourceCoverage
- 孤立卡片：$orphanCount（$orphanRate）
- 断链：$brokenLinks
- 超过 $StaleDays 天未复核：$staleCount
- 争议卡片：$disputedCount
- 敏感隔离：$quarantinedFileCount 个文件 / $quarantinedHashCount 个内容哈希
- 隔离类型：$(if ($quarantinedTypes.Count) { $quarantinedTypes -join ', ' } else { '无' })
- 问题总数：$($issues.Count)
- 健康门策略：$($healthPolicy.schema)
- 健康门策略哈希：$($healthPolicy.sha256)
- 硬阻断问题：$($issues.Count - $reminderCount)
- 普通逾期提醒：$reminderCount
- 关键卡逾期：$criticalOverdueCount
- 本轮依赖逾期：$dependencyOverdueCount
- 日期异常：$invalidDateCount
- 本轮依赖卡：$($dependencySet.Count)

普通逾期仅作为维护提醒；安全故障、结构问题、关键卡和本轮依赖问题仍硬阻断。提醒不代表已复核；采用、修改或升格普通卡前必须按实际依据真实复核，不得刷日期。关键或依赖卡不可读、被隔离或分类不明时整轮停止，不能靠豁免放行。

## 结构质量建议指标（不纳入问题总数）

- 缺少实质语义入链：$semanticOrphanCount / $($knowledgeFiles.Count)（$semanticOrphanRate）
- 连接至少两张知识卡的输出：$outputsWithKnowledgeContext / $($discoveredOutputFiles.Count)（$outputKnowledgeContextRate）
- 缺少“当前认知”段落：$missingCurrentCognitionCount
- 缺少“证据时间线”段落：$missingEvidenceTimelineCount

语义入链只统计 02_Knowledge、03_Topics、04_Outputs 正文指向知识卡的链接，不把 _知识索引.md 的目录链接当作实质关系。输出总数包含 04_Outputs 下发现的全部 Markdown；被敏感门隔离的文件不读取正文，并保守视为未确认关联。这四项用于发现结构性盲点，不会单独把自动化判为失败。

## 人机协作反馈

- 已接受：$($decisions.accepted)
- 已拒绝：$($decisions.rejected)
- 延后：$($decisions.deferred)
- 待决定：$($decisions.pending)
- 人工接受率：$acceptanceRate（只计算 accepted / rejected）

## 问题清单

$issueText

## 处理规则

本报告只发现问题，不自动删除、合并或改写知识结论。规则调整应记录到 [[05_Review/知识迭代日志|知识迭代日志]]。
"@

if ($NoWrite) {
    Write-Output $content
    return
}

New-Item -ItemType Directory -Path $reviewDir -Force | Out-Null
$report = Join-Path $reviewDir "知识健康报告-$date.md"
if ((Test-Path -LiteralPath $report) -and -not $Force) {
    $timestamp = Get-Date -Format 'HHmmss'
    $report = Join-Path $reviewDir "知识健康报告-$date-$timestamp.md"
}
[System.IO.File]::WriteAllText($report, $content, [System.Text.UTF8Encoding]::new($false))
Write-Output $report
