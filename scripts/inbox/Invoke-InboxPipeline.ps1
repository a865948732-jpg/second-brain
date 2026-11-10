[CmdletBinding()]
param(
    [ValidateSet('Enqueue', 'Process', 'Retry', 'Status')]
    [string]$Action = 'Status',

    [string]$Path,

    [ValidateSet('knowledge', 'topic', 'source', 'archive', 'ignore')]
    [string]$Decision,

    [string]$Reason,

    [string]$QueueId,

    [switch]$Apply,

    [string]$VaultRoot
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    return [System.IO.Path]::GetFullPath($LiteralPath)
}

function Test-PathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    $childPath = (Get-FullPath $Child).TrimEnd('\', '/')
    $parentPath = (Get-FullPath $Parent).TrimEnd('\', '/')
    return $childPath.Equals($parentPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $childPath.StartsWith($parentPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-RelativeVaultPath {
    param([Parameter(Mandatory = $true)][string]$AbsolutePath)

    $rootWithSlash = $script:Root.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $rootUri = New-Object System.Uri($rootWithSlash)
    $pathUri = New-Object System.Uri((Get-FullPath $AbsolutePath))
    return [System.Uri]::UnescapeDataString($rootUri.MakeRelativeUri($pathUri).ToString()).Replace('\', '/')
}

function Resolve-VaultPath {
    param([Parameter(Mandatory = $true)][string]$VaultRelativePath)

    $nativePath = $VaultRelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
    $resolved = Get-FullPath (Join-Path $script:Root $nativePath)
    if (-not (Test-PathInside -Child $resolved -Parent $script:Root)) {
        throw "路径越界：$VaultRelativePath"
    }
    return $resolved
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Container)) {
        [void](New-Item -ItemType Directory -Path $LiteralPath)
    }
}

function Write-NewJsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)]$Data
    )

    if (Test-Path -LiteralPath $LiteralPath) {
        throw "拒绝覆盖已有文件：$LiteralPath"
    }

    $directory = Split-Path -Parent $LiteralPath
    Ensure-Directory $directory
    $temporaryPath = Join-Path $directory ('.tmp-' + [System.Guid]::NewGuid().ToString('N') + '.json')
    $json = $Data | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($temporaryPath, $json + [Environment]::NewLine, $script:Utf8NoBom)
    Move-Item -LiteralPath $temporaryPath -Destination $LiteralPath
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    return Get-Content -LiteralPath $LiteralPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function New-QueueId {
    param([Parameter(Mandatory = $true)][string]$Hash)
    $timestamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
    $nonce = [System.Guid]::NewGuid().ToString('N').Substring(0, 6)
    return "$timestamp-$($Hash.Substring(0, 10).ToLowerInvariant())-$nonce"
}

function Assert-QueueId {
    param([Parameter(Mandatory = $true)][string]$Id)
    if ($Id -notmatch '^[A-Za-z0-9-]+$') {
        throw "非法 QueueId：$Id"
    }
}

function Get-RequestPath {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Id
    )
    Assert-QueueId $Id
    return Join-Path (Join-Path $script:QueueRoot $State) "$Id.request.json"
}

function Get-ResultPath {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Id
    )
    Assert-QueueId $Id
    return Join-Path (Join-Path $script:QueueRoot $State) "$Id.result.json"
}

function Resolve-InboxSource {
    param([Parameter(Mandatory = $true)][string]$InputPath)

    $candidate = if ([System.IO.Path]::IsPathRooted($InputPath)) {
        Get-FullPath $InputPath
    } else {
        Get-FullPath (Join-Path $script:Root $InputPath)
    }

    if (-not (Test-PathInside -Child $candidate -Parent $script:InboxRoot)) {
        throw "只允许显式入队 00_Inbox 内的文件：$candidate"
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "文件不存在：$candidate"
    }
    if ([System.IO.Path]::GetFileName($candidate).Equals('README.md', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'README.md 不能进入摄取队列。'
    }

    $supported = @('.md', '.txt', '.html', '.url', '.json')
    $extension = [System.IO.Path]::GetExtension($candidate).ToLowerInvariant()
    if ($supported -notcontains $extension) {
        throw "当前管线只处理文本类文件：$($supported -join ', ')"
    }
    return $candidate
}

function Get-DestinationPath {
    param([Parameter(Mandatory = $true)]$Request)

    $sourcePath = Resolve-VaultPath $Request.source_path
    $fileName = [System.IO.Path]::GetFileName($sourcePath)
    switch ($Request.decision) {
        'knowledge' { $directory = Join-Path $script:QueueRoot '待编译\knowledge' }
        'topic'     { $directory = Join-Path $script:QueueRoot '待编译\topic' }
        'source'    { $directory = Join-Path $script:QueueRoot '待人工入源' }
        'archive'   { $directory = Join-Path $script:Root ('05_Review\待归档\' + (Get-Date -Format 'yyyy-MM-dd')) }
        'ignore'    { return $sourcePath }
        default     { throw "未知 decision：$($Request.decision)" }
    }
    return Join-Path $directory $fileName
}

function New-Request {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$RouteDecision,
        [Parameter(Mandatory = $true)][string]$RouteReason,
        [string]$RetryOf
    )

    if ([string]::IsNullOrWhiteSpace($RouteReason)) {
        throw 'Reason 必填，用于保留路由依据。'
    }

    $hash = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $relativeSource = Get-RelativeVaultPath $SourcePath
    foreach ($pendingPath in @(Get-ChildItem -LiteralPath (Join-Path $script:QueueRoot 'pending') -Filter '*.request.json' -File)) {
        $pending = Read-JsonFile $pendingPath.FullName
        if ($pending.source_path -eq $relativeSource -and
            $pending.source_sha256 -eq $hash -and
            $pending.decision -eq $RouteDecision) {
            return [pscustomobject]@{
                action = 'already-enqueued'
                id = $pending.id
                source = $pending.source_path
                decision = $pending.decision
                request = Get-RelativeVaultPath $pendingPath.FullName
            }
        }
    }

    $id = New-QueueId $hash
    $request = [ordered]@{
        schema_version = 1
        id = $id
        created_at = (Get-Date).ToString('o')
        source_path = $relativeSource
        source_sha256 = $hash
        decision = $RouteDecision
        reason = $RouteReason.Trim()
        retry_of = $RetryOf
    }

    $requestPath = Get-RequestPath -State 'pending' -Id $id
    Write-NewJsonFile -LiteralPath $requestPath -Data $request
    return [pscustomobject]@{
        action = 'enqueued'
        id = $id
        source = $request.source_path
        decision = $RouteDecision
        request = Get-RelativeVaultPath $requestPath
    }
}

function Invoke-Enqueue {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'Enqueue 需要 -Path。' }
    if ([string]::IsNullOrWhiteSpace($Decision)) { throw 'Enqueue 需要 -Decision。' }
    $sourcePath = Resolve-InboxSource $Path
    return New-Request -SourcePath $sourcePath -RouteDecision $Decision -RouteReason $Reason
}

function Move-RequestWithResult {
    param(
        [Parameter(Mandatory = $true)][string]$RequestPath,
        [Parameter(Mandatory = $true)]$Request,
        [Parameter(Mandatory = $true)][ValidateSet('processed', 'failed')][string]$State,
        [Parameter(Mandatory = $true)]$Result
    )

    $targetRequest = Get-RequestPath -State $State -Id $Request.id
    $targetResult = Get-ResultPath -State $State -Id $Request.id
    if ((Test-Path -LiteralPath $targetRequest) -or (Test-Path -LiteralPath $targetResult)) {
        throw "队列历史已存在，拒绝覆盖：$($Request.id)"
    }
    Write-NewJsonFile -LiteralPath $targetResult -Data $Result
    Move-Item -LiteralPath $RequestPath -Destination $targetRequest
}

function Invoke-OnePendingRequest {
    param([Parameter(Mandatory = $true)][string]$RequestPath)

    $request = Read-JsonFile $RequestPath
    $sourcePath = Resolve-InboxSource $request.source_path
    $actualHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $request.source_sha256) {
        throw '源文件在入队后发生变化；请检查后重新入队。'
    }

    $destinationPath = Get-DestinationPath $request
    if ($request.decision -ne 'ignore' -and (Test-Path -LiteralPath $destinationPath)) {
        throw "目标已存在，拒绝覆盖：$destinationPath"
    }
    foreach ($historyPath in @(
        (Get-RequestPath -State 'processed' -Id $request.id),
        (Get-ResultPath -State 'processed' -Id $request.id)
    )) {
        if (Test-Path -LiteralPath $historyPath) {
            throw "成功历史已存在，拒绝移动源文件：$historyPath"
        }
    }

    $plan = [pscustomobject]@{
        action = if ($Apply) { 'process' } else { 'dry-run' }
        id = $request.id
        decision = $request.decision
        source = $request.source_path
        destination = Get-RelativeVaultPath $destinationPath
    }
    if (-not $Apply) { return $plan }

    if ($request.decision -ne 'ignore') {
        Ensure-Directory (Split-Path -Parent $destinationPath)
        Move-Item -LiteralPath $sourcePath -Destination $destinationPath
    }

    $result = [ordered]@{
        schema_version = 1
        id = $request.id
        completed_at = (Get-Date).ToString('o')
        status = 'processed'
        source_path = $request.source_path
        destination_path = Get-RelativeVaultPath $destinationPath
        decision = $request.decision
        reason = $request.reason
    }
    Move-RequestWithResult -RequestPath $RequestPath -Request $request -State 'processed' -Result $result
    return $plan
}

function Invoke-Process {
    $requestPaths = if ([string]::IsNullOrWhiteSpace($QueueId)) {
        @(Get-ChildItem -LiteralPath (Join-Path $script:QueueRoot 'pending') -Filter '*.request.json' -File | Sort-Object Name | ForEach-Object FullName)
    } else {
        @(Get-RequestPath -State 'pending' -Id $QueueId)
    }

    if ($requestPaths.Count -eq 0) {
        return [pscustomobject]@{ action = 'status'; message = 'pending 队列为空。' }
    }

    $output = @()
    foreach ($requestPath in $requestPaths) {
        try {
            if (-not (Test-Path -LiteralPath $requestPath -PathType Leaf)) {
                throw "pending 请求不存在：$requestPath"
            }
            $output += Invoke-OnePendingRequest $requestPath
        } catch {
            if (-not $Apply) {
                $request = Read-JsonFile $requestPath
                $output += [pscustomobject]@{
                    action = 'would-fail'
                    id = $request.id
                    decision = $request.decision
                    source = $request.source_path
                    error = $_.Exception.Message
                }
                continue
            }
            if (-not (Test-Path -LiteralPath $requestPath -PathType Leaf)) { throw }
            $request = Read-JsonFile $requestPath
            $result = [ordered]@{
                schema_version = 1
                id = $request.id
                failed_at = (Get-Date).ToString('o')
                status = 'failed'
                source_path = $request.source_path
                decision = $request.decision
                reason = $request.reason
                error = $_.Exception.Message
            }
            Move-RequestWithResult -RequestPath $requestPath -Request $request -State 'failed' -Result $result
            $output += [pscustomobject]@{ action = 'failed'; id = $request.id; error = $_.Exception.Message }
        }
    }
    return $output
}

function Invoke-Retry {
    if ([string]::IsNullOrWhiteSpace($QueueId)) { throw 'Retry 需要 -QueueId。' }
    $failedRequestPath = Get-RequestPath -State 'failed' -Id $QueueId
    if (-not (Test-Path -LiteralPath $failedRequestPath -PathType Leaf)) {
        throw "失败请求不存在：$QueueId"
    }
    $failedRequest = Read-JsonFile $failedRequestPath
    $sourcePath = Resolve-InboxSource $failedRequest.source_path
    $retryReason = if ([string]::IsNullOrWhiteSpace($Reason)) {
        "重试失败请求 $QueueId；原路由依据：$($failedRequest.reason)"
    } else {
        $Reason
    }
    return New-Request -SourcePath $sourcePath -RouteDecision $failedRequest.decision -RouteReason $retryReason -RetryOf $QueueId
}

function Get-QueueStatus {
    $states = @('pending', 'processed', 'failed')
    $counts = [ordered]@{}
    foreach ($state in $states) {
        $counts[$state] = @(Get-ChildItem -LiteralPath (Join-Path $script:QueueRoot $state) -Filter '*.request.json' -File).Count
    }
    return [pscustomobject]@{
        queue_root = Get-RelativeVaultPath $script:QueueRoot
        pending = $counts.pending
        processed = $counts.processed
        failed = $counts.failed
    }
}

if ([string]::IsNullOrWhiteSpace($VaultRoot)) {
    $VaultRoot = if ($env:SECOND_BRAIN_VAULT) { $env:SECOND_BRAIN_VAULT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.local/vault')) }
}
$script:Root = Get-FullPath $VaultRoot
$script:InboxRoot = Join-Path $script:Root '00_Inbox'
$script:QueueRoot = Join-Path $script:Root '05_Review\摄取队列'

if (-not (Test-Path -LiteralPath $script:Root -PathType Container)) { throw "Vault 不存在：$script:Root" }
if (-not (Test-Path -LiteralPath $script:InboxRoot -PathType Container)) { throw "00_Inbox 不存在：$script:InboxRoot" }
foreach ($directory in @('pending', 'processed', 'failed')) {
    Ensure-Directory (Join-Path $script:QueueRoot $directory)
}

$result = switch ($Action) {
    'Enqueue' { Invoke-Enqueue }
    'Process' { Invoke-Process }
    'Retry'   { Invoke-Retry }
    'Status'  { Get-QueueStatus }
}
$result | ConvertTo-Json -Depth 8
