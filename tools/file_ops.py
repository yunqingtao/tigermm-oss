TOOL = {
    "name": "file_ops",
    "description": "File operations: read, write, list, delete, copy, move files and directories.",
    "keywords": ["读取", "写入", "文件", "目录", "列出", "删除", "复制", "移动", "read", "write", "file", "list"],
    "params": [
        {"name": "action", "type": "str", "required": True,
         "enum": ["read", "write", "append", "list", "delete", "copy", "move", "mkdir", "search", "walk"],
         "description": "Operation to perform"},
        {"name": "path", "type": "str", "required": True,
         "description": "File or directory path"},
        {"name": "content", "type": "str", "required": False,
         "description": "Content to write (for 'write')"},
        {"name": "dest", "type": "str", "required": False,
         "description": "Destination path (for copy/move)"},
        {"name": "offset", "type": "int", "required": False,
         "description": "Line number to start reading from"},
        {"name": "limit", "type": "int", "required": False,
         "description": "Maximum lines to read"},
    ]
}
def _safe_int(val, default=0):
    try:
        return int(val) if val != "" and val is not None else default
    except (ValueError, TypeError):
        return default


def _perm_msg(e) -> str:
    """越权/权限错误文案 —— 保留真因, 不叠加前缀。

    2026-09-23: 原写法 f"Access denied: {e}", 而 e 往往已是
    "Access denied: <路径>"(_resolve 里包的) -> 用户看到
    "Access denied: Access denied: <路径>", 真因(为什么被拒)全丢。
    故: e 里已含 denied/outside 就直接用, 不再套前缀。
    """
    _m = str(e)
    if "denied" in _m.lower() or "outside" in _m.lower():
        return _m
    return f"Access denied: {_m}"


#: 已知的**非文本**扩展名 → 人话类型 + 该用哪个工具。
_MEDIA_KIND = {
    ".png": ("图片", "看图"), ".jpg": ("图片", "看图"), ".jpeg": ("图片", "看图"),
    ".gif": ("图片", "看图"), ".webp": ("图片", "看图"), ".bmp": ("图片", "看图"),
    ".ico": ("图标", "看图"), ".tif": ("图片", "看图"), ".tiff": ("图片", "看图"),
    ".mp3": ("音频", "whisper"), ".wav": ("音频", "whisper"), ".m4a": ("音频", "whisper"),
    ".flac": ("音频", "whisper"), ".mp4": ("视频", "whisper"),
    ".zip": ("压缩包", ""), ".rar": ("压缩包", ""), ".7z": ("压缩包", ""),
    ".exe": ("可执行文件", ""), ".dll": ("动态库", ""), ".so": ("动态库", ""),
    ".db": ("数据库", ""), ".sqlite": ("数据库", ""), ".sqlite3": ("数据库", ""),
    ".doc": ("Word 文档", "tiger_office"), ".docx": ("Word 文档", "tiger_office"),
    ".xls": ("Excel 表格", "tiger_office"), ".xlsx": ("Excel 表格", "tiger_office"),
    ".pptx": ("PPT 演示稿", "tiger_office"), ".pdf": ("PDF", "tiger_office"),
}


def binary_kind(p) -> str:
    r"""这是不是"不能用读文本打开"的文件? 返回人话说明, 空串 = 可以当文本读。

    ★★ 2026-09-28 立 (用户 23:38 原话实测): PNG 被当文本读, 回给用户的是
      `'utf-8' codec can't decode byte 0x89 in position 0: invalid start byte`
      —— 一句 Python 异常, 既看不出"这是张图", 也没有下一步。
    判据两级:
      ① 扩展名在白名单里 (图片/音视频/压缩包/可执行/数据库/办公文档) → 直接说类型;
      ② 认不出扩展名 → **嗅头 8KB**: 含 NUL 字节 或 解不出 utf-8 ⇒ 二进制。
    第二级是为了兜住"没有扩展名/扩展名不认识"的二进制文件 (如 .bin / 无后缀)。
    """
    from pathlib import Path as _P
    _p = _P(p)
    _ext = _p.suffix.lower()
    _hit = _MEDIA_KIND.get(_ext)
    if _hit:
        _kind, _tool = _hit
        _how = f"要用 {_tool} 打开" if _tool else "不是文本, 读不出来"
        return (f"{_p.name} 是{_kind}文件(二进制)—— 不能用读文本的方式打开 ({_how})。")
    try:
        _head = _p.open("rb").read(8192)
    except Exception:
        return ""
    # ★★ 2026-09-29 修 (门禁 verify_table_ops 抓出): **不许把"非 UTF-8 的文本"当成二进制**。
    #   第一版只试 utf-8, 于是 GBK/GB18030/UTF-16/BIG5 全被判成二进制 —— 中文 Windows
    #   的文件一大半是 GBK, 这一刀砍太宽了 (实测: 一个 GBK 的 CSV 被拦下, 表格链跟着崩)。
    #   改成**逐个常见编码试**: 任一能解出来就是文本; 全都不行 (或含 NUL 且连 UTF-16 都解不出)
    #   才判二进制。截断的尾巴同理容忍 (见 _tail_ok)。
    if _looks_text(_head):
        return ""
    return (f"{_p.name} 不是文本文件(二进制)—— 不能用读文本的方式打开; "
            f"若是图片请用看图(vision), 若是 Office/PDF 请用 tiger_office。")


#: 文本嗅探要试的编码, 按命中概率排序 (中文 Windows 上 GBK 极常见, 不能只试 utf-8)
_TEXT_ENCS = ("utf-8", "gb18030", "big5", "utf-16", "utf-16-le", "utf-16-be")

#: 允许"追加写"的编码 —— 排除 utf-16 族: 无 BOM 追加会把文件劈成两半 (半 utf-16 半 utf-8),
#: 认不出就**别动文件**, 比"看着写成功、文件其实坏了"值钱。
_APPEND_ENCS = ("utf-8", "gb18030", "big5")


def _read_text_any(p):
    """按常见文本编码依次试读整份文件 ⇒ (正文, 编码); 认不出 ⇒ (None, "")。

    ★★ 2026-09-29 加 (真缺陷, 用户桌面那张 csv 就是 GBK):
      原来 append 只用 `utf-8-sig` 读原有内容, 且**失败被静默吞掉** (`_old = ""`):
        ① 行数核对里的"原有 N 行"变成 0 —— 用户拿到的**证据本身就是假的**
           (09-28 20:12:18 那句 `行数核对(真实重读): 0 → 39`, 用户原话就是"没有写入");
        ② 更要命: 它**先写盘再读回**, 读回时抛 UnicodeDecodeError ⇒ 文件已经被追加了
           一段 utf-8, 而前半段还是 GBK ⇒ 整个文件两种编码混在一起, 谁都解不出来,
           而用户收到的是一句 `'utf-8' codec can't decode byte 0xd0 …` (Python 异常)。
      所以: 认准编码再动手, 认不出就不动手, 事后按**同一个编码**核实。
    """
    try:
        data = p.read_bytes()
    except Exception:
        return None, ""
    if not data:
        return "", "utf-8"
    for _enc in _TEXT_ENCS:
        try:
            return data.decode(_enc), _enc
        except (UnicodeDecodeError, LookupError, UnicodeError):
            continue
    return None, ""



def _looks_text(head: bytes) -> bool:
    r"""这段字节像不像文本? 任一常见编码能解出来 (或只错在截断的尾巴上) 就算像。

    为什么不是"只试 utf-8": GBK/GB18030/BIG5/UTF-16 都是中文 Windows 上的常见文本编码,
    只看 utf-8 会把它们误判成二进制 (实测踩过: GBK 的 CSV 被拦, 表格链跟着崩)。

    为什么要容忍"尾巴错": 按 8KB 切的时候, 边界可能正好落在一个多字节字符中间 ——
    严格解码必然失败 (实测 tools/file_ops.py 头 8KB: `position 8190-8191:
    unexpected end of data`, 距尾只差 2 字节, 那是个被劈成两半的汉字)。
    所以错误位置离尾巴 ≤3 字节的, 一律当"切断了", 不算二进制。
    """
    if head[:2] in (b"\xff\xfe", b"\xfe\xff") or head[:3] == b"\xef\xbb\xbf":
        return True                                   # 带 BOM 的文本
    if b"\x00" in head and not head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False                                  # 裸 NUL ⇒ 二进制 (UTF-16 已由 BOM 放行)
    for _enc in _TEXT_ENCS:
        try:
            head.decode(_enc)
            return True
        except UnicodeDecodeError as _e:
            if _e.start >= len(head) - 3:
                return True                           # 只错在截断的尾巴 ⇒ 文本
        except (LookupError, UnicodeError):
            continue
    return False


"""
File operations plugin v2.0 — full Hermes-level file capabilities.
read(paginated) / write / list / walk(recursive+glob) / search(grep)
/ copy / move / delete / rename / patch / mkdir / exists / stat
All paths validated through storage.file_secure.
"""
import logging
import os as _os, json, sys, fnmatch, shutil, re as _re
from pathlib import Path

# ★ 2026-09-24: 本文件原来没有模块级 logger, 而我加的"最近目录兜底"里用了
#   logger.info ⇒ verify_static_hygiene 的 pyflakes 当场抓到
#   `undefined name 'logger'` (8 PASS / 1 FAIL)。补上声明 (与其他工具同风格)。
logger = logging.getLogger("tools.file_ops")

# ★★ 2026-09-24 加 (复盘报告真问题 3 + 用户实测):
#   13:45:16 刚列过 F:\cs (里面就有 一首诗.txt), 13:45:49 说 打开一首诗.txt
#   → 裸文件名一律落桌面 ⇒ File not found (真文件在 F:\cs)。
#   现在: 记"最近一次被成功读/列/写的目录", **只在默认目录(桌面)找不到时**当第二候选。
#   纯内存 (进程内有效): 不用新文件、重启即清 —— 加性、可回退, 不命中就退回原行为。
_RECENT_MAX = 12
#: ★★ 实测教训 (2026-09-24): 一开始用**模块级内存列表** —— 同进程直测通过, 但在引擎里**失效**。
#   真因: 同一个工具在本架构里有**多个模块实例** (技能 DAG 与 IR chain 各自加载一次,
#   plugin_manager 用 spec_from_file_location 各建一份), 模块级变量**不跨实例共享**。
#   ⇒ 改成落一个**小文件**(跨实例、跨请求、重启引擎也保留), 原子写。
#   位置用系统临时目录: 属运行状态, 不动 data/ (免撞"零污染"断言), 不进分发包。


def _recent_file() -> Path:
    import tempfile as _tf
    return Path(_tf.gettempdir()) / "tmm_recent_dirs.json"


def _recent_dirs() -> list:
    """读"最近目录"(最近的在最前)。读不到 → 空列表 (退回原行为)。"""
    try:
        f = _recent_file()
        if not f.exists():
            return []
        d = json.loads(f.read_text(encoding="utf-8"))
        return [x for x in (d if isinstance(d, list) else []) if isinstance(x, str)]
    except Exception:
        return []


def _note_dir(p) -> None:
    """记下刚被成功读/列/写的目录 (目录本身, 或文件的父目录)。最近的在最前。

    只保留**当前仍存在**的目录 (磁盘上没了的自动淘汰, 免兜底到幽灵路径)。
    """
    try:
        s = str(p)
        d = s if _os.path.isdir(s) else _os.path.dirname(s)
        if not d or not _os.path.isdir(d):
            return
        d = _os.path.normpath(d)
        cur = [x for x in _recent_dirs() if x != d and _os.path.isdir(x)]
        cur.insert(0, d)
        cur = cur[:_RECENT_MAX]
        f = _recent_file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        _os.replace(tmp, f)                      # 原子替换
        logger.info("recent dir noted: %s (共 %d)", d, len(cur))
    except Exception:
        pass

# ── 路径参数强校验 (2026-08-20 硬防线): 中文说明文字混入 → 拒绝 ──
_CJK_RE = _re.compile(r'[\u4e00-\u9fff]')

# 2026-08-20 v2: 中文指令词前缀 — 模型把 "把时间写入agent_test.txt" 整个当 path
# (相对路径无盘符, 原校验不拦 → 创建垃圾文件 "把时间写入agent_test.txt" 于项目根)
_CMD_PREFIX_RE = _re.compile(
    r'^(把|将|写|写入|保存|存|创建|新建|读取|读|打开|获取|添加|追加|覆盖|更新|修改|'
    r'内容|文字|文本|信息|数据|时间|日期|一行|为|叫|名为|名字|称|在|到|请|帮我|我要|'
    r'你好世界|你好|世界|本地|测试|完成)\s*[^A-Za-z:.\\\\/]*', _re.DOTALL)

# 合法相对路径前缀 (safe_path 支持): 桌面/文档/下载/盘符/./../
_LEGAL_REL_PREFIX = _re.compile(r'^(?:desktop|桌面|documents|文档|downloads|下载|D盘|C盘|[A-Za-z]:|[.][./\\\\]?)[\\\\/]?')


def _validate_path_arg(p, arg_name="path"):
    """路径参数强校验 (2026-08-20 硬防线):
    1. 剥离引号
    2. 绝对路径段后混入中文说明文字(如 'C:\\...\\a.txt 帮我保存') → 拒绝执行
    3. 相对路径以中文指令词开头(如 '把时间写入agent_test.txt'/'把你好世界写入x.txt') → 拒绝
    4. 合法中文文件名/目录(路径段内部中文) → 放行
    """
    if not isinstance(p, str) or not p.strip():
        return p
    p0 = p.strip().strip('"').strip("'")
    # ★★ 2026-09-23 加: **路径真实存在 → 直接放行**, 不再猜"是不是说明文字"。
    #   实测: `D:\tmm-考卷\题目\A1 查询.txt` 因**文件名里有空格**,
    #   被下面那条"按空白切分"的正则把 ` 查询.txt` 判成"路径后附中文说明"而拒读
    #   (用户从题目目录粘路径 → 读不了)。
    #   "真实存在"是最强判据: 存在的路径不可能是模型编的描述文字。
    try:
        if _os.path.exists(p0):
            return p0
    except Exception:
        pass
    m = _re.match(r'^([A-Za-z]:[\\/][^\s，。；、！？"\'<>|?*]*)(.*)$', p0, _re.DOTALL)
    if m:
        path_part, tail = m.group(1), m.group(2)
        if tail and _CJK_RE.search(tail):
            raise ValueError(
                f"{arg_name} 参数包含中文说明文字「{tail.strip()[:20]}」— 只允许纯文件路径, "
                f"禁止在路径后附加描述。请重新调用工具, 只传路径本身。")
        # 文件名段: 扩展名之后还有中文 → 说明文字混入 (如 'a.txt的内容')
        _last_seg = path_part.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]
        if '.' in _last_seg:
            _after_ext = _last_seg[_last_seg.rfind('.') + 1:]
            if _after_ext and _CJK_RE.search(_after_ext):
                raise ValueError(
                    f"{arg_name} 参数中「{_last_seg[:30]}」的文件扩展名后混入中文说明文字 — "
                    f"只允许纯文件路径, 请重新调用工具。")
        if tail:
            # 路径段后还有内容但无中文(如含空格的路径后续) → 保留原始串
            return p0
        return path_part
    # ── 相对路径: 中文指令词污染检测 (2026-08-20 v2) ──
    # "把时间写入agent_test.txt" → 以指令词开头 → 拒绝 (模型把指令当路径)
    if _CMD_PREFIX_RE.match(p0) and not _LEGAL_REL_PREFIX.match(p0):
        # 剥指令词后剩余
        _stripped = _CMD_PREFIX_RE.sub('', p0).strip()
        # 如果剩余是纯文件名(含扩展名) → 明确拒绝, 提示模型单独传 path/content
        if _stripped and '.' in _stripped:
            raise ValueError(
                f"{arg_name} 参数「{p0[:40]}」以中文指令词开头 — 模型把指令与文件名混在一起了。"
                f"请重新调用: path 只传纯文件路径(如 'agent_test.txt'), 内容单独放 content 参数。")
        # 剩余无文件名特征 → 纯指令, 同样拒绝
        if _CJK_RE.search(_stripped) and not _re.search(r'\.[A-Za-z0-9]{1,5}$', _stripped):
            raise ValueError(
                f"{arg_name} 参数「{p0[:40]}」是中文指令而非文件路径 — 拒绝执行。"
                f"请重新调用工具, path 传纯文件路径。")
    return p0

PLUGIN = {
    "name": "file_ops",
    "description": "File ops: read/write/list/walk/search/copy/move/delete/rename/patch/mkdir/stat",
    "version": "2.0",
    "requires": [],
    "trigger": ["读文件", "写文件", "列出", "ls", "dir", "文件", "read", "write", "list",
                "读取", "遍历", "扫描", "找文件", "搜索文件", "walk", "scan",
                "复制", "拷贝", "copy", "移动", "mv", "move",
                "删除", "del", "rm", "rename", "重命名",
                "grep", "search", "查找", "搜索", "替换", "patch"],
    "permission": ["file_read", "file_write"],
    "category": "data",
}

async def run(**kwargs) -> dict:
    action = kwargs.get("action", "read")

    # ── 路径参数强校验 (2026-08-20 硬防线): 中文说明文字混入 → 拒绝 ──
    try:
        raw_path = _validate_path_arg(kwargs.get("path", ""), "path")
        dest = _validate_path_arg(kwargs.get("dest", kwargs.get("to", "")), "dest")
    except ValueError as _ve:
        return {"success": False, "error": f"参数校验失败: {_ve}"}
    content = kwargs.get("content", "")
    # ── ★ 2026-09-22 修 (用户实测报错 "Is a directory: ."): 写盘必须有**具体文件名** ──
    #   原来 path 缺省值是 "." ⇒ "写一首诗" 这类没给文件名的请求会真去写当前目录,
    #   报出用户看不懂的 `Is a directory: .`。缺文件名 → 诚实说缺什么, 不拿 "." 去撞。
    # ★ 2026-09-26: 把 append 一并纳进来 —— 真引擎实测 `桌面有个csv…你再增加20个人员信息`
    #   (没给文件名) 时, 追加请求带着**空路径**进来, `_resolve("")` 落到**项目根目录**,
    #   报出 `Permission denied: 'D:\<work>\codex-distill\mary3'` —— 用户看不懂,
    #   而且这是"差一点就往一个目录里追加"的写法。缺文件名就该诚实说缺文件名。
    if action in ("write", "append", "add") and \
            (not raw_path.strip() or raw_path.strip() in (".", "./", ".\\")):
        return {"success": False,
                # ★ 消息里必须保留英文 "directory": 既有回归测试
                #   tests/test_cli_regression.py::test_dot_rejected_for_write 断言 error 含 "directory"
                "error": "没有指定文件名 —— path 为空或指向一个目录 (directory)。"
                         "请给出具体文件, 例如: 桌面/诗.txt"}

    # ── write 空内容防线 (2026-08-20 v2): 模型把 content 揉进 path 导致 content 空 → 拒绝 ──
    # ★ 2026-09-25 加: `empty_ok` —— 原话就是"新建 x.txt"(没别的内容) 时, 意图是**建个空文件**。
    #   上面那条"内容为空"防线是防"模型把内容揉进 path 参数"(真实事故), 与"用户就要个空文件"
    #   是两回事。只有路由侧在「新建/创建 + 有具体文件名 + 没有其它内容」时才置这个标记。
    if action in ("write",) and not str(content or "").strip() and not kwargs.get("empty_ok"):
        return {"success": False,
                "error": "写入内容为空: content 参数缺失或为空。模型可能把内容误放进了 path — "
                         "请重新调用: path 传纯文件路径, 内容放 content 参数。"}
    pattern = kwargs.get("pattern", kwargs.get("query", ""))
    old_str = kwargs.get("old_string", kwargs.get("old", ""))
    new_str = kwargs.get("new_string", kwargs.get("new", ""))
    offset = _safe_int(kwargs.get("offset", 0))
    limit = _safe_int(kwargs.get("limit", 500))
    glob_pat = kwargs.get("glob", kwargs.get("file_glob", "*"))
    max_depth = _safe_int(kwargs.get("max_depth", kwargs.get("depth", 10)))

    # Path validation
    try:
        from storage.file_secure import safe_path, safe_read, safe_write, safe_list_dir
        from config.settings import PROJECT_ROOT as _prj_root
    except ImportError as e:
        return {"success": False, "error": f"Security module unavailable: {e}"}

    def _resolve(p, must_exist=False):
        """Resolve path: absolute paths pass through, relative paths resolve to PROJECT_ROOT.
        2026-08-20 v3: 裸文件名(无盘符/无目录分隔符) → 默认桌面 (用户语义"写文件"=桌面)。
        带目录的路径(core/pipeline.py) 保持项目根。"""
        _p_original = p
        if isinstance(p, str):
            # Strip quotes that models sometimes wrap paths in
            p = p.strip().strip('"').strip("'")
            # Absolute Windows path? Use directly (after security check)
            if _os.path.isabs(p):
                _denied = False
                try:
                    target = safe_path(p, _prj_root)
                except PermissionError:
                    # 路径越权 (不在允许根内) ≠ 文件不存在。旧代码一律吞成 None
                    # → 用户看到 "File not found" 会去查文件, 真因其实是路径不允许。
                    target, _denied = None, True
                if target and (not must_exist or target.exists()):
                    # 绝对路径的 write 目标若落在目录上 → 走 L186 的友好报错, 别提前返回目录
                    # (提前返回会让写入直接撞 PermissionError, 报成 "Access denied" 之类看不懂的错)
                    if action == "write" and target.is_dir():
                        raise IsADirectoryError(
                            f"Is a directory: {_p_original}. Specify a filename.")
                    _note_dir(target)          # ★ 2026-09-24: 记最近目录 (供裸名兜底)
                    return target
                if must_exist:
                    if _denied:
                        raise PermissionError(
                            f"Access denied (outside allowed roots): {_p_original}")
                    raise FileNotFoundError(f"File not found: {_p_original} (resolved to {p})")
            # 裸文件名 (无盘符, 无 / \ 分隔符, 非 . / .. / ./ 前缀) → 桌面 (2026-08-20 v3)
            # ./ 或 ..\ 前缀 = 明确"当前目录" → 保持项目根
            _norm = _os.path.normpath(p)
            _bare = _norm.replace('\\', '/')
            _p_lower = p.strip().lower()
            if (_bare not in ('.', '..') and '/' not in _bare
                    and not _p_lower.startswith(('./', '.\\', '../', '..\\'))
                    and not _bare.lower().startswith(('desktop', '桌面', 'documents', '文档',
                                                       'downloads', '下载', 'd盘', 'c盘'))
                    and not _os.path.splitdrive(_norm)[0]):
                # 2026-09-18 修正: 只有"文件名"(带扩展名)才映射到桌面。
                # 裸目录名 (core / tools) 是项目内相对路径 —— 之前一律映射桌面,
                # 导致 "列出 core 目录的文件" 去找 ~/Desktop/core 而失败 (既有 bug)。
                #   agent_test.txt (有扩展名)      → 桌面  (保留 2026-08-20 的修法)
                #   core (无扩展名, 项目内存在)     → 项目根
                #   报告 (无扩展名, 写意图, 项目内无) → 桌面
                _has_ext = bool(_os.path.splitext(_norm)[1])
                _in_project = _os.path.exists(_os.path.join(str(_prj_root), _norm))
                if _has_ext or (action == "write" and not _in_project):
                    _home_desktop = _os.path.join(_os.path.expanduser("~"), "Desktop")
                    _cand = _os.path.join(_home_desktop, _norm)
                    # ★ 2026-09-24 加: 默认(桌面)那份不在 → 依次试"最近目录"(最近优先)。
                    #   实测场景: 刚列过 F:\cs, 后说"打开一首诗.txt" ⇒ 命中 F:\cs\一首诗.txt。
                    #   只对**已存在**候选生效 ⇒ 写新文件仍落桌面 (不改变写入语义)。
                    if not _os.path.exists(_cand):
                        for _d in _recent_dirs():
                            _c2 = _os.path.join(_d, _norm)
                            try:
                                if _os.path.exists(_c2):
                                    _cand = _c2
                                    logger.info("bare filename %r: 桌面没有 -> 用最近目录 %s",
                                                _norm, _d)
                                    break
                            except Exception:
                                continue
                    p = _cand
            # Relative path
            p = _os.path.normpath(p)
            p = p.removeprefix('.\\\\').removeprefix('./').removeprefix('\\\\').removeprefix('/')
        try:
            target = safe_path(p, _prj_root)
        except PermissionError:
            target = None
        if target and target.is_dir() and action in ("write", "append", "add"):
            # Reject bare directory references — model shouldn't write to a dir
            _bare = raw_path.strip().rstrip('\\/')
            if _bare in ('.', '..', '') or _os.path.isdir(raw_path.strip()) and not _os.path.splitext(raw_path.strip())[1]:
                raise IsADirectoryError(f"Is a directory: {raw_path}. Specify a filename.")
            # Auto-filename for directory write targets
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            p = _os.path.join(p, f"doc_{ts}.txt")
        try:
            t = safe_path(p, _prj_root)
            if must_exist and not t.exists():
                raise FileNotFoundError(f"File not found: {_p_original} (resolved to {t})")
            return t
        except PermissionError as e:
            # 2026-09-23: 保留真因 (原写法用裸路径覆盖消息 -> 用户只看到路径, 查不出为什么被拒)
            _why = str(e)
            raise PermissionError(_why if ("denied" in _why.lower() or "outside" in _why.lower())
                                  else f"Access denied: {p}") from e

    # ═══════════════════════════════════
    # READ (with offset/limit pagination)
    # ═══════════════════════════════════
    if action == "read":
        try:
            target = _resolve(raw_path, must_exist=True)
            if target.is_dir():
                # Auto-redirect: reading a directory → list it
                return await run(action="list", path=raw_path)
            # Excel (.xlsx): parse with openpyxl into readable text
            if target.suffix.lower() in (".xlsx", ".xlsm"):
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
                    sheet_names = list(wb.sheetnames)
                    parts = []
                    for ws in wb.worksheets:
                        rows = []
                        for row in ws.iter_rows(values_only=True):
                            cells = [str(c) for c in row if c is not None]
                            if cells:
                                rows.append("  " + " | ".join(cells))
                        if rows:
                            parts.append(f"[Sheet: {ws.title}]")
                            parts.extend(rows[:200])
                    wb.close()
                    if not parts:
                        return {"success": True, "output": f"{target.name}: 空工作簿 (0 数据行)"}
                    n_rows = len(parts) - len(sheet_names)
                    meta = f"{target.name}: {len(sheet_names)} sheet(s), 前 {min(200, n_rows)} 行"
                    return {"success": True, "output": "\n".join(parts[:500]), "meta": meta}
                except ImportError:
                    return {"success": False, "error": f"{target.name}: 读取 xlsx 需要 openpyxl (pip install openpyxl)"}
                except Exception as e:
                    return {"success": False, "error": f"{target.name}: xlsx 解析失败: {e}"}
            # ★★ 2026-09-28 加: **二进制/图片文件不许当文本读** —— 换成一句人话。
            #   实测 (用户 23:38 原话): PNG 走到这里 → read_text(utf-8) 抛
            #   `'utf-8' codec can't decode byte 0x89 in position 0: invalid start byte`,
            #   而这句被原样回给了用户 (看不出"这是张图", 也看不出下一步该干嘛)。
            #   路由侧已把图片改走 vision (core/intent_router.py 1.42 节) —— 这里是
            #   **兜底**: 任何别的路把图片/二进制送到读口, 也只给一句能懂的话。
            _bk = binary_kind(target)
            if _bk:
                return {"success": False, "error": _bk}
            # ★★ 2026-09-29 加: **不是 UTF-8 的文本也要读得出来**。
            #   中文 Windows 上 GBK 极常见 —— 旧写法直接 read_text("utf-8") 会抛
            #   UnicodeDecodeError, 用户看到的又是一句 Python 异常 (正是本批要堵的那类)。
            #   这里按 utf-8 → gb18030 → big5 → utf-16 依次试, 读出来就照常返回正文。
            try:
                _txt = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                _txt = None
                for _enc in ("gb18030", "big5", "utf-16"):
                    try:
                        _txt = target.read_text(encoding=_enc)
                        logger.info("file_ops.read: %s 不是 UTF-8, 已按 %s 读出",
                                    target.name, _enc)
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                if _txt is None:
                    return {"success": False,
                            "error": (f"{target.name} 不是能当文本读的文件(编码认不出)—— "
                                      f"若是图片请用看图(vision), 若是 Office/PDF 请用 tiger_office。")}
            lines = _txt.split("\n")
            total = len(lines)
            if offset > 0 or limit < total:
                chunk = lines[offset:offset + limit]
                preview = "\n".join(chunk)
                chunk_end = min(offset + limit, total)
                done = chunk_end >= total
                meta_str = f"Lines {offset+1}-{chunk_end} of {total}"
                if done:
                    meta_str += " [FILE COMPLETE - all lines read]"
                else:
                    meta_str += f" [MORE AVAILABLE - use offset={chunk_end} for next page]"
                return {"success": True, "output": preview,
                        "meta": meta_str,
                        "total_lines": total, "offset": offset, "limit": limit, "complete": done}
            return {"success": True, "output": "\n".join(lines),
                    "meta": f"{total} lines [FILE COMPLETE]",
                    "total_lines": total, "complete": True}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        except PermissionError as e:
            return {"success": False, "error": _perm_msg(e)}
        except UnicodeDecodeError as e:
            # ★★ 2026-09-28 加: 兜底的**兜底**。binary_kind 已经拦在前面了, 但
            #   它按"头 8KB"判, 万一二进制块在 8KB 之后 (或文件被改写), 解码错还是会抛。
            #   这里彻底堵死: 用户永远不该看到 `'utf-8' codec can't decode ...`
            #   这种 Python 异常 —— 换成一句人话 + 下一步。
            _nm = getattr(locals().get("target"), "name", "该文件") or "该文件"
            _bk2 = binary_kind(locals().get("target")) if locals().get("target") else ""
            if _bk2:
                return {"success": False, "error": _bk2}
            return {"success": False,
                    "error": (f"{_nm} 不是 UTF-8 文本 (读第 {e.start} 个字节时解不出来)—— "
                              f"它不是能用读文本方式打开的文件; 若是图片请用看图(vision), "
                              f"若是 Office/PDF 请用 tiger_office。")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # WRITE
    # ═══════════════════
    elif action == "write":
        try:
            target = _resolve(raw_path)
            # ── content 污染检测 (2026-08-20 v3): 模型把整句指令塞进 content ──
            # 例: content="桌面 文档 把你好世界 agent_test.txt" 含自身文件名 → 拒绝
            _c = str(content or "")
            _fname = _os.path.basename(str(target))
            if _fname and _fname in _c and len(_c) > len(_fname) + 4:
                return {"success": False,
                        "error": f"写入内容疑似污染: content 包含文件名「{_fname}」和多余文字 — "
                                 f"模型把指令/文件名混进了内容。请重新调用: path 传纯路径, content 只传要写入的纯文本内容。"}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            # 2026-08-20 v3: 返回绝对路径, 模型才知道文件实际落点(裸文件名→桌面)
            return {"success": True, "output": f"Written {len(content)} chars to {target}"}
        except PermissionError as e:
            return {"success": False, "error": _perm_msg(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # APPEND  ★ 2026-09-26 新增 (真缺口)
    # ═══════════════════
    # 为什么要有它: 2026-09-26 用户说「"…人员信息采集表.csv" 增加20条人员信息」,
    # 连问 6 次。本工具**当时只有 read 和 write** —— 那就意味着「增加」这个请求
    # 在工具层面只能变成两种东西: ①把文件读出来 (什么也没改) 或 ②整篇覆盖 (会把
    # 原有 20 条冲掉)。**两个都不是用户要的**, 而路由只能在这两个里挑, 于是连撞
    # 6 次墙 (全落在 file_read, 把旧内容原样糊回给用户)。
    # 缺的不是"更聪明的路由", 是**缺一个原语**。补齐后:
    #   · 绝不截断原文件 (只以 'a' 模式追加)
    #   · 内容为空**拒绝执行** (不糊弄成"已完成")
    #   · 原文件不以换行结尾时先补换行, 防止两行粘成一行
    #   · 写完后**重新读一遍真的数行数**并回报 (证据, 不是宣称)
    elif action in ("append", "add"):
        try:
            target = _resolve(raw_path)
            _c = str(content or "")
            if not _c.strip():
                return {"success": False,
                        "error": "追加内容为空 —— 请说明要往文件里加什么。"
                                 "(本动作不会改动文件, 也不会把原有内容回显当作已经做完。)"}
            target.parent.mkdir(parents=True, exist_ok=True)
            _is_new = not target.exists()
            _old = ""
            _enc = "utf-8"
            if not _is_new:
                # ★★ 2026-09-29: 先按**扩展名/嗅头**判二进制 (复用 read 口同一把尺子)。
                #   少了这一步, 一张 PNG 也能被 gb18030"解"出来 —— 我的门禁 E6 当场抓到:
                #   `\x89PNG…` 里的 0x89 在 GBK 里是合法前导字节 ⇒ 判成"GBK 文本" ⇒ 照样写入。
                _bk = binary_kind(target)
                if _bk:
                    return {"success": False, "error": _bk + " (本动作不会改动文件)"}
                # ★★ 2026-09-29: 认准编码再动手 (原来只试 utf-8-sig, 失败还静默吞掉 ——
                #   中文 Windows 上 GBK 的 csv 会因此被追加成"半 GBK 半 UTF-8", 用户拿到的
                #   既是一句 Python 异常, 又是一个已经坏掉的文件。见 _read_text_any 注释)。
                _old, _enc = _read_text_any(target)
                if _old is None:
                    return {"success": False,
                            "error": (f"{target.name} 不是能用文本方式打开的编码"
                                      f"(二进制或罕见编码) —— **没有改动这个文件**。"
                                      f"若它是图片请用看图(vision), Office/PDF 请用 tiger_office;"
                                      f"若它是 UTF-16 之类, 请先说明, 我不做无 BOM 追加。")}
                if _enc not in _APPEND_ENCS:
                    return {"success": False,
                            "error": (f"{target.name} 是 {_enc} 编码 —— 直接追加会把文件"
                                      f"劈成两种编码, 所以**没有动它**。"
                                      f"要加内容的话, 我先把整篇读出来、按同一编码整体重写。")}
            _sep = "\n" if (_old and not _old.endswith("\n")) else ""
            with target.open("a", encoding=_enc, newline="") as fh:
                fh.write(_sep + _c)
                if not _c.endswith("\n"):
                    fh.write("\n")
            # ★ 证据: 真重读一遍再数, 不复用内存里的字符串 (按**同一个编码**读回)
            _now, _ = _read_text_any(target)
            if _now is None:            # 理论上不会走到; 真走到也绝不把异常甩给用户
                _now = ""
            _before = len([l for l in _old.splitlines() if l.strip()])
            _after = len([l for l in _now.splitlines() if l.strip()])
            return {"success": True,
                    "output": (f"Appended {len(_c)} chars to {target}"
                               + (" (新建文件)" if _is_new else "")
                               + f"\n编码: {_enc}"
                               + f"\n行数核对(真实重读): {_before} → {_after}")}
        except PermissionError as e:
            return {"success": False, "error": _perm_msg(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # LIST
    # ═══════════════════
    elif action == "list":
        try:
            target = _resolve(raw_path, must_exist=True)
            if not target.is_dir():
                return {"success": False, "error": f"Not a directory: {raw_path}"}
            items = sorted(target.iterdir())
            lines = []
            for item in items[:50]:
                tag = "/" if item.is_dir() else ""
                try:
                    sz = item.stat().st_size
                    lines.append(f"  {item.name}{tag} ({sz:,}B)")
                except Exception:
                    lines.append(f"  {item.name}{tag}")
            return {"success": True, "output": "\n".join(lines) or "(empty)"}
        except PermissionError as e:
            return {"success": False, "error": _perm_msg(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ══════════════════════════════════
    # WALK (recursive + glob filter)
    # ══════════════════════════════════
    elif action == "walk":
        try:
            target = _resolve(raw_path, must_exist=True)
            if not target.is_dir():
                return {"success": False, "error": f"Not a directory: {raw_path}"}
            results = []
            for root, dirs, files in _os.walk(str(target)):
                depth = root.replace(str(target), "").count(_os.sep)
                if depth > max_depth:
                    dirs[:] = []
                    continue
                for f in files:
                    if fnmatch.fnmatch(f, glob_pat):
                        fpath = _os.path.join(root, f)
                        size = _os.path.getsize(fpath)
                        results.append(f"{fpath} ({size:,}B)")
                if len(results) >= 200:
                    results.append("... (truncated at 200 results)")
                    break
            return {"success": True, "output": "\n".join(results) or "(no matches)"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ══════════════════════════════════
    # SEARCH (grep in files)
    # ══════════════════════════════════
    elif action in ("search", "grep"):
        if not pattern:
            return {"success": False, "error": "Missing 'pattern' for search"}
        try:
            target = _resolve(raw_path, must_exist=True)
            results = []
            if target.is_file():
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.lower() in line.lower():
                            results.append(f"{target.name}:{i}: {line.rstrip()[:200]}")
                            if len(results) >= 50:
                                break
            elif target.is_dir():
                for root, dirs, files in _os.walk(str(target)):
                    depth = root.replace(str(target), "").count(_os.sep)
                    if depth > max_depth:
                        dirs[:] = []
                        continue
                    for fname in files:
                        if fnmatch.fnmatch(fname, glob_pat):
                            fpath = _os.path.join(root, fname)
                            try:
                                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                    for i, line in enumerate(f, 1):
                                        if pattern.lower() in line.lower():
                                            results.append(f"{fpath}:{i}: {line.rstrip()[:200]}")
                                            if len(results) >= 50:
                                                break
                            except Exception:
                                pass
                            if len(results) >= 50:
                                break
                    if len(results) >= 50:
                        break
            return {"success": True, "output": "\n".join(results) or f"No matches for '{pattern}'"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # COPY
    # ═══════════════════
    elif action in ("copy", "cp"):
        if not dest:
            return {"success": False, "error": "Missing 'dest' for copy"}
        try:
            src = _resolve(raw_path, must_exist=True)
            dst = _resolve(dest) if _os.path.isabs(dest) else safe_path(dest, _prj_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
            else:
                shutil.copy2(str(src), str(dst))
            return {"success": True, "output": f"Copied {raw_path} -> {dest}"}
        except PermissionError as e:
            return {"success": False, "error": _perm_msg(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # MOVE / RENAME
    # ═══════════════════
    elif action in ("move", "mv", "rename"):
        if not dest:
            return {"success": False, "error": "Missing 'dest' for move/rename"}
        try:
            src = _resolve(raw_path, must_exist=True)
            dst = _resolve(dest) if _os.path.isabs(dest) else safe_path(dest, _prj_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return {"success": True, "output": f"Moved {raw_path} -> {dest}"}
        except PermissionError as e:
            return {"success": False, "error": _perm_msg(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # DELETE
    # ═══════════════════
    elif action in ("delete", "del", "rm"):
        try:
            target = _resolve(raw_path, must_exist=True)
            # ★★ 2026-09-24 修 (真 bug, 实测): 原来无条件调 input() 要确认 ——
            #   网页版/中继(无 stdin, DETACHED 进程) 里 input() 直接抛
            #   `EOFError: EOF when reading a line`, 并且以
            #   "Step N (file_ops): EOF when reading a line" 糊到用户脸上 (用户实测)。
            #   修法: 先探 stdin 是否可交互; 不可交互 → **不猜、直接拒绝**并说清怎么删。
            #   (删除是不可逆操作, 无人确认时宁可不动。)
            _stdin_ok = False
            try:
                import sys as _sys_del
                _stdin_ok = bool(_sys_del.stdin) and _sys_del.stdin.isatty()
            except Exception:
                _stdin_ok = False
            if not _stdin_ok:
                return {"success": False,
                        "error": ("删除需要人工确认，但当前没有可交互终端 (网页版/远程)。"
                                  "为安全起见没有执行删除。请在命令行模式操作，"
                                  "或改用 file_ops 的其它动作。")}
            try:
                confirm = input(f"\n  ⚠ 确认删除 {raw_path}? (y/n) ").strip().lower()
            except (EOFError, OSError) as _ie:
                return {"success": False,
                        "error": f"删除确认失败(无输入): {type(_ie).__name__} — 没有执行删除。"}
            if confirm not in ('y', 'yes'):
                return {"success": False, "error": "Delete cancelled by user"}
            if target.is_dir():
                shutil.rmtree(str(target))
            else:
                target.unlink()
            return {"success": True, "output": f"Deleted {raw_path}"}
        except PermissionError as e:
            return {"success": False, "error": _perm_msg(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # PATCH (targeted replace)
    # ═══════════════════
    elif action == "patch":
        if not old_str:
            return {"success": False, "error": "Missing 'old_string' for patch"}
        try:
            target = _resolve(raw_path, must_exist=True)
            text = target.read_text(encoding="utf-8")
            if old_str not in text:
                return {"success": False, "error": f"old_string not found in {raw_path}"}
            new_text = text.replace(old_str, new_str, 1)  # Replace first occurrence
            target.write_text(new_text, encoding="utf-8")
            return {"success": True, "output": f"Patched {raw_path} ({len(old_str)}->{len(new_str)} chars)"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # MKDIR
    # ═══════════════════
    elif action == "mkdir":
        try:
            target = _resolve(raw_path)
            target.mkdir(parents=True, exist_ok=True)
            return {"success": True, "output": f"Created directory {raw_path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # EXISTS
    # ═══════════════════
    elif action == "exists":
        try:
            target = _resolve(raw_path)
            info = f"exists={target.exists()}, "
            if target.exists():
                info += f"is_file={target.is_file()}, is_dir={target.is_dir()}, "
                info += f"size={target.stat().st_size}B" if target.is_file() else ""
            return {"success": True, "output": info}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ═══════════════════
    # STAT
    # ═══════════════════
    elif action == "stat":
        try:
            target = _resolve(raw_path, must_exist=True)
            st = target.stat()
            import datetime
            mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            info = f"size={st.st_size}B, mtime={mtime}, is_file={target.is_file()}, is_dir={target.is_dir()}"
            return {"success": True, "output": info}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Unknown action: {action}"}
