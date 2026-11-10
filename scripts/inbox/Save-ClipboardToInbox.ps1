[CmdletBinding()]
param(
    [ValidateSet('wechat', 'qq', 'workbuddy', 'web', 'manual')]
    [string]$Source = 'manual',

    [string]$Text,

    [string]$VaultRoot = $(if ($env:SECOND_BRAIN_VAULT) { $env:SECOND_BRAIN_VAULT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.local/vault')) }),

    [switch]$Notify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Show-Notice {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [Parameter(Mandatory = $true)][string]$Title,
        [ValidateSet('Information', 'Error')][string]$Kind = 'Information'
    )

    if (-not $Notify) { return }
    Add-Type -AssemblyName System.Windows.Forms
    $icon = if ($Kind -eq 'Error') {
        [System.Windows.Forms.MessageBoxIcon]::Error
    } else {
        [System.Windows.Forms.MessageBoxIcon]::Information
    }
    [void][System.Windows.Forms.MessageBox]::Show(
        $Message,
        $Title,
        [System.Windows.Forms.MessageBoxButtons]::OK,
        $icon
    )
}

function ConvertTo-YamlSingleQuoted {
    param([AllowEmptyString()][string]$Value)
    return "'$($Value.Replace("'", "''"))'"
}

function Protect-SensitiveText {
    param([Parameter(Mandatory = $true)][string]$Value)

    $protected = $Value
    $protected = [regex]::Replace(
        $protected,
        '(?im)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|passwd|secret|authorization)\b\s*[:=：]\s*\S+',
        '$1: [REDACTED]'
    )
    $protected = [regex]::Replace($protected, '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]')
    $protected = [regex]::Replace($protected, '(?i)\bsk-[A-Za-z0-9_-]{12,}\b', '[REDACTED]')
    $protected = [regex]::Replace(
        $protected,
        '(?is)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        '[REDACTED PRIVATE KEY]'
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(验证码|校验码|verification code|otp)\s*[:：]?\s*\d{4,8}',
        '$1: [REDACTED]'
    )
    return $protected
}

function Get-FirstUrl {
    param([Parameter(Mandatory = $true)][string]$Value)
    $match = [regex]::Match($Value, 'https?://[^\s<>"'']+')
    if (-not $match.Success) { return '' }
    return $match.Value.TrimEnd('.', ',', ';', ':', '!', '?', ')', ']', '}', '。', '，', '；', '：', '！', '？', '）', '】')
}

function Get-CaptureTitle {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [AllowEmptyString()][string]$Url
    )

    $trimmed = $Value.Trim()
    if ($Url -and $trimmed -eq $Url) {
        try { return ([System.Uri]$Url).Host } catch { return '链接收件' }
    }

    $firstLine = @($trimmed -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })[0]
    $title = [regex]::Replace($firstLine, '[\x00-\x1F<>:"/\\|?*]', ' ')
    $title = [regex]::Replace($title, '\s+', ' ').Trim(' ', '.')
    if ([string]::IsNullOrWhiteSpace($title)) { $title = '移动收件' }
    if ($title.Length -gt 60) { $title = $title.Substring(0, 60).Trim() }
    return $title
}

try {
    $root = [System.IO.Path]::GetFullPath($VaultRoot)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Vault 不存在：$root"
    }

    if ([string]::IsNullOrWhiteSpace($Text)) {
        $Text = Get-Clipboard -Raw -Format Text
    }
    if ([string]::IsNullOrWhiteSpace($Text)) {
        throw '剪贴板没有可保存的文本。图片和文件请先保存到附件目录。'
    }

    $safeText = Protect-SensitiveText $Text.Trim()
    $wasRedacted = $safeText -ne $Text.Trim()
    $url = Get-FirstUrl $safeText
    $title = Get-CaptureTitle -Value $safeText -Url $url
    $safeFileName = [regex]::Replace($title, '[\x00-\x1F<>:"/\\|?*]', '_').Trim(' ', '.')
    if ([string]::IsNullOrWhiteSpace($safeFileName)) { $safeFileName = '移动收件' }

    $sourceLabels = @{
        wechat = '微信文件传输助手'
        qq = 'QQ 我的电脑'
        workbuddy = 'WorkBuddy Windows'
        web = 'Windows 浏览器'
        manual = 'Windows 手动收件'
    }
    $contentKind = if ($url -and $safeText.Trim() -eq $url) { 'link' } else { 'note' }
    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $today = Get-Date -Format 'yyyy-MM-dd'
    $targetDirectory = Join-Path $root '00_Inbox\WorkBuddy'
    if (-not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
        [void](New-Item -ItemType Directory -Path $targetDirectory)
    }
    $targetPath = Join-Path $targetDirectory "$timestamp-$safeFileName.md"
    if (Test-Path -LiteralPath $targetPath) { throw "拒绝覆盖已有收件：$targetPath" }

    $frontmatterTitle = ConvertTo-YamlSingleQuoted $title
    $frontmatterSource = ConvertTo-YamlSingleQuoted $sourceLabels[$Source]
    $frontmatterUrl = ConvertTo-YamlSingleQuoted $url
    $markdown = @"
---
title: $frontmatterTitle
type: inbox
status: pending
created: $today
updated: $today
source: $frontmatterSource
source_url: $frontmatterUrl
captured_by: windows-clipboard
content_kind: $contentKind
decision: pending
tags: [mobile-capture, $Source]
confidence: 低
redacted: $($wasRedacted.ToString().ToLowerInvariant())
---

# $title

## 原始内容

$safeText

## 处理备注

- 可能关联：
- 待确认：
"@
    [System.IO.File]::WriteAllText($targetPath, $markdown.TrimStart() + [Environment]::NewLine, $Utf8NoBom)

    $relativePath = $targetPath.Substring($root.TrimEnd('\').Length + 1).Replace('\', '/')
    Show-Notice -Title '第二大脑收件' -Message "已存入：$relativePath"
    [pscustomobject]@{
        status = 'captured'
        path = $relativePath
        source = $Source
        redacted = $wasRedacted
    } | ConvertTo-Json
} catch {
    Show-Notice -Title '第二大脑收件失败' -Message $_.Exception.Message -Kind Error
    throw
}
