"""
TigerOffice — 办公大插件
=======================
熔合: Excel读写 | Word提取/合并 | PDF合并/提取 | 实体卡片导入导出
PPT预留(python-pptx未装)

所有操作遵循 mary3 插件规范: PLUGIN dict + async run(**kwargs) -> {success, output}
"""
import os
import re
import base64
from io import BytesIO

PLUGIN = {
    "name": "tiger_office",
    "description": "办公全家桶: Excel读写/双向卡片, Word提取合并, PDF合并提取, PPT预留",
    "version": "1.0",
    "requires": ["openpyxl"],
    "trigger": ["Excel", "excel", "表格", "新建表格", "建表格", "xlsx",
                "Word", "word", "文档", "PDF", "pdf",
                "台账", "导入卡片", "导出卡片", "实体卡片", "人员通讯录",
                "合并文档", "提取文字", "提取表格", "办公"],
    "permission": ["file_read", "file_write"],
    "category": "office",
}

# ── 依赖检测 ──
_DEPS = {}
for _mod, _pkg in [("openpyxl", "openpyxl"), ("docx", "python-docx"),
                    ("PyPDF2", "PyPDF2"), ("pdfplumber", "pdfplumber"),
                    # ★ 2026-09-20 加: 机器上装的是 pypdf 6.x (不是 PyPDF2, 也不是 pdfplumber)
                    #   → 原来"提取 PDF"技能直接报 "pdfplumber or PyPDF2 required",
                    #   明明有库却用不了。pypdf 是 PyPDF2 的官方续作 (同名 API 家族)。
                    ("pypdf", "pypdf"),
                    ("pptx", "python-pptx")]:
    try:
        __import__(_mod)
        _DEPS[_mod] = True
    except ImportError:
        _DEPS[_mod] = False


async def run(**kwargs) -> dict:
    """统一入口。action 决定走哪个分支。"""
    action = kwargs.get("action", "")

    handlers = {
        "write_excel": _write_excel,
        "scan_excel": _scan_excel,
        "extract_column": _extract_column,
        "merge_base64": _merge_base64,
        "format_excel": _format_excel,
        "import_cards": _import_entity_cards,
        "export_cards": _export_entity_cards,
        "extract_word": _extract_word_text,
        "merge_word": _merge_word_docs,
        "write_word": _write_word,
        "write_ppt": _write_ppt,
        "merge_pdf": _merge_pdf,
        "extract_pdf_text": _extract_pdf_text,
        "extract_pdf_table": _extract_pdf_table,
        "file_sort": _auto_sort_files,
        "batch_rename": _batch_rename,
        "analyze_table": _analyze_table,
    }

    if action not in handlers:
        return {"success": False, "error": f"Unknown action: {action}",
                "available": list(handlers.keys())}

    try:
        return handlers[action](kwargs)
    except Exception as e:
        return {"success": False, "error": f"{action} failed: {e}"}


# ═══════════════════════════════════════
# Excel 操作
# ═══════════════════════════════════════

def _scan_excel(kwargs) -> dict:
    """扫描目录下所有 Excel 文件"""
    if not _DEPS.get("openpyxl"):
        return {"success": False, "error": "openpyxl not installed"}
    folder = kwargs.get("folder", kwargs.get("path", "."))
    keyword = kwargs.get("keyword", "")
    if not folder or folder == ".":
        folder = "."
    if not os.path.isdir(folder):
        return {"success": False, "error": f"Not a directory: {folder}"}

    results = []
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.endswith(".xlsx") and not f.startswith('~$') and (not keyword or keyword in f):
                results.append(os.path.join(root, f))

    return {"success": True, "output": results, "count": len(results)}


def _extract_column(kwargs) -> dict:
    """提取 Excel 指定列数据 (列索引从1开始)"""
    if not _DEPS.get("openpyxl"):
        return {"success": False, "error": "openpyxl not installed"}
    import openpyxl

    path = kwargs.get("path", "").strip()
    raw_col = kwargs.get("col", kwargs.get("col_index", 2))
    try:
        col = int(raw_col) if raw_col else 2
    except (ValueError, TypeError):
        col = 2
    sheet_name = kwargs.get("sheet", None)

    if not path:
        return {"success": False, "error": "请指定Excel文件路径，如: 提取列 D:/数据.xlsx 2"}
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    data = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if len(row) >= col:
            data.append(row[col - 1])
    wb.close()
    return {"success": True, "output": data, "count": len(data)}


def _merge_base64(kwargs) -> dict:
    """将多组数据合并为 base64 台账字符串"""
    parts = kwargs.get("parts", [])
    labels = kwargs.get("labels", [])
    lines = []
    for i, part in enumerate(parts):
        label = labels[i] if i < len(labels) else f"数据{i+1}"
        lines.append(f"【{label}】{part}")
    content = chr(10).join(lines)
    b64 = base64.b64encode(content.encode("utf-8")).decode()
    return {"success": True, "output": b64, "length": len(b64)}


def _format_excel(kwargs) -> dict:
    """Excel 标准化排版：表头加粗居中"""
    if not _DEPS.get("openpyxl"):
        return {"success": False, "error": "openpyxl not installed"}
    import openpyxl

    path = kwargs.get("path", "").strip()
    if not path:
        return {"success": False, "error": "请指定Excel文件路径，如: 格式化表格 D:/台账.xlsx"}
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    wb = openpyxl.load_workbook(path)
    ws = wb.active
    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True)
        cell.alignment = openpyxl.styles.Alignment(horizontal="center")
    wb.save(path)
    wb.close()
    return {"success": True, "output": f"标准化完成: {os.path.basename(path)}"}


# ═══════════════════════════════════════
# 实体卡片导入/导出（Excel <-> 人物实体）
# ═══════════════════════════════════════

# 标准卡片字段映射: Excel列名 -> 实体存储key
CARD_FIELDS = {
    chr(22995)+chr(21517): "name",
    chr(30005)+chr(35805): "phone",
    chr(37038)+chr(31665): "email",
    chr(20303)+chr(22336): "address",
    chr(29233)+chr(22909): "hobby",
    chr(20844)+chr(21496): "company",
    chr(24494)+chr(20449): "wechat",
    chr(29983)+chr(26085): "birthday",
    chr(36523)+chr(20221)+chr(35777)+chr(21495): "idcard",
    "openid": "openid",
}
CARD_HEADERS = list(CARD_FIELDS.keys())
CARD_KEYS = list(CARD_FIELDS.values())  # name,phone,email,address,hobby,company,wechat,birthday,idcard,openid


def _write_excel(kwargs) -> dict:
    """新建 Excel 表格 (★ 2026-09-19 深测补齐的能力缺口)。

    深测发现: 办公全家桶只有 scan/format/extract/import/export, **没有"新建表格"** ——
    用户说"新建一个 Excel 记录本月销量"时, 工具返回 `Unknown action: write_excel`,
    Agent 只能回"我做不到"。补上。

    参数:
      path       输出位置 (可以是**目录** → 自动补文件名; 缺省 → 桌面)
      headers    表头 (list[str])
      rows       数据行 (list[list] 或 list[dict])
      content    markdown 表格 / CSV / TSV 文本 (无 headers/rows 时从这里解析)
      sheet      工作表名 (缺省 "Sheet1")
      title      写进 A1 上方的标题行 (可选)

    排版与 `_format_excel` 同一口径: 表头加粗 + 居中。
    """
    if not _DEPS.get("openpyxl"):
        return {"success": False, "error": "openpyxl not installed"}
    import openpyxl
    from openpyxl.styles import Font, Alignment

    headers = kwargs.get("headers") or []
    rows = kwargs.get("rows") or []
    content = kwargs.get("content") or kwargs.get("text") or ""
    sheet = str(kwargs.get("sheet") or "Sheet1")
    title = str(kwargs.get("title") or "").strip()

    # ── content → headers/rows (markdown 表 / CSV / TSV 都认) ──
    if (not headers or not rows) and isinstance(content, str) and content.strip():
        parsed = []
        for line in content.splitlines():
            s = line.strip()
            if not s or re.fullmatch(r"[|\-:\s]+", s):
                continue                      # 空行 / markdown 分隔行
            if s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
            elif "\t" in s:
                cells = [c.strip() for c in s.split("\t")]
            elif "," in s:
                cells = [c.strip() for c in s.split(",")]
            else:
                continue                      # 无分隔符的说明文字: 跳过, 不硬塞
            parsed.append(cells)
        if parsed and not headers:
            headers, rows = parsed[0], parsed[1:]
        elif parsed and headers and not rows:
            rows = parsed

    if not headers and isinstance(rows, list) and rows and isinstance(rows[0], dict):
        headers = list(rows[0].keys())
    if not headers and not rows:
        return {"success": False, "output": "",
                "error": "没有内容可写: 请给 headers+rows, 或 content(表格文本)"}

    # ── 输出位置 (目录 → 补文件名, 与 _resolve_out_path 同口径) ──
    if not title:
        title = str(kwargs.get("name") or "").strip() or sheet
    path = _resolve_out_path(kwargs.get("path"), title, ".xlsx")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet[:31] or "Sheet1"

    r0 = 1
    if kwargs.get("title"):
        ws.cell(row=1, column=1, value=str(kwargs["title"]))
        ws.cell(row=1, column=1).font = Font(bold=True, size=14)
        r0 = 2

    if headers:
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=r0, column=c, value=h)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        r0 += 1

    n = 0
    for row in (rows if isinstance(rows, list) else []):
        if isinstance(row, dict):
            vals = [row.get(h, "") for h in headers]
        elif isinstance(row, (list, tuple)):
            vals = list(row)
        else:
            vals = [row]
        for c, v in enumerate(vals, start=1):
            ws.cell(row=r0, column=c, value=v)
        r0 += 1
        n += 1

    # 列宽自适应 (中文按 2 个字符宽度算)
    for c, h in enumerate((headers or []), start=1):
        wid = 4
        vals = [str(h)] + [str(r.get(h, "")) if isinstance(r, dict) else (
            str(r[c - 1]) if isinstance(r, (list, tuple)) and len(r) >= c else "")
            for r in (rows if isinstance(rows, list) else [])]
        for v in vals:
            wid = max(wid, sum(2 if ord(ch) > 127 else 1 for ch in v) + 2)
        ws.column_dimensions[openpyxl.utils.get_column_letter(c)].width = min(wid, 42)

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        wb.save(path)
    except Exception as e:
        return {"success": False, "output": "", "error": f"保存失败: {e}"}
    finally:
        wb.close()
    return {"success": True, "path": path,
            "output": f"已新建表格: {path} ({n} 行 × {len(headers)} 列)"}


def _import_entity_cards(kwargs) -> dict:
    """从 Excel 读取人员通讯录，返回标准卡片列表"""
    if not _DEPS.get("openpyxl"):
        return {"success": False, "error": "openpyxl not installed"}
    import openpyxl

    path = kwargs.get("path", "").strip()
    if not path:
        return {"success": False, "error": "请指定Excel文件路径，如: 导入人员卡片 D:/通讯录.xlsx"}
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    headers = [str(c.value).strip() if c.value else "" for c in ws[1]]
    col_map = {}
    for idx, h in enumerate(headers):
        if h in CARD_FIELDS:
            col_map[idx] = CARD_FIELDS[h]

    if not col_map:
        wb.close()
        hdr_str = ", ".join(CARD_HEADERS)
        return {"success": False, "error": f"表头不匹配，需要: {hdr_str}"}

    cards = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        card = {key: "" for key in CARD_KEYS}
        for col_idx, field_key in col_map.items():
            val = row[col_idx] if col_idx < len(row) else ""
            card[field_key] = str(val).strip() if val else ""
        if card.get("name"):
            cards.append(card)

    wb.close()
    return {"success": True, "output": cards, "count": len(cards),
            "fields": CARD_HEADERS}


def _export_entity_cards(kwargs) -> dict:
    """将实体卡片列表导出为 Excel 文件"""
    if not _DEPS.get("openpyxl"):
        return {"success": False, "error": "openpyxl not installed"}
    import openpyxl

    entities = kwargs.get("entities", kwargs.get("cards", []))
    save_path = kwargs.get("path", kwargs.get("save_path", "entities_export.xlsx"))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(CARD_HEADERS)

    for ent in entities:
        if isinstance(ent, dict):
            row = [ent.get(key, ent.get(cn, "")) for cn, key in zip(CARD_HEADERS, CARD_KEYS)]
        else:
            row = [str(ent)] + [""] * (len(CARD_HEADERS) - 1)
        ws.append(row)

    wb.save(save_path)
    wb.close()
    return {"success": True, "output": f"已导出 {len(entities)} 条到 {save_path}",
            "path": save_path, "count": len(entities)}


# ═══════════════════════════════════════
# Word 操作
# ═══════════════════════════════════════

def _extract_word_text(kwargs) -> dict:
    """提取 Word 文档全部文本"""
    if not _DEPS.get("docx"):
        return {"success": False, "error": "python-docx not installed"}
    from docx import Document

    path = kwargs.get("path", "").strip()
    if not path:
        return {"success": False, "error": "请指定Word文件路径，如: 提取Word D:/报告.docx"}
    if not path.lower().endswith('.docx'):
        return {"success": False, "error": f"不是Word文档(.docx): {path}"}
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    doc = Document(path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    full_text = chr(10).join(paragraphs)
    return {"success": True, "output": full_text, "paragraph_count": len(paragraphs)}


# ═══════════════════════════════════════
# Word 生成 (2026-09-18 新增)
# ═══════════════════════════════════════

def _set_cjk_font(doc, font_name="微软雅黑"):
    """把默认字体(含 eastAsia)设成中文字体 —— 否则 Word 打开中文可能走默认宋体/乱码。

    python-docx 只设 font.name 管不到中文, 必须写 w:eastAsia。这是实测踩过的坑。
    """
    from docx.oxml.ns import qn
    try:
        st = doc.styles["Normal"]
        st.font.name = font_name
        rpr = st.element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rf)
        rf.set(qn("w:eastAsia"), font_name)
        rf.set(qn("w:ascii"), font_name)
        rf.set(qn("w:hAnsi"), font_name)
    except Exception:
        pass


def _default_out_path(title: str, ext: str) -> str:
    """没给输出路径时: 桌面 + 标题(或时间戳)。与 file_ops 的"裸名落桌面"约定一致。

    文件名里的非法字符要洗掉 (Windows: \\ / : * ? " < > |)。
    """
    import datetime
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", (title or "").strip())[:40]
    if not name:
        name = "文档_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(os.path.expanduser("~"), "Desktop", name + ext)


def _resolve_out_path(raw: str, title: str, ext: str) -> str:
    """把用户给的位置解析成**具体文件路径**。

    ★ 2026-09-19 深测修: 原来只做 "给路径就用 / 没给就落桌面 + 缺扩展名就补",
      于是用户给**目录**时 ("存到 D:/报告/") 会被当成文件补成 "D:/报告/.docx" 之类,
      技能层干脆把这种句子抽成空参数 → 静默落桌面 (用户指定的位置被无视)。

    规则 (加性, 不改变既有输入的行为):
      ① 空 → 桌面 + 标题/时间戳 (与 file_ops 裸名落桌面约定一致)
      ② 已存在的目录, 或以分隔符结尾 → 该目录 + 标题(或时间戳) + 扩展名
      ③ 其余 → 原样; 缺扩展名就补 (旧行为)
    """
    raw = (raw or "").strip().strip('"\'“”')
    if not raw:
        return _default_out_path(title, ext)
    _sep = ("/", "\\")
    if raw.endswith(_sep) or os.path.isdir(raw):
        _name = re.sub(r'[\\/:*?"<>|\s]+', "_", (title or "").strip())[:40]
        if not _name:
            import datetime
            _name = "文档_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(raw, _name + ext)
    if not raw.lower().endswith(ext):
        return raw + ext
    return raw


def _write_word(kwargs) -> dict:
    """生成 Word 文档。content 用 markdown 风格: '# 标题' / '- 要点' / 普通段落。

    参数: path(可选, 缺省→桌面), title, content, sections([{heading, body}])
    """
    if not _DEPS.get("docx"):
        return {"success": False, "error": "python-docx not installed"}
    from docx import Document

    title = (kwargs.get("title") or "").strip()
    content = kwargs.get("content") or ""
    sections = kwargs.get("sections") or []
    if not (title or content or sections):
        return {"success": False, "error": "没有内容可写: 请给 title / content / sections"}
    # 没给 title → 从 content 的第一个 '# ' 抽 (技能就不用单独提取标题了)
    # 抽走后**从正文里删掉那行**, 否则标题会出现两次 (实测: [0]周报 [1]周报)
    if not title and content:
        _m = re.search(r"^\s*#\s+(.+?)\s*$", str(content), re.M)
        if _m:
            title = _m.group(1).strip()
            content = (str(content)[:_m.start()] + str(content)[_m.end():]).lstrip("\n")

    path = _resolve_out_path(kwargs.get("path"), title, ".docx")

    doc = Document()
    _set_cjk_font(doc)

    if title:
        doc.add_heading(title, level=0)

    def _emit_body(text: str):
        """把 markdown 风格文本铺进文档"""
        for raw in str(text).split("\n"):
            s = raw.rstrip()
            if not s.strip():
                continue
            t = s.strip()
            if t.startswith("### "):
                doc.add_heading(t[4:], level=3)
            elif t.startswith("## "):
                doc.add_heading(t[3:], level=2)
            elif t.startswith("# "):
                doc.add_heading(t[2:], level=1)
            elif t.startswith(("- ", "* ", "• ")):
                doc.add_paragraph(t[2:], style="List Bullet")
            elif re.match(r"^\d+[.、)]\s+", t):
                doc.add_paragraph(re.sub(r"^\d+[.、)]\s+", "", t), style="List Number")
            else:
                doc.add_paragraph(t)

    if sections and isinstance(sections, list):
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            h = (sec.get("heading") or "").strip()
            if h:
                doc.add_heading(h, level=1)
            if sec.get("body"):
                _emit_body(sec["body"])
    if content:
        _emit_body(content)

    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return {"success": False, "error": f"目录建不了: {out_dir} ({e})"}

    try:
        doc.save(path)
    except Exception as e:
        return {"success": False, "error": f"保存失败: {e}"}

    paras = len([p for p in doc.paragraphs if p.text.strip()])
    return {"success": True, "output": f"已生成 Word: {path} ({paras} 段)",
            "path": path, "paragraph_count": paras}


# ═══════════════════════════════════════
# PPT 生成 (2026-09-18 新增)
# ═══════════════════════════════════════

def _write_ppt(kwargs) -> dict:
    """生成 PPT 演示文稿。

    参数: path(可选, 缺省→桌面), title, subtitle, slides([{title, bullets:[...], body}]),
          content(markdown 回退: '## 页标题' 开新页, '- 要点' 加项目符号)
    """
    if not _DEPS.get("pptx"):
        return {"success": False, "error": "python-pptx not installed"}
    from pptx import Presentation

    title = (kwargs.get("title") or "").strip()
    subtitle = (kwargs.get("subtitle") or "").strip()
    slides = kwargs.get("slides") or []
    content = kwargs.get("content") or ""

    # markdown 回退解析: '## ' 开新页, '- ' 要点, 其他当正文
    if not slides and content:
        slides = []
        cur = None
        for raw in str(content).split("\n"):
            t = raw.strip()
            if not t:
                continue
            if t.startswith("## "):
                cur = {"title": t[3:], "bullets": []}
                slides.append(cur)
            elif t.startswith("# "):
                if not title:
                    title = t[2:]
                else:
                    cur = {"title": t[2:], "bullets": []}
                    slides.append(cur)
            elif t.startswith(("- ", "* ", "• ")):
                if cur is None:
                    cur = {"title": "", "bullets": []}
                    slides.append(cur)
                cur["bullets"].append(t[2:])
            else:
                if cur is None:
                    cur = {"title": "", "bullets": []}
                    slides.append(cur)
                cur.setdefault("bullets", []).append(t)

    if not (title or slides):
        return {"success": False, "error": "没有内容可做: 请给 title / slides / content"}

    path = _resolve_out_path(kwargs.get("path"), title, ".pptx")

    prs = Presentation()

    # 封面
    cover = prs.slides.add_slide(prs.slide_layouts[0])
    if title:
        cover.shapes.title.text = title
    if subtitle and len(cover.placeholders) > 1:
        cover.placeholders[1].text = subtitle

    made = 0
    for s in (slides if isinstance(slides, list) else []):
        if not isinstance(s, dict):
            continue
        st = (s.get("title") or "").strip()
        bullets = [str(b).strip() for b in (s.get("bullets") or []) if str(b).strip()]
        body = (s.get("body") or "").strip()
        if not (st or bullets or body):
            continue
        sl = prs.slides.add_slide(prs.slide_layouts[1])
        if st:
            sl.shapes.title.text = st
        tf = sl.placeholders[1].text_frame
        tf.clear()
        first = True
        for b in bullets:
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            p.text = b
            p.level = 0
            first = False
        if body:
            for line in body.split("\n"):
                if not line.strip():
                    continue
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                p.text = line.strip()
                first = False
        made += 1

    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return {"success": False, "error": f"目录建不了: {out_dir} ({e})"}

    try:
        prs.save(path)
    except Exception as e:
        return {"success": False, "error": f"保存失败: {e}"}

    return {"success": True, "output": f"已生成 PPT: {path} (封面 + {made} 页)",
            "path": path, "slide_count": made + 1}


def _merge_word_docs(kwargs) -> dict:
    """合并多个 Word 文档为一个"""
    if not _DEPS.get("docx"):
        return {"success": False, "error": "python-docx not installed"}
    from docx import Document

    folder = kwargs.get("folder", kwargs.get("path", "."))
    save_name = kwargs.get("save_name", "merged_document.docx")

    if not folder or folder == ".":
        folder = "."
    if not folder or folder == ".":
        folder = "."
    if not os.path.isdir(folder):
        return {"success": False, "error": f"Not a directory: {folder}"}

    new_doc = Document()
    merged = []
    for f in sorted(os.listdir(folder)):
        if f.endswith(".docx"):
            fp = os.path.join(folder, f)
            try:
                doc = Document(fp)
                hdr = "===== " + f + " ====="
                new_doc.add_paragraph(hdr)
                for para in doc.paragraphs:
                    new_doc.add_paragraph(para.text)
                merged.append(f)
            except Exception as e:
                return {"success": False, "error": f"Failed on {f}: {e}"}

    save_path = os.path.join(folder, save_name)
    new_doc.save(save_path)
    return {"success": True, "output": f"合并 {len(merged)} 个文档 -> {save_path}",
            "files": merged, "path": save_path}


# ═══════════════════════════════════════
# PDF 操作
# ═══════════════════════════════════════

def _merge_pdf(kwargs) -> dict:
    """合并多个 PDF"""
    if not _DEPS.get("PyPDF2"):
        return {"success": False, "error": "PyPDF2 not installed"}

    paths = kwargs.get("paths", kwargs.get("files", []))
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",")]
    save_path = kwargs.get("save_path", kwargs.get("output", "merged.pdf"))

    if not paths or (len(paths) == 1 and not paths[0]):
        return {"success": False, "error": "请指定要合并的PDF文件，如: 合并PDF D:/a.pdf D:/b.pdf"}
    if len(paths) < 2:
        return {"success": False, "error": "至少需要2个PDF文件才能合并"}

    from PyPDF2 import PdfWriter, PdfReader
    writer = PdfWriter()
    for p in paths:
        if not os.path.isfile(p):
            return {"success": False, "error": f"File not found: {p}"}
        reader = PdfReader(p)
        for page in reader.pages:
            writer.add_page(page)

    with open(save_path, "wb") as f:
        writer.write(f)
    return {"success": True, "output": f"合并 {len(paths)} 个PDF -> {save_path}",
            "page_count": len(writer.pages)}


def _extract_pdf_text(kwargs) -> dict:
    """提取 PDF 全部文字（优先 pdfplumber，回退 PyPDF2）"""
    path = kwargs.get("path", "").strip()
    if not path:
        return {"success": False, "error": "请指定PDF文件路径，如: 提取PDF D:/报告.pdf"}
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    if _DEPS.get("pdfplumber"):
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages_text = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages_text.append(t)
        return {"success": True, "output": chr(10).join(pages_text),
                "page_count": len(pages_text)}

    elif _DEPS.get("PyPDF2") or _DEPS.get("pypdf"):
        if _DEPS.get("PyPDF2"):
            from PyPDF2 import PdfReader
        else:
            from pypdf import PdfReader
        reader = PdfReader(path)
        pages_text = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages_text.append(t)
        return {"success": True, "output": chr(10).join(pages_text),
                "page_count": len(pages_text)}

    return {"success": False, "error": "pdfplumber or PyPDF2 required"}


def _extract_pdf_table(kwargs) -> dict:
    """提取 PDF 表格数据（需要 pdfplumber）"""
    if not _DEPS.get("pdfplumber"):
        return {"success": False, "error": "pdfplumber not installed"}

    path = kwargs.get("path", "").strip()
    if not path:
        return {"success": False, "error": "请指定PDF文件路径，如: 提取PDF表格 D:/报表.pdf"}
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    import pdfplumber
    tables = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_tables = page.extract_tables()
            for t in page_tables:
                if t:
                    tables.append({"page": i + 1, "rows": len(t), "data": t})

    return {"success": True, "output": tables, "table_count": len(tables)}
# ═══════════════════════════════════════
# 表格解析
# ═══════════════════════════════════════

def _analyze_table(kwargs) -> dict:
    path = kwargs.get("path", "").strip()
    if not path or not os.path.isfile(path):
        return {"success": False, "error": "请指定Excel文件路径"}
    if not path.endswith('.xlsx'):
        return {"success": False, "error": "仅支持.xlsx格式"}

    import openpyxl
    from collections import Counter

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    # Find header row (skip summary rows starting with 总条数/合计/序号)
    header_row = 1
    headers = []
    for r in range(1, min(6, ws.max_row + 1)):
        row_vals = [str(c.value)[:50] if c.value else "" for c in ws[r]]
        first_val = row_vals[0].strip() if row_vals else ""
        if first_val and not any(first_val.startswith(k) for k in ['总条数', '合计', '序号', '总计']):
            headers = row_vals
            header_row = r
            break

    data_start = header_row + 1
    total_rows = 0
    empty_rows = 0

    # Count and sample
    samples = []
    for row in ws.iter_rows(min_row=data_start, values_only=True):
        vals = [v for v in row if v is not None and str(v).strip()]
        if not vals:
            empty_rows += 1
            continue
        total_rows += 1
        if len(samples) < 3:
            samples.append([str(v)[:40] if v else "" for v in row[:len(headers)]])

    # Column analysis
    cols_info = []
    for i, h in enumerate(headers):
        if not h:
            continue
        vals = []
        for row in ws.iter_rows(min_row=data_start, max_row=data_start+min(total_rows, 1000),
                                min_col=i+1, max_col=i+1, values_only=True):
            v = row[0]
            if v is not None and str(v).strip():
                vals.append(str(v).strip())
        if vals:
            counter = Counter(vals)
            top = counter.most_common(5)
            cols_info.append({
                "col": i + 1,
                "name": h,
                "unique": len(counter),
                "top5": [[v, c] for v, c in top],
                "sample": vals[0][:50] if vals else "",
            })

    wb.close()

    # Build report
    report = []
    report.append(f"文件: {os.path.basename(path)}")
    report.append(f"Sheet: {ws.title}")
    report.append(f"数据行: {total_rows}, 空行: {empty_rows}, 总列: {len(headers)}")
    report.append(f"表头行: 第{header_row}行")
    report.append(f"有效列: {len(cols_info)}")
    report.append("")

    for ci in cols_info:
        report.append(f"列{ci['col']}: {ci['name']}")
        report.append(f"  种类: {ci['unique']}, 样例: {ci['sample']}")
        if ci['unique'] <= 20:
            tops = [f"{v}({c})" for v, c in ci['top5']]
            report.append(f"  分布: {', '.join(tops)}")

    output = chr(10).join(report)
    return {"success": True, "output": output[:8000], "columns": len(cols_info), "rows": total_rows}

# ═══════════════════════════════════════
# 文件归档操作
# ═══════════════════════════════════════

def _auto_sort_files(kwargs) -> dict:
    folder = kwargs.get("folder", kwargs.get("path", "."))
    if not folder or not os.path.isdir(folder):
        return {"success": False, "error": f"Not a directory: {folder}"}

    # Safety: refuse to sort project directories
    project_markers = {'.git', '.crypto.key', 'package.json', 'Cargo.toml', 'pyproject.toml',
                       'requirements.txt', 'Dockerfile', 'CMakeLists.txt', 'setup.py'}
    top_items = set(os.listdir(folder))
    py_count = sum(1 for f in top_items if f.endswith('.py'))
    if project_markers & top_items or py_count > 3:
        return {"success": False, "error": f"拒绝归档项目目录(含代码工程文件): {folder}"}

    type_dirs = {
        ".xlsx": "Excel表格", ".xls": "Excel表格",
        ".docx": "Word文档", ".doc": "Word文档",
        ".pdf": "PDF报表",
        ".png": "截图图片", ".jpg": "截图图片", ".jpeg": "截图图片", ".bmp": "截图图片",
        ".txt": "文本文件", ".md": "文本文件", ".py": "代码文件", ".js": "代码文件",
    }
    created_dirs = set()
    moved = 0

    for f in os.listdir(folder):
        fp = os.path.join(folder, f)
        if os.path.isdir(fp):
            continue
        ext = os.path.splitext(f)[1].lower()
        target_dir_name = type_dirs.get(ext, "其他文件")
        target_dir = os.path.join(folder, target_dir_name)
        if target_dir_name not in created_dirs:
            os.makedirs(target_dir, exist_ok=True)
            created_dirs.add(target_dir_name)
        try:
            import shutil
            shutil.move(fp, os.path.join(target_dir, f))
            moved += 1
        except Exception:
            pass

    return {"success": True, "output": f"归档完成: {moved}个文件分入{len(created_dirs)}个分类文件夹",
            "moved": moved, "categories": list(created_dirs)}


def _batch_rename(kwargs) -> dict:
    folder = kwargs.get("folder", kwargs.get("path", "."))
    prefix = kwargs.get("prefix", "")

    if not folder or not os.path.isdir(folder):
        return {"success": False, "error": f"Not a directory: {folder}"}

    from datetime import datetime
    date_tag = datetime.now().strftime("%Y%m%d")
    name_prefix = prefix if prefix else date_tag

    renamed = 0
    files = sorted([f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))])
    for idx, f in enumerate(files):
        old = os.path.join(folder, f)
        ext = os.path.splitext(f)[1]
        new_name = f"{name_prefix}_{idx+1:03d}{ext}"
        new = os.path.join(folder, new_name)
        try:
            os.rename(old, new)
            renamed += 1
        except Exception:
            pass

    return {"success": True, "output": f"重命名完成: {renamed}个文件 -> {name_prefix}_001~{renamed:03d}",
            "renamed": renamed, "pattern": f"{name_prefix}_NNN"}

