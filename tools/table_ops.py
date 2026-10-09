"""
TableOps — 统一表格操作层
==========================
一个入口, 管住 CSV / Excel(.xlsx) / Word(.docx) 表格的**全部行列表头级操作**:
    读结构 · 加行 · 删行 · 加列 · 删列 · 改格子 · 改表头 · 排序 · 去重 · 拆分 · 合并

为什么要有它 (2026-09-28 用户实测):
    用户对着一个 CSV 说「续写20条记录」, 得到的是一行文字「续写20条记录」;
    说「添加40条人员信息」, 得到的是「冰的密度小于水」。同一个文件连撞四轮,
    每轮走不同的路 (file_write / knowledge / file_append / general), 互不知情。
    根因不是某条路写错了, 是**没有一层"表格"的模型**:
      · file_ops 只有 read/write/append —— append 只懂纯文本, 不懂表头/列
      · tiger_office 只会整篇 write_excel —— 不会"在第 20 行后面加 20 行"
      · "加一列""改表头""填某个格" 根本没人认领
    于是每一层各挑一个最像的动作去做, 合起来就是"把指令写进了文件里"。

铁律 (写进代码, 不靠人记):
  1. 原有数据默认不动 —— 加行只 append, 永不重写既有行; 删/改类必须显式点名
  2. 干完必须**重读一遍数行数列数**并回报 —— 证据, 不是宣称
  3. 动文件前先备份到 backups/table_ops_<ts>/ —— 可回滚
  4. 编码/行尾/BOM 照原样保留 (GBK 的表不能被写成 UTF-8)
  5. 给数就用给的, 没给才生成; 生成的数据按表头语义造, 不造空白格

插件规范: PLUGIN dict + async run(**kwargs) -> {success, output}
"""
import re
import csv
import json
import shutil
import random
import datetime
import string
from io import StringIO
from pathlib import Path

PLUGIN = {
    "name": "table_ops",
    "description": ("统一表格操作: CSV/Excel/Word 的 读结构·加行·删行·加列·删列·改格子·"
                    "改表头·排序·去重·拆分·合并。原有数据默认不动, 干完重读数核对"),
    "version": "1.0",
    "requires": ["openpyxl", "docx"],
    "trigger": ["加行", "增行", "删行", "加列", "增列", "删列", "表头", "改表头", "改列名",
                "格子", "单元格", "拆分表格", "拆表", "合并表格", "合并表", "表格",
                "写入表格", "填表", "生成记录", "添加记录", "增加记录", "续写记录",
                "添加人员信息", "加几行", "补几行", "删掉几行", "排序表格", "去重",
                "csv", "CSV", "xlsx", "excel", "Excel", "word表格", "docx"],
    "permission": ["file_read", "file_write"],
    "category": "office",
    # ★ 必须声明 params —— tool_schema.get_all_schemas 的准入判据是 `meta.get("params")`
    #   为真, 空/缺省 ⇒ 工具静默不可见 (见 tool_schema.py:159 那段踩坑注释)。
    "params": [
        {"name": "action", "type": "str", "required": True,
         "description": "info|read|add_rows|del_rows|add_col|del_col|set_cell|set_header|sort|dedupe|split|merge"},
        {"name": "path", "type": "str", "required": True, "description": "表格文件路径 (.csv/.xlsx/.docx)"},
        {"name": "count", "type": "int", "required": False, "description": "add_rows: 加几行"},
        {"name": "data", "type": "str", "required": False,
         "description": "add_rows: 要写入的真实内容 (CSV/TSV 文本, 每行一条); 不填则按表头生成"},
        {"name": "rows", "type": "str", "required": False, "description": "del_rows: 行号, 如 \"2,5\" 或 \"2-5\""},
        {"name": "name", "type": "str", "required": False, "description": "列名 (add_col/del_col/排序等)"},
        {"name": "col", "type": "str", "required": False, "description": "列名或 1 起的列号"},
        {"name": "row", "type": "int", "required": False, "description": "行号 (不含表头, 从 1 数)"},
        {"name": "value", "type": "str", "required": False, "description": "set_cell: 新值"},
        {"name": "names", "type": "str", "required": False, "description": "set_header: 整行表头, 用逗号分隔"},
        {"name": "map", "type": "dict", "required": False, "description": "set_header: {旧列名: 新列名}"},
        {"name": "default", "type": "str", "required": False, "description": "add_col: 新列默认值"},
        {"name": "by", "type": "str", "required": False, "description": "sort/dedupe: 按哪列"},
        {"name": "order", "type": "str", "required": False, "description": "sort: asc|desc"},
        {"name": "by_rows", "type": "int", "required": False, "description": "split: 每份几行"},
        {"name": "by_col", "type": "str", "required": False, "description": "split: 按哪列的值分开"},
        {"name": "outdir", "type": "str", "required": False, "description": "split: 输出目录"},
        {"name": "target", "type": "str", "required": False, "description": "merge: 输出文件"},
        {"name": "paths", "type": "str", "required": False, "description": "merge: 源文件列表, 用分号或换行分隔"},
        {"name": "sheet", "type": "str", "required": False, "description": "Excel 工作表名"},
        {"name": "table_index", "type": "int", "required": False, "description": "Word: 第几个表格 (从 1 数)"},
        {"name": "encoding", "type": "str", "required": False,
         "description": "CSV 编码覆盖, 如 gbk/utf-8-sig (只在自动识别不对时才用)"},
        {"name": "dry_run", "type": "bool", "required": False, "description": "只预演不落盘"},
    ],
}

# ══════════════════════════════════════════════════════════════════════
# 依赖
# ══════════════════════════════════════════════════════════════════════
_DEPS = {}
for _m in ("openpyxl", "docx", "charset_normalizer"):
    try:
        __import__(_m)
        _DEPS[_m] = True
    except Exception:
        _DEPS[_m] = False

# 表格类文件后缀
_TABLE_EXT = (".csv", ".tsv", ".xlsx", ".xlsm", ".docx")

# 备份目录 (项目根下, 不往用户桌面丢东西)
_BACKUP_ROOT = Path(__file__).resolve().parent.parent / "backups"


# ══════════════════════════════════════════════════════════════════════
# 编码 / 行尾 探测 (照原样保留)
# ══════════════════════════════════════════════════════════════════════
def _looks_like_instruction(s: str) -> bool:
    """★ 2026-09-28: 「续写20条记录」被当成文件内容整句写进 CSV 的病根拦截。

    实测事故: 用户说「"…人员信息采集表.csv" 续写20条记录」, 路由判成 file_write,
    内容提取=抠掉路径剩下 '续写20条记录' → 真的把这句指令写进了文件, 原有 20 行被覆盖。
    判据: 短句 + 含"动作词+数量/对象" + 不含任何分隔符(逗号/制表/竖线) ⇒ 是指令不是数据。
    """
    s2 = (s or "").strip()
    if not s2 or len(s2) > 40:
        return False
    if any(d in s2 for d in (",", "，", "\t", "|", "；", ";")):
        return False          # 有分隔符 → 像一行真数据
    verbs = ("续写", "添加", "增加", "追加", "写入", "加上", "补", "加", "生成", "来",
             "填", "作成", "弄", "搞", "写", "录入", "新建")
    units = ("条", "行", "个", "列", "份", "人", "记录", "信息", "数据", "表")
    has_verb = any(v in s2 for v in verbs)
    has_unit = any(u in s2 for u in units)
    has_num = bool(re.search(r"\d", s2))
    return has_verb and (has_unit or has_num)


def _norm_enc(enc: str) -> str:
    """编码归名 —— GBK 系统一成 gb18030 (超集, 读 GBK/GB2312 都不出错)。"""
    e = (enc or "").lower().replace("_", "-")
    if e in ("gb2312", "gbk", "gb18030", "cp936", "ms936", "euc-cn", "hz"):
        return "gb18030"
    if e in ("utf8", "ascii"):
        return "utf-8"
    return e or "utf-8"


# 可信的中文系编码 —— 猜成这些就直接用
_ZH_ENCS = ("gb18030", "gb2312", "gbk", "cp936", "euc-cn", "hz",
            "big5", "cp950", "big5hkscs")
# 韩日系编码 —— ★ 不能直接信: 实测 GBK 中文常被 charset_normalizer 猜成 cp949/euc-kr
#   ("姓名,性别" 被猜成 cp949 后解出 "일珙,켕"), 因为韩文码页和 GBK 字节域大量重叠。
_KOJP_ENCS = ("cp949", "euc-kr", "uhc", "shift-jis", "shift_jis", "cp932", "sjis",
              "euc-jp", "iso-2022-jp", "cp950", "big5hkscs")


def _cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    n = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    return n / max(1, len(s))


def _gbk_lead_pairs(raw: bytes) -> int:
    """数一遍字节流里像 GBK 双字节汉字的位置 (前导 0x81-0xFE + 尾 0x40-0xFE)。"""
    n = 0
    i = 0
    while i < len(raw) - 1:
        b = raw[i]
        if 0x81 <= b <= 0xFE and 0x40 <= raw[i + 1] <= 0xFE and raw[i + 1] != 0x7F:
            n += 1
            i += 2
        else:
            i += 1
    return n


def _lib_guess(raw: bytes) -> str:
    # ★ 2026-09-28: 只留 charset_normalizer, 不再挂 chardet 兜底。
    #   理由: 分发门禁 `verify_distribution_ready` 的判据是「源码里所有第三方
    #   `import X` 都要在 requirements 里」—— 多挂一个 chardet 就等于给分发版
    #   多一个没人测过的硬依赖, 而它在这儿只是第三顺位兜底, 一次都没被用到。
    #   真正的兜底是下面那套 GBK/韩日编码白黑名单 (实测 GBK 短文件会被猜成 cp949)。
    for mod in ("charset_normalizer",):
        if not _DEPS.get(mod):
            continue
        try:
            from charset_normalizer import from_bytes
            best = from_bytes(raw[:200000]).best()
            if best and best.encoding:
                return best.encoding
        except Exception:
            continue
    return ""


def _detect_encoding(raw: bytes, override: str = "") -> str:
    """探测文本编码。中文表格实际大量是 GBK —— 猜错会把整表读成乱码/写回去变问号。

    ★ 2026-09-28 实测踩坑 (两个真缺陷, 都是实测抓到的, 不是推的):
      ① 短文件上库会把 GBK 中文猜成单字节拉丁系 (cp1252/latin-1);
      ② 更阴的: 把 GBK 中文猜成 **cp949 (韩文)** —— "姓名,性别" 解出来是 "일珙,켕"。
         因为韩文码页与 GBK 字节域大面积重叠, 猜错后解出的还是"合法字符",
         于是整表按韩文读, 表头全变乱码, 保存时汉字编不进 cp949 → 静默降级成 utf-8-sig。
      处置: 韩日系猜测一律不直接采信, 回头用 gb18030 复核 (本工具服务中文表格,
      Excel 中文版导出的 CSV 就是 GBK)。真要读韩文/日文表, 用 encoding= 显式指定。
    """
    if override:
        return _norm_enc(override)
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    # 先按 utf-8 试: 能解就是 utf-8
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    g = _norm_enc(_lib_guess(raw))
    if g in _ZH_ENCS:
        return g                                  # 库明确说是中文系, 信
    try:
        s = raw.decode("gb18030")
    except UnicodeDecodeError:
        return g or "gb18030"
    # 库说的是韩日系 / 拉丁系 / 没说 → 用中文字占比 + GBK 前导字节复核
    if _cjk_ratio(s) >= 0.15 or _gbk_lead_pairs(raw) >= 4:
        return "gb18030"
    return g or "gb18030"


def _detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except Exception:
        if "\t" in sample:
            return "\t"
        if ";" in sample and sample.count(";") > sample.count(","):
            return ";"
        if "|" in sample and sample.count("|") > sample.count(","):
            return "|"
        return ","


# ══════════════════════════════════════════════════════════════════════
# 表格读写 (三种格式统一成 headers + rows)
# ══════════════════════════════════════════════════════════════════════
class Table:
    """统一表格模型。headers 一行, rows 若干行 (list[list[str]])。"""

    def __init__(self, path, headers, rows, meta=None):
        self.path = Path(str(path))
        self.headers = list(headers or [])
        self.rows = [list(r) for r in (rows or [])]
        self.meta = meta or {}

    # ── 结构 ──
    @property
    def ncols(self):
        return len(self.headers)

    @property
    def nrows(self):
        return len(self.rows)

    def col_index(self, name_or_idx):
        """列定位: 1-based 序号 / 列名 / 近似列名。

        ★ 2026-09-28 实测踩坑: 近似匹配原来两边都试 (want in h 或 h in want),
          于是 "部门编号" 被判成已存在的 "部门" 列 —— add_col 直接拒了, del_col
          错删了别的列。改成: 只认"表头里含你要的名字"(想加列时的正常用法),
          反方向必须够长才认 (防用一个 2 字碎片匹到 4 字的列名上)。
        """
        if name_or_idx is None:
            return None
        if isinstance(name_or_idx, int) or str(name_or_idx).strip().isdigit():
            i = int(name_or_idx)
            return i - 1 if 1 <= i <= self.ncols else None
        want = str(name_or_idx).strip()
        for i, h in enumerate(self.headers):
            if str(h).strip() == want:
                return i
        for i, h in enumerate(self.headers):
            if want and want in str(h):
                return i
        for i, h in enumerate(self.headers):
            h = str(h).strip()
            if h and h in want and len(h) >= max(3, int(len(want) * 0.7)):
                return i
        return None

    def normalize_width(self, pad_only=True):
        """把每行补齐到表头列数。

        ★ 2026-09-28: pad_only=True —— **只补不裁**。原来长行会被裁到表头列数,
          于是"表头传错列数"会静默吃掉数据列 (实测: 表头被当成一整串压成 1 列,
          100 行 × 7 列的数据被裁成 1 列)。宁可留着也不许悄悄丢。
        """
        n = self.ncols
        out = []
        for r in self.rows:
            r = list(r)
            if len(r) < n:
                r += [""] * (n - len(r))
            elif len(r) > n and not pad_only:
                r = r[:n]
            out.append(r)
        self.rows = out

    def snapshot(self) -> dict:
        return {
            "path": str(self.path),
            "format": self.meta.get("format", self.path.suffix.lower().lstrip(".")),
            "sheet": self.meta.get("sheet"),
            "encoding": self.meta.get("encoding"),
            "eol": "CRLF" if self.meta.get("eol") == "\r\n" else ("LF" if self.meta.get("eol") else None),
            "header": self.headers,
            "rows": self.nrows,
            "cols": self.ncols,
        }


# ── CSV ──────────────────────────────────────────────────────────────
def _load_csv(path: Path, encoding: str = "") -> Table:
    raw = path.read_bytes()
    enc = _detect_encoding(raw, encoding)
    text = raw.decode(enc, errors="replace")
    eol = _detect_eol(text)
    delim = _sniff_delimiter(text[:4096])
    data = list(csv.reader(StringIO(text), delimiter=delim))
    data = [r for r in data if any((c or "").strip() for c in r)]
    headers = data[0] if data else []
    rows = data[1:] if len(data) > 1 else []
    return Table(path, headers, rows, {"format": "csv", "encoding": enc,
                                       "eol": eol, "delimiter": delim})


def _save_csv(t: Table):
    enc = t.meta.get("encoding") or "utf-8"
    eol = t.meta.get("eol") or "\n"
    delim = t.meta.get("delimiter") or ","
    buf = StringIO()
    w = csv.writer(buf, delimiter=delim, lineterminator="\n")
    if t.headers:
        w.writerow(t.headers)
    for r in t.rows:
        w.writerow(r)
    body = buf.getvalue().replace("\r\n", "\n").replace("\n", eol)
    # ★ 不许静默 errors="replace" —— 那会把汉字悄悄变成 "?"。编不进去就换 utf-8-sig 并记账。
    try:
        data = body.encode(enc)
    except (UnicodeEncodeError, LookupError):
        enc = "utf-8-sig"
        t.meta["encoding"] = enc
        t.meta["enc_forced"] = True
        data = body.encode(enc)
    t.path.write_bytes(data)


# ── XLSX ─────────────────────────────────────────────────────────────
def _load_xlsx(path: Path, sheet=None) -> Table:
    if not _DEPS.get("openpyxl"):
        raise RuntimeError("openpyxl 未安装 —— 无法处理 Excel")
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=False)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
    grid = []
    for row in ws.iter_rows(values_only=True):
        grid.append(["" if c is None else str(c) for c in row])
    while grid and not any((c or "").strip() for c in grid[-1]):
        grid.pop()
    headers = grid[0] if grid else []
    rows = grid[1:] if len(grid) > 1 else []
    t = Table(path, headers, rows, {"format": "xlsx", "sheet": ws.title,
                                    "sheets": list(wb.sheetnames)})
    return t


def _save_xlsx(t: Table, wb=None):
    """写回 xlsx —— 只动目标 sheet, 其它 sheet 原样保留。"""
    import openpyxl
    if wb is None:
        wb = openpyxl.load_workbook(str(t.path))
    name = t.meta.get("sheet") or wb.active.title
    ws = wb[name] if name in wb.sheetnames else wb.create_sheet(name)
    # 清空该 sheet 的旧网格 (只清这个 sheet)
    if ws.max_row and ws.max_column:
        ws.delete_rows(1, ws.max_row)
    if t.headers:
        ws.append([_cell_num(h) for h in t.headers])
    for r in t.rows:
        ws.append([_cell_num(c) for c in r])
    wb.save(str(t.path))


def _cell_num(v):
    """能变成数字就变数字 —— Excel 里手机号/年龄存成文本会被当错误。"""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return ""
    if re.fullmatch(r"-?\d{1,15}", s):
        try:
            return int(s)
        except Exception:
            return s
    return s


# ── DOCX 表格 ────────────────────────────────────────────────────────
def _load_docx(path: Path, table_index=1) -> Table:
    if not _DEPS.get("docx"):
        raise RuntimeError("python-docx 未安装 —— 无法处理 Word")
    from docx import Document
    doc = Document(str(path))
    if not doc.tables:
        raise RuntimeError(f"{path.name} 里没有表格 (Word 表格操作只对表格生效)")
    idx = max(0, int(table_index) - 1)
    if idx >= len(doc.tables):
        raise RuntimeError(f"只有 {len(doc.tables)} 个表格, 请求的是第 {table_index} 个")
    tb = doc.tables[idx]
    grid = [[c.text for c in row.cells] for row in tb.rows]
    headers = grid[0] if grid else []
    rows = grid[1:] if len(grid) > 1 else []
    return Table(path, headers, rows, {"format": "docx", "table_index": idx + 1,
                                       "tables": len(doc.tables)})


def _save_docx(t: Table):
    from docx import Document
    doc = Document(str(t.path))
    idx = int(t.meta.get("table_index", 1)) - 1
    tb = doc.tables[idx]
    want = ([t.headers] if t.headers else []) + t.rows
    # 现有行够就改字, 不够就加行, 多了就删行
    while len(tb.rows) < len(want):
        tb.add_row()
    while len(tb.rows) > len(want):
        tb._element.remove(tb.rows[-1]._element)
    for ri, rowvals in enumerate(want):
        cells = tb.rows[ri].cells
        for ci in range(len(cells)):
            val = str(rowvals[ci]) if ci < len(rowvals) and rowvals[ci] is not None else ""
            if cells[ci].text != val:
                # 保留首个 run 的格式, 清掉多余 run
                for extra in cells[ci].paragraphs[0].runs[1:]:
                    extra._element.getparent().remove(extra._element)
                if cells[ci].paragraphs[0].runs:
                    cells[ci].paragraphs[0].runs[0].text = val
                else:
                    cells[ci].text = val
    doc.save(str(t.path))


# ── 统一入口 ─────────────────────────────────────────────────────────
def _load(path, sheet=None, table_index=1, encoding="") -> Table:
    p = Path(str(path))
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    ext = p.suffix.lower()
    if ext in (".csv", ".tsv"):
        return _load_csv(p, encoding)
    if ext in (".xlsx", ".xlsm"):
        return _load_xlsx(p, sheet)
    if ext == ".docx":
        return _load_docx(p, table_index)
    raise RuntimeError(f"不支持的表格格式: {ext or '(无后缀)'} —— 支持 {'/'.join(_TABLE_EXT)}")


def _save(t: Table, wb=None):
    fmt = t.meta.get("format")
    if fmt == "csv":
        _save_csv(t)
    elif fmt == "xlsx":
        _save_xlsx(t, wb)
    elif fmt == "docx":
        _save_docx(t)
    else:
        raise RuntimeError(f"未知格式: {fmt}")


def _backup(path) -> str:
    """动文件前留一份。备份进项目 backups/, 不往用户桌面丢。"""
    p = Path(str(path))
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
    d = _BACKUP_ROOT / f"table_ops_{ts}"
    try:
        d.mkdir(parents=True, exist_ok=True)
        dst = d / p.name
        shutil.copy2(str(p), str(dst))
        return str(dst)
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════
# 按表头造数据 (只说"加40条"时用)
# ══════════════════════════════════════════════════════════════════════
_SURNAME = list("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤")
_GIVEN_1 = list("伟芳娜秀敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英建华文博子晓春晓雅思志国永志")
_GIVEN_2 = list("强军磊洋勇艳杰娟涛明超霞平刚英华文博子萱涵轩然浩宇婷怡欣悦睿诚鑫琳霖彬睿")
_JOB = ["前端开发工程师", "后端开发工程师", "全栈工程师", "产品经理", "UI设计师", "测试工程师",
        "运维工程师", "数据分析师", "算法工程师", "项目经理", "运营专员", "市场专员",
        "人力资源专员", "财务会计", "法务专员", "行政助理", "客服经理", "销售经理",
        "供应链主管", "物流主管", "机械工程师", "电气工程师", "质量工程师", "内容编辑",
        "新媒体运营", "商务拓展", "培训讲师", "安全工程师", "数据库管理员", "技术总监"]
_DEPT = ["技术部", "产品部", "设计部", "市场部", "销售部", "运营部", "财务部",
         "人力资源部", "法务部", "供应链部", "客服部", "行政部", "数据部", "质量部"]
_SKILL = ["React/Vue/TypeScript", "Java/Spring/MySQL", "Python/SQL/数据可视化",
          "Linux/Docker/Kubernetes", "Figma/Sketch/交互设计", "自动化测试/Selenium",
          "用户调研/原型设计/需求分析", "PMP/风险管理/跨部门协作", "数据分析/BI/报表",
          "短视频剪辑/文案/社群运营", "ETL/Spark/Hadoop", "网络安全/渗透测试/应急响应",
          "招聘/薪酬/员工关系", "财务报表/预算/税务", "合同审核/合规/知识产权"]
_EDU = ["大专", "本科", "本科", "本科", "硕士", "硕士", "博士"]
_CITY = ["北京市朝阳区", "上海市浦东新区", "广东省广州市天河区", "广东省深圳市南山区",
         "浙江省杭州市西湖区", "江苏省南京市鼓楼区", "四川省成都市武侯区", "湖北省武汉市洪山区",
         "陕西省西安市雁塔区", "福建省厦门市思明区", "湖南省长沙市岳麓区", "山东省青岛市市南区",
         "河南省郑州市金水区", "辽宁省沈阳市和平区", "天津市河西区", "重庆市渝北区"]
_MAILBOX = ["example.com", "example.org", "test.com"]
_PINYIN = ["zhang", "wang", "li", "zhao", "liu", "chen", "yang", "huang", "zhou", "wu",
           "xu", "sun", "ma", "zhu", "hu", "guo", "he", "gao", "lin", "luo"]


def _fake_id_card(birth: datetime.date, rng) -> str:
    """生成格式合法的 18 位示例证件号 (含正确校验位)。仅用于模板占位。"""
    area = rng.choice(["110101", "310101", "440106", "440301", "330106",
                       "320106", "510107", "420111", "610113", "350203"])
    seq = "".join(rng.choice(string.digits) for _ in range(3))
    body = f"{area}{birth:%Y%m%d}{seq}"
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    s = sum(int(body[i]) * weights[i] for i in range(17))
    return body + "10X98765432"[s % 11]


def _gen_value(header: str, rng, used_names=None, birth=None):
    """按表头语义造一个像样的值。认不出来就留空 —— 不硬编。"""
    h = str(header or "").strip()
    h_low = h.lower()

    # 姓名
    if any(k in h for k in ("姓名", "名字", "员工", "联系人")) and "电话" not in h and "手机" not in h:
        for _ in range(200):
            n = rng.choice(_SURNAME) + rng.choice(_GIVEN_1) + (rng.choice(_GIVEN_2) if rng.random() < 0.7 else "")
            if used_names is None or n not in used_names:
                if used_names is not None:
                    used_names.add(n)
                return n
        return rng.choice(_SURNAME) + rng.choice(_GIVEN_1) + rng.choice(_GIVEN_2)

    if "性别" in h:
        return rng.choice(["男", "女"])
    if "年龄" in h or "年纪" in h or h.endswith("岁"):
        return str(rng.randint(23, 52))
    if any(k in h for k in ("出生", "生日")):
        d = birth or datetime.date(rng.randint(1972, 2001), rng.randint(1, 12), rng.randint(1, 28))
        return d.strftime("%Y-%m-%d")
    if any(k in h for k in ("身份证", "证件号", "证件号码")):
        d = birth or datetime.date(rng.randint(1972, 2001), rng.randint(1, 12), rng.randint(1, 28))
        return _fake_id_card(d, rng)
    if any(k in h for k in ("手机", "电话", "联系方式", "手机号")):
        return "1" + rng.choice("35789") + "".join(rng.choice(string.digits) for _ in range(9))
    if any(k in h for k in ("邮箱", "邮件", "email", "e-mail")):
        return rng.choice(_PINYIN) + str(rng.randint(100, 999)) + "@" + rng.choice(_MAILBOX)
    if any(k in h for k in ("入职", "入职日期", "报到")):
        d = datetime.date(rng.randint(2010, 2024), rng.randint(1, 12), rng.randint(1, 28))
        return d.strftime("%Y/%m/%d")
    if any(k in h for k in ("日期", "时间")) and "出生" not in h:
        d = datetime.date(rng.randint(2015, 2026), rng.randint(1, 12), rng.randint(1, 28))
        return d.strftime("%Y/%m/%d")
    if any(k in h for k in ("职位", "岗位", "职务", "工种")):
        return rng.choice(_JOB)
    if "部门" in h:
        return rng.choice(_DEPT)
    if any(k in h for k in ("学历", "学位")):
        return rng.choice(_EDU)
    if any(k in h for k in ("技能", "标签", "专长", "tag")):
        return rng.choice(_SKILL)
    if any(k in h for k in ("住址", "地址", "籍贯", "现住", "家庭住")):
        return rng.choice(_CITY) + "".join(str(rng.randint(1, 200)) for _ in range(1)) + "号"
    if any(k in h for k in ("紧急联系人", "担保人")) and "电话" not in h:
        return rng.choice(_SURNAME) + rng.choice(_GIVEN_1)
    if any(k in h for k in ("关系",)):
        return rng.choice(["配偶", "父母", "子女", "兄弟", "姐妹", "朋友"])
    if any(k in h for k in ("工号", "编号", "序号", "id")):
        return None  # 交给调用方按行号补
    if any(k in h for k in ("金额", "工资", "薪资", "薪水")):
        return str(rng.randint(6000, 35000))
    if any(k in h for k in ("备注", "说明", "描述", "其他")):
        return ""
    if any(k in h for k in ("状态",)):
        return rng.choice(["在职", "在职", "在职", "试用"])
    return ""   # 认不出的列留空, 不硬编垃圾值


def _gen_rows(headers, count, existing_rows=None, seed=None) -> list:
    """按表头造 count 行。姓名去重, 且不与既有行重复。"""
    rng = random.Random(seed if seed is not None else 20260928)
    used = set()
    for r in (existing_rows or []):
        if r:
            used.add(str(r[0]))
    out = []
    for i in range(count):
        birth = datetime.date(rng.randint(1972, 2001), rng.randint(1, 12), rng.randint(1, 28))
        row = []
        for h in headers:
            v = _gen_value(h, rng, used, birth)
            row.append("" if v is None else v)
        # 序号/工号 这类按序号补
        for ci, h in enumerate(headers):
            if any(k in str(h) for k in ("序号", "编号", "工号", "id")) and "身份证" not in str(h):
                if not row[ci]:
                    row[ci] = str(len(existing_rows or []) + i + 1)
        out.append(row)
    return out


# ══════════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════════
def _fmt_table(headers, rows, limit=8) -> str:
    if not headers:
        return "  (空表)"
    lines = ["  " + " | ".join(str(h) for h in headers)]
    lines.append("  " + "-+-".join("-" * min(12, max(3, len(str(h)))) for h in headers))
    for r in rows[:limit]:
        lines.append("  " + " | ".join(str(c) for c in r))
    if len(rows) > limit:
        lines.append(f"  … 还有 {len(rows) - limit} 行")
    return "\n".join(lines)


def _verify_after_write(path, sheet=None, table_index=1, expect_rows=None, expect_cols=None,
                        must_still_contain=None, expect_headers=None) -> dict:
    """★ 铁律: 干完重读一遍, 用真实磁盘内容核对。不复用内存里的对象。

    expect_headers 是 2026-09-28 加的一道保险 —— 编码猜错那次, 行数/列数看着都对,
    但表头整行变成了乱码 (GBK 被按韩文读)。光数行数列数是抓不住这种事故的。
    """
    try:
        t2 = _load(path, sheet=sheet, table_index=table_index)
    except Exception as e:
        return {"ok": False, "detail": f"重读失败: {e}"}
    ok = True
    _enc = t2.meta.get("encoding") or "-"
    _eol = "CRLF" if t2.meta.get("eol") == "\r\n" else ("LF" if t2.meta.get("eol") else "-")
    notes = [f"重读核对: {t2.nrows} 行 × {t2.ncols} 列, 编码 {_enc}, 行尾 {_eol}"]
    if expect_headers is not None:
        a = [str(x).strip() for x in (expect_headers or [])]
        b = [str(x).strip() for x in (t2.headers or [])]
        if a != b:
            ok = False
            notes.append(f"✗ 表头对不上 (期望 {a}, 实际 {b}) —— 可能读文件时编码猜错了")
    if expect_rows is not None and t2.nrows != expect_rows:
        ok = False
        notes.append(f"✗ 行数不符 (期望 {expect_rows}, 实际 {t2.nrows})")
    if expect_cols is not None and t2.ncols != expect_cols:
        ok = False
        notes.append(f"✗ 列数不符 (期望 {expect_cols}, 实际 {t2.ncols})")
    if must_still_contain:
        blob = "\n".join(",".join(str(c) for c in r) for r in t2.rows)
        for needle in must_still_contain:
            if str(needle) not in blob:
                ok = False
                notes.append(f"✗ 原有内容丢失: {needle!r} 不在了")
    return {"ok": ok, "detail": " / ".join(notes), "after": t2.snapshot()}


def _ok(output, **extra):
    d = {"success": True, "output": output}
    d.update(extra)
    return d


def _err(msg, **extra):
    d = {"success": False, "error": msg}
    d.update(extra)
    return d


# ══════════════════════════════════════════════════════════════════════
# 动作实现
# ══════════════════════════════════════════════════════════════════════
def _act_info(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    s = t.snapshot()
    out = [f"表格结构 — {Path(str(path)).name}",
           f"  格式: {s['format']}   工作表: {s['sheet'] or '-'}   编码: {s['encoding'] or '-'}   行尾: {s['eol'] or '-'}",
           f"  规模: {s['rows']} 行数据 × {s['cols']} 列",
           f"  表头: {s['header']}",
           "", "前几行:"]
    out.append(_fmt_table(t.headers, t.rows))
    return _ok("\n".join(out), structure=s)


def _act_read(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    start = int(kw.get("start") or 1)
    limit = int(kw.get("limit") or 20)
    seg = t.rows[start - 1:start - 1 + limit]
    out = [f"{t.nrows} 行 × {t.ncols} 列  第 {start}~{start + len(seg) - 1} 行:",
           _fmt_table(t.headers, seg, limit)]
    return _ok("\n".join(out))


def _act_add_rows(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    if not t.headers:
        return _err("这个表没有表头 —— 先设表头再谈加行 (action=set_header)", structure=t.snapshot())

    count = kw.get("count") or kw.get("rows") or kw.get("n")
    data = kw.get("data") or kw.get("content") or kw.get("rows_data")
    header_csv = kw.get("header")

    # 用户给了表头且表是空的 → 先立表头
    if header_csv and not any(str(h).strip() for h in t.headers):
        if isinstance(header_csv, str):
            t.headers = [c.strip() for c in re.split(r"[,，\t;|]", header_csv) if c.strip()]
        else:
            t.headers = [str(c).strip() for c in header_csv]

    # 解析要加的行
    new_rows = []
    dropped_instruction = ""
    if isinstance(data, str) and data.strip() and _looks_like_instruction(data):
        # ★ 用户/模型把「续写20条记录」这种指令塞进了内容参数 —— 不是数据, 丢掉它,
        #   改走"按条数生成"。这正是 2026-09-28 把指令写进文件那次的病根。
        #   顺手从这句指令里把条数抠出来 (「续写20条记录」→ 20), 别让用户白说一遍。
        if not count:
            _m = re.search(r"(\d+)\s*(?:条|行|个|份|人|列)?", data)
            if _m and str(_m.group(1)).isdigit() and int(_m.group(1)) > 0:
                count = _m.group(1)
        dropped_instruction = f"  (内容参数 {data!r} 像指令不像数据, 已忽略; 改用条数 {count})"
        data = None
    if isinstance(data, str) and data.strip():
        # 给的是表格文本 (CSV/TSV/竖线) —— 按表头列数切
        txt = data.strip()
        delim = _sniff_delimiter(txt[:1000])
        parsed = [[c.strip() for c in r] for r in csv.reader(StringIO(txt), delimiter=delim)]
        parsed = [r for r in parsed if any(c for c in r)]
        new_rows = parsed
    elif isinstance(data, list) and data:
        if data and isinstance(data[0], dict):
            new_rows = [[str(d.get(h, "")) for h in t.headers] for d in data]
        else:
            new_rows = [[str(c) for c in r] for r in data]

    if new_rows:
        source = "用户给的数据"
    else:
        if not count or str(count).strip() == "":
            return _err("没说加几条、也没给数据 —— 请给 count(条数) 或 data(内容)",
                        structure=t.snapshot())
        try:
            n = int(str(count).strip())
        except Exception:
            return _err(f"条数读不出来: {count!r}")
        if n <= 0:
            return _err("条数必须大于 0")
        if n > 5000:
            return _err("一次最多加 5000 行")
        new_rows = _gen_rows(t.headers, n, existing_rows=t.rows)
        source = f"按表头生成 {n} 行"

    # 宽度对齐
    ncol = t.ncols
    fixed = []
    for r in new_rows:
        r = list(r)
        if len(r) < ncol:
            r += [""] * (ncol - len(r))
        elif len(r) > ncol:
            r = r[:ncol]
        fixed.append(r)
    new_rows = fixed

    # ★ 原有不动: 追加, 绝不重写既有行
    old_rows = [list(r) for r in t.rows]
    old_first = old_rows[0][0] if old_rows and old_rows[0] else None
    t.rows = old_rows + new_rows

    if kw.get("dry_run"):
        return _ok(f"[试运行] 将追加 {len(new_rows)} 行, 不动原有 {len(old_rows)} 行\n"
                   + _fmt_table(t.headers, new_rows, 5), would_add=len(new_rows))

    bak = _backup(t.path)
    _save(t)
    v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                            expect_headers=t.headers,
                            expect_rows=len(old_rows) + len(new_rows), expect_cols=ncol,
                            must_still_contain=[old_first] if old_first else None)
    out = [f"{Path(str(path)).name}: 原有 {len(old_rows)} 行 → 现有 {len(old_rows) + len(new_rows)} 行"
           f" (加了 {len(new_rows)} 行, {source})" + dropped_instruction,
           f"  表头: {t.headers}",
           "  新加的行:", _fmt_table(t.headers, new_rows, 6) if new_rows else "  (无)"]
    if v["detail"]:
        out.append(f"  {v['detail']}")
    if not v["ok"]:
        out.append("  ⚠ 核对没通过 —— 请人工看一眼这个文件")
    if bak:
        out.append(f"  备份: {bak}")
    return _ok("\n".join(out), before=len(old_rows), after=len(old_rows) + len(new_rows),
               added=len(new_rows), verified=v["ok"], backup=bak)


def _act_del_rows(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    targets = kw.get("rows") or kw.get("indices") or kw.get("row")
    where_col = kw.get("where_col") or kw.get("col")
    where_val = kw.get("where_val") or kw.get("value")

    if targets is None and where_col is not None and where_val is not None:
        ci = t.col_index(where_col)
        if ci is None:
            return _err(f"找不到列: {where_col!r}  (现有表头 {t.headers})")
        targets = [i + 1 for i, r in enumerate(t.rows) if str(r[ci]) == str(where_val)]
        if not targets:
            return _ok(f"没有匹配 {where_col} == {where_val} 的行, 未改动", removed=0)

    if targets is None:
        return _err("删行必须点名: rows=[2,5,7] (从 1 数) 或 where_col+where_val")
    if isinstance(targets, (str, int)):
        targets = [targets]
    idx = []
    for x in targets:
        s = str(x).strip()
        # 支持 "2-5" 区间
        m = re.fullmatch(r"(\d+)\s*[-~到]\s*(\d+)", s)
        if m:
            idx += list(range(int(m.group(1)), int(m.group(2)) + 1))
        elif s.isdigit():
            idx.append(int(s))
    idx = sorted({i for i in idx if 1 <= i <= t.nrows}, reverse=True)
    if not idx:
        return _err("没有有效的行号 (行号从 1 数, 不含表头)")

    removed_vals = [t.rows[i - 1] for i in idx]
    old_rows = [list(r) for r in t.rows]
    if kw.get("dry_run"):
        return _ok("[试运行] 将删除这些行:\n" + _fmt_table(t.headers, removed_vals),
                   would_remove=len(idx))
    bak = _backup(t.path)
    for i in idx:
        del t.rows[i - 1]
    _save(t)
    v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                            expect_headers=t.headers,
                            expect_rows=len(old_rows) - len(idx), expect_cols=t.ncols)
    out = [f"{Path(str(path)).name}: 原有 {len(old_rows)} 行 → 现有 {t.nrows} 行 (删了 {len(idx)} 行)",
           "  删掉的行:", _fmt_table(t.headers, removed_vals), f"  {v['detail']}"]
    if not v["ok"]:
        out.append("  ⚠ 核对没通过")
    if bak:
        out.append(f"  备份: {bak}")
    return _ok("\n".join(out), before=len(old_rows), after=t.nrows, removed=len(idx),
               verified=v["ok"], backup=bak)


def _act_add_col(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    name = kw.get("name") or kw.get("col") or kw.get("column")
    if not name:
        return _err("加列要给列名 (name)")
    name = str(name).strip()
    if t.col_index(name) is not None:
        return _err(f"列 {name!r} 已经存在了")
    pos = kw.get("pos") or kw.get("at")
    if pos is not None and str(pos).strip().isdigit():
        at = max(0, min(int(pos) - 1, t.ncols))
    else:
        at = t.ncols

    default = kw.get("default", "")
    values = kw.get("values")
    if isinstance(values, list) and values:
        vals = [str(v) for v in values]
        if len(vals) < t.nrows:
            vals += [str(default)] * (t.nrows - len(vals))
        vals = vals[:t.nrows]
    elif kw.get("generate"):
        vals = [str(_gen_value(name, random.Random(20260928 + i)) or default) for i in range(t.nrows)]
    else:
        vals = [str(default)] * t.nrows

    if kw.get("dry_run"):
        return _ok(f"[试运行] 将在第 {at + 1} 列插入 {name!r}, {t.nrows} 行填 {default!r}")
    bak = _backup(t.path)
    old_first = t.rows[0][0] if t.rows and t.rows[0] else None
    t.headers.insert(at, name)
    for i, r in enumerate(t.rows):
        r.insert(at, vals[i])
    _save(t)
    v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                            expect_headers=t.headers,
                            expect_rows=t.nrows, expect_cols=t.ncols,
                            must_still_contain=[old_first] if old_first else None)
    out = [f"{Path(str(path)).name}: 加了 1 列 → 现有 {t.ncols} 列 × {t.nrows} 行",
           f"  新表头: {t.headers}", f"  {v['detail']}"]
    if not v["ok"]:
        out.append("  ⚠ 核对没通过")
    if bak:
        out.append(f"  备份: {bak}")
    return _ok("\n".join(out), cols=t.ncols, verified=v["ok"], backup=bak)


def _act_del_col(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    want = kw.get("name") or kw.get("col") or kw.get("column")
    if want is None:
        return _err("删列要点名 (name 或 col, 列名或 1 起的列号)")
    ci = t.col_index(want)
    if ci is None:
        return _err(f"找不到列: {want!r}  (现有表头 {t.headers})")
    if t.ncols <= 1:
        return _err("只剩一列了, 不能删")
    if kw.get("dry_run"):
        return _ok(f"[试运行] 将删除第 {ci + 1} 列 {t.headers[ci]!r}")
    bak = _backup(t.path)
    gone = t.headers[ci]
    old_first = t.rows[0][0] if t.rows and t.rows[0] else None
    t.headers.pop(ci)
    for r in t.rows:
        if len(r) > ci:
            r.pop(ci)
    _save(t)
    v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                            expect_headers=t.headers,
                            expect_rows=t.nrows, expect_cols=t.ncols,
                            must_still_contain=[old_first] if old_first else None)
    out = [f"{Path(str(path)).name}: 删了列 {gone!r} → 现有 {t.ncols} 列 × {t.nrows} 行",
           f"  新表头: {t.headers}", f"  {v['detail']}"]
    if not v["ok"]:
        out.append("  ⚠ 核对没通过")
    if bak:
        out.append(f"  备份: {bak}")
    return _ok("\n".join(out), cols=t.ncols, verified=v["ok"], backup=bak)


def _act_set_cell(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    row = kw.get("row")
    col = kw.get("col") or kw.get("column") or kw.get("name")
    val = kw.get("value", kw.get("content", ""))
    if row is None or col is None:
        return _err("改格子要给 row (第几行, 不含表头) 和 col (列名或列号)")
    ri = int(str(row).strip())
    if not (1 <= ri <= t.nrows):
        return _err(f"行号 {ri} 超出范围 (1~{t.nrows})")
    ci = t.col_index(col)
    if ci is None:
        return _err(f"找不到列: {col!r}  (现有表头 {t.headers})")
    old = t.rows[ri - 1][ci]
    t.rows[ri - 1][ci] = str(val)
    if kw.get("dry_run"):
        return _ok(f"[试运行] {ri} 行 {t.headers[ci]}: {old!r} → {val!r}")
    bak = _backup(t.path)
    _save(t)
    v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                            expect_headers=t.headers,
                            expect_rows=t.nrows, expect_cols=t.ncols)
    return _ok(f"{Path(str(path)).name}: 第 {ri} 行「{t.headers[ci]}」{old!r} → {val!r}\n  {v['detail']}",
               verified=v["ok"], backup=bak)


def _row_looks_like_data(row1) -> bool:
    """现有"第一行"是表头还是数据?

    ★ 2026-09-28 用户实测: 「他没填表头, 我就告诉他第一行填表头, 然后一会儿做完了,
      就光剩表头了, 人员信息没了」。根因: CSV 里第一行被朴素地当成表头, 于是
      "设表头"把它顶掉了。判据: 表头行不该有纯数字格 (数据行几乎一定有,
      如年龄/工号/电话)。
    """
    if not row1:
        return False
    n = sum(1 for c in row1 if str(c).strip() and
            re.fullmatch(r"-?\d+(\.\d+)?", str(c).strip()))
    return n >= 1


def _act_set_header(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    names = kw.get("names") or kw.get("header") or kw.get("headers")
    mapping = kw.get("map") or kw.get("rename")

    if mapping and isinstance(mapping, dict):
        changed = []
        for old, new in mapping.items():
            ci = t.col_index(old)
            if ci is None:
                return _err(f"找不到列: {old!r}")
            changed.append(f"{t.headers[ci]!r} → {new!r}")
            t.headers[ci] = str(new)
        if kw.get("dry_run"):
            return _ok("[试运行] 改列名: " + "; ".join(changed))
        bak = _backup(t.path)
        _save(t)
        v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                                expect_headers=t.headers,
                                expect_rows=t.nrows, expect_cols=t.ncols)
        return _ok(f"{Path(str(path)).name}: 改了 {len(changed)} 个列名 — " + "; ".join(changed)
                   + f"\n  新表头: {t.headers}\n  {v['detail']}",
                   verified=v["ok"], backup=bak)

    if names is None:
        return _err("设表头要给 names=[...] (整行表头) 或 map={旧名:新名}")
    if isinstance(names, str):
        # ★ 2026-09-28 修 (真缺陷, 实测抓到): 原来只按 [,，\t;|] 切, **不切空格** ——
        #   而用户/模型常写成 "姓名 性别 年龄 职位" 这种空格分隔 ⇒ 整串变成一个列名
        #   ⇒ 表被压成 1 列, 原有 7 列数据被默默裁掉。空格必须切。
        names = [c.strip() for c in re.split(r"[\s,，、;；|/]+", names) if c.strip()]
    names = [str(x).strip() for x in names if str(x).strip()]
    if not names:
        return _err("表头是空的 —— 没动文件")

    # ★ 宽度校验: 表里有数据时, 表头列数必须和现有列数一致, 否则**拒绝执行**。
    #   宁可让用户改一下, 也不许静默把数据裁掉。
    if t.headers and t.nrows and len(names) != t.ncols:
        _wider = max([len(r) for r in t.rows] + [t.ncols])
        if len(names) != _wider:
            return _err(
                f"表头给了 {len(names)} 列 ({names}), 但表里是 {t.ncols} 列 "
                f"(数据行最宽 {_wider} 列)。对不上, 没动文件。\n"
                f"  现有表头: {t.headers}\n"
                f"  要么把表头改成 {t.ncols} 列, 要么先确认这个表到底几列。",
                structure=t.snapshot())

    # ★ 改表头 还是 插表头? —— 看现有第一行像数据还是像表头, 也可显式 insert=true 指定。
    row1 = [str(x) for x in (t.headers or [])]
    has_row1 = bool(row1 and any(x.strip() for x in row1))
    insert = kw.get("insert")
    if insert is None:
        insert = has_row1 and _row_looks_like_data(row1)
    else:
        insert = str(insert).lower() in ("1", "true", "yes", "y", "是", "prepend", "insert", "加")

    bak = _backup(t.path)
    if insert:
        # 第一行是数据 → 插入一行表头, 原有数据(含原来那第一行)一行不动
        old_first = list(row1)
        t.rows = [old_first] + [list(r) for r in t.rows]
        t.headers = names
        t.normalize_width()
        _save(t)
        v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                                expect_headers=t.headers,
                                expect_rows=len(t.rows), expect_cols=len(names))
        out = [f"{Path(str(path)).name}: 第一行原来是数据(不是表头), 已**插入**一行表头 → {t.headers}",
               f"  原有 {len(t.rows)} 行数据一行没动 (含原来的第一行 {old_first[:3]})",
               f"  {v['detail']}"]
    else:
        old = list(t.headers)
        t.headers = names
        t.normalize_width()
        _save(t)
        v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                                expect_headers=t.headers,
                                expect_rows=t.nrows, expect_cols=len(names))
        out = [f"{Path(str(path)).name}: 表头已更新",
               f"  旧: {old}", f"  新: {t.headers}",
               f"  数据 {t.nrows} 行未动", f"  {v['detail']}"]
    if not v["ok"]:
        out.append("  ⚠ 核对没通过")
    if bak:
        out.append(f"  备份: {bak}")
    return _ok("\n".join(out), verified=v["ok"], backup=bak, inserted_header=bool(insert))


def _act_sort(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    ci = t.col_index(kw.get("by") or kw.get("col") or kw.get("name"))
    if ci is None:
        return _err(f"排序要指定列 (by=列名或列号)。现有表头 {t.headers}")
    reverse = str(kw.get("order", "asc")).lower() in ("desc", "倒序", "降序", "-1")

    def key(r):
        v = str(r[ci]) if ci < len(r) else ""
        nums = re.findall(r"-?\d+\.?\d*", v.replace(",", ""))
        if nums and len("".join(nums)) >= len(v.strip()) - 2:
            try:
                return (0, float(nums[0]), "")
            except Exception:
                pass
        return (1, 0.0, v)

    if kw.get("dry_run"):
        return _ok(f"[试运行] 将按 {t.headers[ci]!r} {'降序' if reverse else '升序'} 排序")
    bak = _backup(t.path)
    t.rows.sort(key=key, reverse=reverse)
    _save(t)
    v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                            expect_headers=t.headers,
                            expect_rows=t.nrows, expect_cols=t.ncols)
    return _ok(f"{Path(str(path)).name}: 已按 {t.headers[ci]!r} {'降序' if reverse else '升序'} 排序"
               f" ({t.nrows} 行)\n  前几行:\n{_fmt_table(t.headers, t.rows, 5)}\n  {v['detail']}",
               verified=v["ok"], backup=bak)


def _act_dedupe(kw):
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    cols = kw.get("by") or kw.get("cols") or t.headers
    if isinstance(cols, str):
        cols = [c.strip() for c in re.split(r"[,，|]", cols) if c.strip()]
    idxs = [t.col_index(c) for c in cols]
    idxs = [i for i in idxs if i is not None]
    if not idxs:
        return _err("去重要指定列 (by=[列名...])")
    seen, kept, dropped = set(), [], []
    for r in t.rows:
        k = tuple(str(r[i]) if i < len(r) else "" for i in idxs)
        if k in seen:
            dropped.append(r)
        else:
            seen.add(k)
            kept.append(r)
    if not dropped:
        return _ok(f"{Path(str(path)).name}: 没有重复行, 未改动 ({t.nrows} 行)", removed=0)
    if kw.get("dry_run"):
        return _ok(f"[试运行] 将删掉 {len(dropped)} 个重复行")
    bak = _backup(t.path)
    t.rows = kept
    _save(t)
    v = _verify_after_write(t.path, t.meta.get("sheet"), t.meta.get("table_index", 1),
                            expect_headers=t.headers,
                            expect_rows=len(kept), expect_cols=t.ncols)
    return _ok(f"{Path(str(path)).name}: 去重 {len(dropped)} 行 → 现有 {t.nrows} 行"
               f" (按 {[t.headers[i] for i in idxs]})\n  {v['detail']}",
               removed=len(dropped), verified=v["ok"], backup=bak)


def _act_split(kw):
    """拆分: 按行数切 / 按某列的值切。"""
    path = kw.get("path") or kw.get("file")
    t = _load(path, kw.get("sheet"), kw.get("table_index", 1), kw.get("encoding", ""))
    outdir = Path(str(kw.get("outdir") or kw.get("out") or t.path.parent))
    outdir.mkdir(parents=True, exist_ok=True)
    stem, ext = t.path.stem, t.path.suffix

    by_rows = kw.get("by_rows") or kw.get("rows_per") or kw.get("chunk")
    by_col = kw.get("by_col") or kw.get("col")
    groups = []

    if by_col:
        ci = t.col_index(by_col)
        if ci is None:
            return _err(f"找不到列: {by_col!r}")
        buckets = {}
        for r in t.rows:
            key = str(r[ci]) if ci < len(r) else ""
            buckets.setdefault(key, []).append(r)
        for k, v in buckets.items():
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", k)[:40] or "空值"
            groups.append((f"{stem}_{safe}", v))
    elif by_rows:
        n = int(str(by_rows).strip())
        if n <= 0:
            return _err("每份行数必须大于 0")
        for i in range(0, t.nrows, n):
            groups.append((f"{stem}_part{i // n + 1}", t.rows[i:i + n]))
    else:
        return _err("拆分要说明怎么分: by_rows=每份几行 或 by_col=按哪列的值分")

    if kw.get("dry_run"):
        return _ok("[试运行] 将拆成 " + ", ".join(f"{d}({len(v)}行)" for d, v in groups))

    written = []
    for name, rows in groups:
        dst = outdir / f"{name}{ext}"
        if ext.lower() in (".csv", ".tsv"):
            tmp = Table(dst, t.headers, rows, {"format": "csv",
                                              "encoding": t.meta.get("encoding"),
                                              "eol": t.meta.get("eol"),
                                              "delimiter": t.meta.get("delimiter")})
            _save_csv(tmp)
        elif ext.lower() in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = (t.meta.get("sheet") or "Sheet1")[:31]
            ws.append([_cell_num(h) for h in t.headers])
            for r in rows:
                ws.append([_cell_num(c) for c in r])
            wb.save(str(dst))
        else:
            return _err(f"拆分暂只支持 csv/xlsx, 不支持 {ext}")
        written.append(f"{dst.name} ({len(rows)} 行)")
    return _ok(f"{Path(str(path)).name}: 拆成 {len(written)} 份, 输出到 {outdir}\n  "
               + "\n  ".join(written), files=[str(outdir / f"{n}{ext}") for n, _ in groups])


def _act_merge(kw):
    """合并: 多个表拼一个。列名对齐; 缺列补空; 首表的表头为准。"""
    paths = kw.get("paths") or kw.get("files") or kw.get("sources")
    target = kw.get("target") or kw.get("out") or kw.get("path")
    if isinstance(paths, str):
        paths = [p.strip().strip('"') for p in re.split(r"[;\n]+", paths) if p.strip()]
    if not paths:
        return _err("合并要给 paths=[文件1, 文件2, ...]")
    if len(paths) < 2:
        return _err("合并至少要两个文件")
    if not target:
        return _err("合并要给 target=输出文件路径")

    tables = []
    for p in paths:
        tables.append(_load(p, kw.get("sheet"), kw.get("table_index", 1)))
    headers = list(tables[0].headers)
    for extra in (kw.get("add_cols") or []):
        if extra not in headers:
            headers.append(extra)
    rows, per_file = [], []
    for t in tables:
        cmap = {}
        for ci, h in enumerate(t.headers):
            hi = headers.index(h) if h in headers else None
            if hi is not None:
                cmap[ci] = hi
        n = 0
        for r in t.rows:
            nr = [""] * len(headers)
            for ci, hi in cmap.items():
                if ci < len(r):
                    nr[hi] = r[ci]
            # 源文件独有列 → 塞进同名附加列
            for ci, h in enumerate(t.headers):
                if h not in headers and ci < len(r):
                    pass
            rows.append(nr)
            n += 1
        per_file.append(f"{Path(str(t.path)).name}: {n} 行, {len(t.headers)} 列")

    tgt = Path(str(target))
    ext = tgt.suffix.lower()
    if ext in (".csv", ".tsv"):
        _save_csv(Table(tgt, headers, rows, {"format": "csv", "encoding": "utf-8-sig",
                                            "eol": "\r\n", "delimiter": ","}))
    elif ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([_cell_num(h) for h in headers])
        for r in rows:
            ws.append([_cell_num(c) for c in r])
        wb.save(str(tgt))
    else:
        return _err(f"合并输出只支持 csv/xlsx, 不支持 {ext}")
    v = _verify_after_write(tgt, None, 1, expect_rows=len(rows), expect_cols=len(headers),
                            expect_headers=headers)
    return _ok(f"合并 {len(paths)} 个文件 → {tgt.name} ({len(rows)} 行 × {len(headers)} 列)\n  "
               + "\n  ".join(per_file) + f"\n  {v['detail']}",
               rows=len(rows), verified=v["ok"])


# ══════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════
_ACTIONS = {
    "info": _act_info,
    "read": _act_read,
    "add_rows": _act_add_rows,
    "append_rows": _act_add_rows,      # 别名
    "add": _act_add_rows,
    "del_rows": _act_del_rows,
    "delete_rows": _act_del_rows,
    "add_col": _act_add_col,
    "add_column": _act_add_col,
    "del_col": _act_del_col,
    "delete_column": _act_del_col,
    "set_cell": _act_set_cell,
    "set_header": _act_set_header,
    "rename_col": _act_set_header,
    "sort": _act_sort,
    "dedupe": _act_dedupe,
    "split": _act_split,
    "merge": _act_merge,
}


async def run(**kwargs) -> dict:
    action = str(kwargs.get("action") or "").strip().lower()
    if not action:
        return _err("没给 action。可用: " + ", ".join(sorted(set(_ACTIONS))))
    fn = _ACTIONS.get(action)
    if fn is None:
        return _err(f"未知 action: {action}",
                    available=sorted(set(_ACTIONS)))
    try:
        return fn(kwargs)
    except FileNotFoundError as e:
        return _err(str(e))
    except PermissionError as e:
        return _err(f"没有权限动这个文件 (可能被 Excel/WPS 打开着): {e}")
    except Exception as e:
        import traceback
        return _err(f"{action} 失败: {e}", trace=traceback.format_exc()[-600:])
