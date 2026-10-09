<#
  Tiger.M.M —— 自动把 Python 装上（收件人不必自己去官网下载）

  为什么用 PowerShell 而不是 .py:
    这一段要在"这台机器**还没有** Python"时也能跑 —— 用 .py 就成死循环了。
    Windows 自带 PowerShell，所以拿它当唯一的引导工具。

  安全上的三道闸（装之前必须全过，否则宁可不装）:
    ① 只从镜像/官方源下载，且四个源依次试
    ② 比对 **SHA256** —— 与 python.org 官方文件逐字节一致才继续
    ③ 校验 **数字签名** 必须是 Python Software Foundation 的有效签名
    两道一起过 ⇒ 即便镜像被投毒，也装不进来。

  安装方式: 只装给当前用户（InstallAllUsers=0），不需要管理员、不弹 UAC、
            不动系统目录、不建快捷方式。

  ★ 本文件必须存成 **UTF-8 带 BOM**。
    PowerShell 5.1 没有 BOM 时会按系统 ANSI 码页(简中=GBK)读文件 ⇒ 中文全乱、脚本哑掉。
    （实测: 带 BOM 正常；不带 BOM 输出为空。）

  退出码: 0 = 装好了（或本来就有）；1 = 没装成（调用方回落到手动指引）。
#>
$ErrorActionPreference = 'Continue'
# 关掉进度条: PS 5.1 画进度条会让下载慢一个数量级（实测）
$ProgressPreference    = 'SilentlyContinue'

$Ver  = '3.11.9'
$Name = "python-$Ver-amd64.exe"
# 与 python.org 官方 python-3.11.9-amd64.exe 的 SHA256（实测核对过）
$Sha  = '5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde'
$Urls = @(
  "https://mirrors.huaweicloud.com/python/$Ver/$Name",
  "https://mirrors.aliyun.com/python-release/windows/$Name",
  "https://registry.npmmirror.com/-/binary/python/$Ver/$Name",
  "https://www.python.org/ftp/python/$Ver/$Name"
)

function Say([string]$m){ Write-Host $m }

Say ''
Say '=============================================================='
Say ' Tiger.M.M —— 自动安装 Python 3.11（只有这一次需要装）'
Say '=============================================================='
Say ''

# ── 1) 下载 ────────────────────────────────────────────────────────────
$dst = Join-Path $env:TEMP $Name
$got = $false
foreach($u in $Urls){
  try{
    Say "  正在下载: $u"
    Invoke-WebRequest -Uri $u -OutFile $dst -UseBasicParsing -TimeoutSec 900
    if((Get-Item $dst).Length -gt 20MB){ $got = $true; Say '  下载完成'; break }
    Say '  文件不完整，换下一个源'
  }catch{
    $why = $_.Exception.Message -split "`n" | Select-Object -First 1
    Say "  这个源不行（$why），换下一个"
  }
}
if(-not $got){
  Say ''
  Say '[X] 四个源都没下下来 —— 网络可能被挡了。'
  Say '    请照 PYTHON_FIRST.txt 手动装一次（只有这一次）。'
  exit 1
}

# ── 2) 校验（哈希 + 数字签名，两道都要过）───────────────────────────────
Say '  正在校验文件…'
$h = (Get-FileHash -Path $dst -Algorithm SHA256).Hash.ToLower()
if($h -ne $Sha){
  Say ''
  Say '[X] 下载到的文件跟官方对不上（哈希不一致）—— 不敢装它。'
  Say "    拿到: $h"
  Say "    应为: $Sha"
  Say '    请照 PYTHON_FIRST.txt 手动装一次。'
  exit 1
}
$sig = Get-AuthenticodeSignature -FilePath $dst
$subj = ''
if($sig.SignerCertificate){ $subj = $sig.SignerCertificate.Subject }
if($sig.Status -ne 'Valid' -or $subj -notmatch 'Python Software Foundation'){
  Say ''
  Say "[X] 这个文件没有 Python 官方的有效数字签名（状态: $($sig.Status)）—— 不敢装它。"
  Say '    请照 PYTHON_FIRST.txt 手动装一次。'
  exit 1
}
Say '  校验通过（与官方逐字节一致，且带 Python 官方签名）'

# ── 3) 静默安装（只给当前用户）────────────────────────────────────────
Say ''
Say '  正在安装，约 30 秒，别关窗口…'
$insArgs = @('/quiet','InstallAllUsers=0','PrependPath=1','Include_launcher=1',
             'Include_test=0','Include_doc=0','Shortcuts=0','AssociateFiles=0')
try{
  $p = Start-Process -FilePath $dst -ArgumentList $insArgs -Wait -PassThru
  Say "  安装程序退出码: $($p.ExitCode)"
}catch{
  Say "  [X] 安装程序起不来: $($_.Exception.Message)"
  exit 1
}

# ── 4) 找到它到底装哪儿了 ─────────────────────────────────────────────
# 先看标准位置（我们自己装的就在这），再看注册表。
# ★ 别反过来: 这台机器上可能有别的工具把 python 预置到别处并注册成"3.12 安装"
#   （实测: C:\ProgramData\Accio\pre-install\...\python），先查注册表就会报出别人的 python。
$found = $null
foreach($v in @('313','312','311','310')){
  $cand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
  if(Test-Path $cand){ $found = $cand; break }
  $cand = Join-Path $env:ProgramFiles "Python$v\python.exe"
  if(Test-Path $cand){ $found = $cand; break }
}
if(-not $found){
  foreach($v in @('3.13','3.12','3.11','3.10')){
    foreach($hive in @('HKCU','HKLM')){
      try{
        $ip = (Get-ItemProperty -Path "${hive}:\Software\Python\PythonCore\$v\InstallPath" -ErrorAction Stop).'(default)'
        if($ip){
          $exe = Join-Path $ip 'python.exe'
          if(Test-Path $exe){ $found = $exe; break }
        }
      }catch{}
    }
    if($found){ break }
  }
}
Say ''
if($found){
  Say '[OK] Python 装好了。位置:'
  Say "     $found"
  exit 0
}
Say '[X] 安装程序跑完了，但没找到 python.exe。请照 PYTHON_FIRST.txt 手动装一次。'
exit 1
