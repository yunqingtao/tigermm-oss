"""Knowledge Engine — entity management, skill matching, environment paths.
Three-layer matching: entity learn/query → skill parse → execute via plugin gateway.
"""
import json
import re
import time
import logging
from pathlib import Path

logger = logging.getLogger("mary3.knowledge")


class KnowledgeEngine:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.entities = {}
        self.environment = {}
        self.skills = {}
        self._load()

    def _load(self):
        for fname in ['entities.json', 'environment.json', 'skills.json']:
            fp = self.data_dir / fname
            if fp.exists():
                try:
                    data = json.loads(fp.read_text(encoding='utf-8'))
                    if fname == 'entities.json':
                        self.entities = data
                    elif fname == 'environment.json':
                        self.environment = data
                    elif fname == 'skills.json':
                        self.skills = data

                except Exception as e:
                    logger.warning("Failed to load %s: %s", fname, e)

        # Also load per-tool skill files from tools/ (override monolithic skills.json)
        import os as _os_ke
        tools_dir = _os_ke.path.join(_os_ke.path.dirname(_os_ke.path.dirname(_os_ke.path.abspath(__file__))), 'tools')
        if _os_ke.path.isdir(tools_dir):
            for fname in _os_ke.listdir(tools_dir):
                if fname.endswith('.skills.json'):
                    fp = _os_ke.path.join(tools_dir, fname)
                    try:
                        data = json.loads(open(fp, 'r', encoding='utf-8').read())
                        self.skills.update(data)
                    except Exception:
                        pass

    def _save_entities(self):
        fp = self.data_dir / 'entities.json'
        try:
            fp.write_text(json.dumps(self.entities, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            logger.warning("Failed to save entities: %s", e)

    # ── Entity management ──

    def _guess_topic(self, message: str) -> str:
        """Guess message topic to scope learned rules."""
        topics = {
            '彩票': ['双色球','大乐透','彩票','红球','蓝球','号码'],
            '数学': ['计算','公式','算法','斐波那契','排序','遍历'],
            '文件': ['文件','目录','桌面','新建','写入','读取'],
        }
        for topic, kws in topics.items():
            if any(kw in message for kw in kws):
                return topic
        return '通用'

    def learn_entity(self, message, user=None):
        """Learn entity from message. Patterns:
        '记住：张三 邮箱 zhangsan@test.com 电话 139xxx'
        '张三邮箱是zhangsan@test.com'
        '张三 电话 138xxx'
        """
        # Pattern 1: 记住：Name field value field value
        m = re.search(r'记住[：:]\s*(\S+)\s+(.+)', message)
        if not m:
            # Pattern 2: Name + field + 是/为 + value
            m = re.search(r'(\S{1,4})(邮箱|email|电话|phone|地址|address|城市|city)[是为：:]\s*(\S+)', message)
            if m:
                name = m.group(1)
                field = m.group(2)
                value = m.group(3)
                return self._set_entity_field(name, field, value)

            # Pattern 3: 删除/忘记 Name
            m = re.search(r'(?:删除|忘记|删掉|去掉)\s*(\S{1,4})', message)
            if m:
                name = m.group(1)
                return self._delete_entity(name)

            return {'success': False, 'response': ''}

        name = m.group(1)
        rest = m.group(2)

        # Parse field:value pairs
        fields = {}
        field_map = {'邮箱': 'email', 'email': 'email', '电话': 'phone', 'phone': 'phone',
                     '地址': 'address', 'address': 'address', '城市': 'city', 'city': 'city'}
        parts = re.findall(r'(\S+?)\s+(\S+)', rest)
        for k, v in parts:
            fk = field_map.get(k, k)
            fields[fk] = v.strip().rstrip('.,;!?，。；！？')

        if not fields:
            return {'success': False, 'response': ''}

        # Create or update entity
        if name not in self.entities:
            self.entities[name] = {'type': 'person', 'name': name, 'aliases': []}
        
        entity = self.entities[name]
        response_parts = []
        for fk, fv in fields.items():
            entity[fk] = fv
            fk_cn = {v: k for k, v in field_map.items()}.get(fk, fk)
            response_parts.append(f"{fk_cn} {fv}")

        self._save_entities()
        return {'success': True, 'response': f"OK {name}的{' '.join(response_parts)}"}

    def import_cards_from_list(self, cards):
        """Batch import entity cards from Excel (tiger_office)"""
        imported = 0
        updated = 0
        for card in cards:
            name = card.get('name', '').strip()
            if not name:
                continue
            is_new = name not in self.entities
            if is_new:
                self.entities[name] = {'type': 'person', 'name': name, 'aliases': []}
            entity = self.entities[name]
            for key, val in card.items():
                if key == 'name' or not val:
                    continue
                entity[key] = str(val).strip()
            if is_new:
                imported += 1
            else:
                updated += 1
        self._save_entities()
        return {'success': True, 'imported': imported, 'updated': updated,
                'total': len(cards)}

    @staticmethod
    def _render_card(name, ent):
        """黑金出品 · 信息卡片"""
        import re
        def vlen(s):
            clean = re.sub(r"\033\[.*?m", "", s)
            w = 0
            for c in clean:
                w += 2 if ord(c) > 127 else 1
            return w

        G = "\033[38;2;255;172;2m"
        B = "\033[48;2;23;13;2m"
        D = "\033[38;2;140;140;140m"
        R = "\033[0m"
        O = "\033[1m"
        cn = {'email':'邮箱','phone':'电话','address':'住址','city':'城市',
              'company':'公司','title':'职位','hobby':'爱好','note':'备注',
              'birthday':'生日','wechat':'微信','qq':'QQ'}
        skip = {'type','name','aliases','is_self'}
        
        items = [(cn.get(k,k), str(v)) for k,v in ent.items() if k not in skip and v]
        aliases = " · ".join(ent.get('aliases', []))
        
        # 预计算：标签最大宽度(中文×2)，值最大宽度
        L = max((vlen(l) for l,_ in items), default=4)  # label col
        V = max((vlen(v) for _,v in items), default=10)  # value col
        inner = L + V + 5  # ▸ + 空格 + 分隔
        # W = inner content width (excludes border chars)
        W = max(inner, vlen(name) + 2, 24) + 1
        
        def bar(left, mid, right, w):
            return G + left + mid * w + right + R
        def side(content):
            """一行：║content║  — content可视宽=W"""
            vis = vlen(content)
            pad = W - vis
            if pad < 0:
                pad = 0
            return G + "║" + R + content + " " * pad + G + "║" + R
        
        lines = [bar("╔", "═", "╗", W)]
        
        # 标题 — 居中
        t_vis = vlen(name)
        lpad = (W - t_vis) // 2
        rpad = W - t_vis - lpad
        lines.append(side(B + " " * lpad + O + name + R + B + " " * rpad + R))
        lines.append(bar("╠", "═", "╣", W))
        
        # 字段 — 标签等宽
        for label, value in items:
            pad_label = " " * (L - vlen(label))
            lines.append(side(f" {G}▸{R} {G}{label}{R}{pad_label}  {D}{value}{R}"))
        
        if aliases:
            lines.append(side(f" {D}别名{R}  {aliases}"))
        
        lines.append(bar("╚", "═", "╝", W))
        return "\n".join(lines)

    def _set_entity_field(self, name, field_cn, value):
        field_map = {'邮箱': 'email', 'email': 'email', '电话': 'phone', 'phone': 'phone',
                     '地址': 'address', '城市': 'city', '爱好': 'hobby', 'hobby': 'hobby',
                     '公司': 'company', '职位': 'title', '生日': 'birthday',
                     '微信': 'wechat', '备注': 'note'}
        fk = field_map.get(field_cn, field_cn)
        if name not in self.entities:
            self.entities[name] = {'type': 'person', 'name': name, 'aliases': []}
        self.entities[name][fk] = value
        self._save_entities()
        return {'success': True, 'response': f"OK {name}的{field_cn}已更新为 {value}"}

    def _delete_entity(self, name):
        if name in self.entities:
            del self.entities[name]
            self._save_entities()
            return {'success': True, 'response': f"已删除 {name}"}
        return {'success': False, 'response': f"未找到 {name}"}

    def query_entity(self, message, user=None):
        """Query entity info. '老王的邮箱' / '涛哥是谁' / '虎哥电话'
        严谨规范：问什么答什么，中文键名，独立信息反馈。
        """
        # 字段映射：用户说的中文 → 存储的英文key → 显示的中文名
        FIELD_MAP = {
            '邮箱': ('email', '邮箱'), 'email': ('email', '邮箱'),
            '电话': ('phone', '电话'), 'phone': ('phone', '电话'),
            '手机': ('phone', '电话'),
            '地址': ('address', '住址'), 'address': ('address', '住址'),
            '住址': ('address', '住址'),
            '城市': ('city', '城市'), 'city': ('city', '城市'),
            '爱好': ('hobby', '爱好'), 'hobby': ('hobby', '爱好'),
            '公司': ('company', '公司'), 'company': ('company', '公司'),
            '职位': ('title', '职位'), 'title': ('title', '职位'),
            '生日': ('birthday', '生日'), 'birthday': ('birthday', '生日'),
            '微信': ('wechat', '微信'), 'wechat': ('wechat', '微信'),
            '备注': ('note', '备注'), 'note': ('note', '备注'),
        }
        
        # 判断用户在问哪个字段（精确）
        asked_field = None
        for cn_key, (en_key, _) in FIELD_MAP.items():
            if cn_key in message:
                asked_field = en_key
                break
        
        # 找实体
        for name in self.entities:
            if name in message:
                ent = self.entities[name]
                # 问指定字段 → 只回答这个字段
                # Try English key first, then Chinese
                actual_key = asked_field
                if asked_field and asked_field not in ent:
                    # Reverse lookup: find Chinese key that maps to this English field
                    for cn_k, (en_k, _) in FIELD_MAP.items():
                        if en_k == asked_field and cn_k in ent:
                            actual_key = cn_k
                            break
                if asked_field and (asked_field in ent or actual_key in ent):
                    display = FIELD_MAP.get(asked_field, (asked_field, asked_field))[0]
                    # 找中文显示名
                    for cn, (en, disp) in FIELD_MAP.items():
                        if en == asked_field and cn in message:
                            display = cn
                            break
                    return {'found': True,
                            'response': f"{name} {display}：{ent.get(actual_key, ent.get(asked_field, ''))}"}
                # 问"是谁" → 信息卡片
                # 黑金出品 · 信息卡片
                return {'found': True, 'response': self._render_card(name, ent)}
        
        # Alias matching
        for name, info in self.entities.items():
            for alias in info.get('aliases', []):
                if alias in message:
                    ent = self.entities[name]
                    if asked_field and asked_field in ent:
                        return {'found': True,
                                'response': f"{name} {asked_field}：{ent[asked_field]}"}
                    cn_labels = {'email': '邮箱', 'phone': '电话', 'address': '住址', 'city': '城市',
                                'company': '公司', 'title': '职位', 'hobby': '爱好', 'note': '备注'}
                    lines = [f"┌─ {name} ─{'─' * max(1, 12 - len(name))}┐"]
                    has_any = False
                    for fk in ['email', 'phone', 'address', 'city', 'company', 'title', 'hobby', 'note']:
                        if fk in ent and ent[fk]:
                            lines.append(f"│ {cn_labels.get(fk, fk)}：{ent[fk]}")
                            has_any = True
                    if ent.get('aliases'):
                        aliases_str = '、'.join(ent['aliases'])
                        if aliases_str:
                            lines.append(f"│ 别名：{aliases_str}")
                            has_any = True
                    if not has_any:
                        lines.append("│ 暂无详细信息")
                    lines.append("└" + "─" * 24 + "┘")
                    return {'found': True, 'response': '\n'.join(lines)}

        return {'found': False, 'response': ''}

    def lookup_entity_field(self, name, field="email"):
        if name in self.entities:
            ent = self.entities[name]
            return {field: ent.get(field, "")}
        for ename, edata in self.entities.items():
            aliases = edata.get("aliases", [])
            if name in aliases or name == ename or name in ename:
                return {field: edata.get(field, "")}
        return None

    # -- Skill matching --

    def _extract_param(self, message, skill_def, matched_keyword, result):
        """Extract string params using skill-specific rules.
        Replaces the generic 'everything after keyword' approach.
        """
        extract_config = skill_def.get('extract', {})
        if not extract_config:
            return
        
        for pname, pdef in skill_def.get('params', {}).items():
            if pdef.get('type') != 'string':
                continue
            if pname in result['params'] and result['params'][pname]:
                continue  # already filled by entity/path/etc
            
            rule = extract_config.get(pname)
            if not rule:
                continue  # no rule = skip, don't guess
            
            mode = rule.get('mode', 'after_keyword')
            
            if mode == 'after_keyword':
                remaining = message
                if matched_keyword:
                    idx = message.lower().find(matched_keyword.lower())
                    if idx >= 0:
                        remaining = message[idx + len(matched_keyword):].strip()
                # Apply strip patterns
                for prefix in rule.get('strip_leading', []):
                    remaining = re.sub(r'^' + re.escape(prefix) + r'\s*', '', remaining)
                for suffix in rule.get('strip_tail', []):
                    remaining = re.sub(r'\s*' + re.escape(suffix) + r'.*$', '', remaining).strip()
                if rule.get('strip_leading_regex'):
                    remaining = re.sub(rule['strip_leading_regex'], '', remaining).strip()
                if rule.get('strip_tail_regex'):
                    remaining = re.sub(rule['strip_tail_regex'], '', remaining).strip()
                if remaining:
                    result['params'][pname] = remaining
                    
            elif mode == 'regex':
                pattern = rule.get('pattern', '')
                group = rule.get('group', 1)
                if pattern:
                    m = re.search(pattern, message)
                    if m:
                        val = m.group(group).strip() if m.lastindex and m.lastindex >= group else ''
                        if val:
                            result['params'][pname] = val
            
            elif mode == 'remainder':
                # Take what's left after extracting other params
                other_values = [v for k, v in result['params'].items() if k != pname and isinstance(v, str) and v]
                remaining = message
                for ov in other_values:
                    remaining = remaining.replace(ov, '', 1)
                remaining = remaining.strip()
                if remaining:
                    result['params'][pname] = remaining

    def parse(self, message):
        """Parse message into skill + params + entities.
        Returns: {'skill': (skill_name, skill_def, score), 'entities': [...], 'paths': [...], 'params': {...}}
        """
        result = {'skill': (None, None, 0), 'entities': [], 'paths': [], 'params': {}, 'filename': None}
        matched_keyword = None  # track which keyword triggered the best skill match

        # Match skills by keywords (word-boundary for short patterns < 4 chars)
        for sk_name, sk_def in self.skills.items():
            keywords = sk_def.get('patterns', sk_def.get('keywords', []))
            score = 0
            best_kw = None
            for kw in keywords:
                kw_lower = kw.lower()
                msg_lower = message.lower()
                matched = False
                
                if len(kw) <= 3 and kw.isascii():
                    # Word boundary match for ASCII keywords
                    if re.search(r'\b' + re.escape(kw_lower) + r'\b', msg_lower):
                        matched = True
                elif len(kw) <= 2:
                    # Short CJK keywords: require context — must be at start,
                    # after punctuation/space, or before a word boundary pattern
                    # This prevents "查" matching inside "调查", "写" inside "描写", etc.
                    precision = sk_def.get('precision', 'high')
                    if precision == 'low':
                        if kw_lower in msg_lower:
                            matched = True
                    else:
                        # High precision: keyword must be a clear command start
                        # Pattern: (start|punctuation|space) + keyword + (non-CJK|end|space)
                        # Safe left-context CJK: function words that don't form compounds
                        _SAFE_LEFT = set('的我你他她它们帮给把被在不很都也会要能就来去上'
                                        '和下里外前后中对向为与同跟从到让叫使请替')
                        # Find all positions of kw in message
                        _pos = 0
                        while True:
                            _pos = msg_lower.find(kw_lower, _pos)
                            if _pos < 0:
                                break
                            # Check left context: OK if start, punctuation, non-CJK, or safe CJK
                            left_ch = msg_lower[_pos-1] if _pos > 0 else ''
                            left_ok = (_pos == 0 or
                                      left_ch in ' ,，。！？、：；\n\t' or
                                      left_ch in _SAFE_LEFT or
                                      not ('CJK' in str(left_ch.encode('unicode_escape'))))
                            # Check right context: must have content after keyword
                            # ★ 2026-09-22 修 (真实语料实测): 原来把**空格**也算"没内容",
                            #   于是 "写入 你好" 里的 "写入" 判不匹配, 只剩裸 "写" 匹配 ⇒
                            #   写进文件的内容变成 "入 你好"。改为: 跳过空格后仍需有内容。
                            _rt = _pos + len(kw)
                            while _rt < len(msg_lower) and msg_lower[_rt] in ' \t':
                                _rt += 1
                            right_ok = (_rt < len(msg_lower) and
                                       msg_lower[_rt] not in ' ,，。！？、：；\n\t')
                            if left_ok and right_ok:
                                matched = True
                                break
                            _pos += len(kw)
                else:
                    # Normal substring match for longer keywords
                    if kw_lower in msg_lower:
                        matched = True
                
                if matched:
                    score += 1.0 / len(keywords)
                    if best_kw is None or len(kw) > len(best_kw):
                        best_kw = kw
            if score > result['skill'][2]:
                result['skill'] = (sk_name, sk_def, score)
                matched_keyword = best_kw

        # Extract entities
        for name in self.entities:
            if name in message:
                result['entities'].append({'name': name, 'info': self.entities[name]})

        # Natural language path patterns: "在X目录下" / "到X目录" / "X目录里"
        _nl_path_map = {}
        _nl_dir_match = re.search(r'(?:在|到)(.{1,20}?)(?:目录|文件夹)(?:下|里|中)?', message)
        _nl_save_match = re.search(r'(?:保存到|存到|写入到)(.{1,20}?)$', message)
        if not _nl_dir_match and _nl_save_match:
            _nl_dir_match = _nl_save_match
        if _nl_dir_match:
            dir_hint = _nl_dir_match.group(1).strip()
            # Try exact match with environment names/aliases first
            found = False
            for env_name, env_info in self.environment.items():
                if env_name == dir_hint or dir_hint == env_name:
                    _nl_path_map['_nl_dir'] = env_info['value']
                    found = True
                    break
                for alias in env_info.get('aliases', []):
                    if alias == dir_hint:
                        _nl_path_map['_nl_dir'] = env_info['value']
                        found = True
                        break
                if found:
                    break
            if not found:
                # Try partial match
                for env_name, env_info in self.environment.items():
                    if env_name in dir_hint or dir_hint in env_name:
                        _nl_path_map['_nl_dir'] = env_info['value']
                        found = True
                        break
            if not found:
                # Direct mapping for common location names
                import os as _os_nl
                home = _os_nl.path.expanduser('~')
                _LOCATION_MAP = {
                    '桌面': _os_nl.path.join(home, 'Desktop'),
                    'desktop': _os_nl.path.join(home, 'Desktop'),
                    '文档': _os_nl.path.join(home, 'Documents'),
                    'documents': _os_nl.path.join(home, 'Documents'),
                    '下载': _os_nl.path.join(home, 'Downloads'),
                    'downloads': _os_nl.path.join(home, 'Downloads'),
                }
                if dir_hint.lower() in _LOCATION_MAP:
                    _nl_path_map['_nl_dir'] = _LOCATION_MAP[dir_hint.lower()]
                else:
                    # Check if it's a subdirectory of Desktop
                    desktop = _os_nl.path.join(home, 'Desktop')
                    candidate = _os_nl.path.join(desktop, dir_hint)
                    if _os_nl.path.isdir(candidate):
                        _nl_path_map['_nl_dir'] = candidate
                    else:
                        _nl_path_map['_nl_dir'] = candidate  # Best guess

        # Extract paths (environment + raw paths in message)
        for env_name, env_info in self.environment.items():
            if env_name in message:
                result['paths'].append({'name': env_name, 'path': env_info['value']})
        # Also extract raw Windows/Unix paths from message (e.g. D:/xxx, C:\xxx, /home/xxx)
        # ★ 2026-09-22: 目录词兜底要放在**文件名抽取之前** —— 这样文件名块会自己把目录前缀
        #   加上, 与"显式路径"那条分支口径一致。(放后面就得额外补前缀, 结果 path 参数只剩目录,
        #   实测 "在桌面新建 note.txt" 的 path 变成纯 Desktop。)
        if '_nl_dir' not in _nl_path_map:
            # ★ 护栏: 别名可能出现在消息里**已有的绝对路径内部** (本机环境表里"项目目录"
            #   有个短别名长得像路径的一段), 命中位置若落在路径区间内就不算目录词 ——
            #   否则会把目录错认成项目根。(注释不写真名: 源码会随分发包出门, 真名属个人标识)
            _spans0 = [(m.start(), m.end()) for m in re.finditer(r'[A-Za-z]:[/\\][^\s,;，；、]+', message)]
            _low0 = message.lower()
            for _en, _ei in (self.environment or {}).items():
                if not (_ei or {}).get('value'):
                    continue
                for _w in [_en] + list((_ei or {}).get('aliases') or []):
                    if not _w or len(str(_w)) < 2:
                        continue
                    _i = _low0.find(str(_w).lower())
                    if _i < 0 or any(a <= _i < b for a, b in _spans0):
                        continue
                    _nl_path_map['_nl_dir'] = _ei['value']
                    logger.debug("[KB] 目录词兜底命中: %s → %s", _w, _ei['value'])
                    break
                if '_nl_dir' in _nl_path_map:
                    break

        #: 支持的扩展名 (文件名/路径解析共用) —— ★ 必须先定义: 下面的 raw_paths 截断要用它
        _EXT = (r'(?:txt|text|py|json|md|markdown|pdf|docx?|xlsx?|pptx?|csv|png|jpe?g|gif'
                r'|log|mp3|mp4|zip|rar|html?)')
        raw_paths = re.findall(r'[A-Za-z]:[/\\][^\s,;，；、]+', message)
        for rp in raw_paths:
            rp_clean = rp.rstrip('.,;!?。，；！？')
            # ★ 2026-09-22 修 (用户原话实测: "把结果写入F:\\data.txt文件，注意格式"
            #   → 路径被解析成 `F:\\data.txt文件`, 会去开一个**错文件**)。
            #   真实说法里路径后面**紧贴散文** (文件/里/中/的内容…), 所以:
            #   若路径里出现 `.扩展名`, 就**在扩展名处截断** (后面的都是散文)。
            # ★ 注意用 (?![A-Za-z0-9_]) 而不是 (?!\w): `\w` 在 Unicode 下**包含中文**,
            #   于是 "data.txt文件" 里 ".txt" 后面跟着"文"就被判成"后面还有字符" → 截不断。
            _ext_hit = re.search(r'\.(?:' + _EXT + r')(?![A-Za-z0-9_])', rp_clean, re.I)
            if _ext_hit:
                rp_clean = rp_clean[:_ext_hit.end()]
            rp_clean = rp_clean.strip().strip('"\'“”「」【】《》')
            if rp_clean not in [p['path'] for p in result['paths']]:
                result['paths'].append({'name': rp_clean, 'path': rp_clean})

        # ── 抽文件名 (含扩展名) ──────────────────────────────────────────────
        # ★ 2026-09-22 重写 (用户真实原话实测驱动, 会话库 116 条):
        #   旧正则 `([a-zA-Z0-9][a-zA-Z0-9_-]{0,30}\.(?:txt|…))` 要求**首字符是 ASCII**
        #   ⇒ **中文文件名("大哥.txt")完全匹配不到** ⇒ 落到下面"自动生成"分支
        #   ⇒ 用户明确写的文件名被悄悄换成 `doc_<时间戳>.txt`, 而且报"成功"。
        #   实测 7 条真实消息全中:
        #     "写一首诗保存到桌面大哥.txt"  → 生成 doc_20260922_164721.txt (用户要的是 大哥.txt)
        #     "写一首诗到桌面 大哥.txt"     → 文件名解析为空
        #     "这首诗写入桌面大哥.txt"      → 文件名解析为空
        #     "把这首诗写入大哥.txt"        → 文件名解析为空
        #   用户从来没写过我文档里那种 "桌面/文件名" 格式; 真实说法是
        #   "到桌面X.txt" / "桌面 X.txt" / "写入X.txt" —— 解析必须按**用户的说法**来。
        # ★ 同样用 (?![A-Za-z0-9_]) 而不是 (?!\w) —— 否则 "写入F:\\data.txt文件" 这类
        #   扩展名后紧跟中文的说法一个都匹配不到 (中文是 \w)。
        _FN_TAIL = re.compile(r'([^\s，。、；：,;！!？?"\'（）()\[\]]{1,60}?\.' + _EXT + r')(?![A-Za-z0-9_])', re.I)
        _DIRWORD = ('桌面', '文档', '下载', 'desktop', 'documents', 'downloads')
        #: 动词/量词: 逐层从**左**剥掉, 直到剩下像文件名
        _STRIP_PREFIX = ('写入到', '保存到', '另存为', '命名为', '取名为', '写到', '写入', '写进',
                         '存到', '存进', '放进', '放到', '放入', '放在', '创建', '新建', '生成',
                         '写一首', '写一篇', '写一份', '写一段', '写个', '写',
                         '叫', '叫做', '帮我', '请', '把', '给',
                         '一个', '一首', '一篇', '一份', '一段', '个', '条', '句', '张', '份')

        # ★ 2026-09-22: 换用**共享规则** core/nl_paths.clean_filename ——
        #   起因: IR 链那条路对同一句话 (`写一首诗保存到桌面大哥.txt`) 解析出的是**整句**,
        #   两条路口径不一致。规则只留一份, 谁也别再各写一套。
        from core.nl_paths import clean_filename as _clean_filename

        _cands = []
        for _m in _FN_TAIL.finditer(message):
            _c = _clean_filename(_m.group(1))
            # 清完还必须像个文件名: 有扩展名、不含语气助词、长度合理
            if (_c and '.' in _c and len(_c) <= 45
                    and not re.search(r'[的了呢吗啊呀吧嘛]', _c)
                    and not _c.startswith(('，', '。'))):
                _cands.append(_c)
        if _cands:
            # 多个候选时取最短的 (最像"纯文件名"; 长的通常是整句散文)
            _cands.sort(key=lambda x: len(x))
            fname = _cands[0]
            if '_nl_dir' in _nl_path_map:
                fname = _nl_path_map['_nl_dir'].rstrip('/\\') + '/' + fname
            result['filename'] = fname
        elif '_nl_dir' in _nl_path_map:
            # NL directory detected but no explicit filename — auto-generate
            import datetime as _dt2
            ts = _dt2.datetime.now().strftime("%Y%m%d_%H%M%S")
            auto_fn = f"doc_{ts}.txt"
            result['filename'] = _nl_path_map['_nl_dir'].rstrip('/\\') + '/' + auto_fn

        # ★ 2026-09-22 修 (用户原话实测: "写一首关于青山绿水的七言绝句" 被当成"写文件"):
        #   `写文件` 技能的 patterns 里有**裸"写"**(见 data/skills.json), 于是任何含"写"的话
        #   ("写一首诗" / "你刚才写诗了吗" / "你给我写个剧本") 都被认成写文件请求 ——
        #   没有文件名时就去开文件, 报出用户看不懂的 `Is a directory: .`。
        #   写文件必须有**文件信号**: 有扩展名文件名 / 有路径 / 有目录词 / 明说"文件(名)"。
        #   没有 → 不算写文件 (清掉技能), 交给内容生成 (模型直接把诗/剧本答出来)。
        if result['skill'][0] and (result['skill'][1] or {}).get('action') == 'write':
            # ★ 信号口径 (两轮实测收敛):
            #   ① 有扩展名文件名 / 有绝对路径 / 有目录词 → 明确是写文件
            #   ② **文件类名词** (文件/文档/表格/清单/附件) 或**文件专用写法**
            #      (写一行/写入文件/创建文件/新建文件) → 也是写文件
            #      ⇒ 保留技能, 让它在"缺文件名"时**如实报错** (verify_honest_failure 要求
            #        垃圾输入必须诚实报错; 我上一版把这类一并放走 ⇒ 丢了诚实报错, 门禁当场抓红)
            #   ③ 纯内容生成 ("写一首诗"/"写个剧本"/"写一篇文章") → 无任何信号 → 放走
            _sig = (bool(_cands)
                    or bool(re.search(r'[A-Za-z]:[\\/]', message))
                    or bool(_nl_path_map.get('_nl_dir'))
                    or bool(re.search(r'(文件|文档|表格|清单|附件)', message))
                    or bool(re.search(r'(写一行|写入文件|创建文件|新建文件)', message)))
            if not _sig:
                logger.debug("[KB] 含'写'但无文件信号 → 不当写文件: %r", message[:60])
                result['skill'] = (None, None, 0)
                result['params'] = {}
                result['filename'] = None

        # ★ 2026-09-22 补 (用户原话实测): 真实说法 "写一首诗到桌面大哥.txt" 里**没有**
        #   "保存到/写入到", 上面的正则抓不到目录 ⇒ 文件名少了桌面前缀, 文件会落到别处。
        #   兜底: 消息里出现目录词或其别名 (桌面/文档/下载/desktop…) 就认那个目录。
        # Add NL dir to paths so L0 doesn't skip
        if '_nl_dir' in _nl_path_map:
            dir_path = _nl_path_map['_nl_dir']
            if dir_path not in [p['path'] for p in result['paths']]:
                result['paths'].append({'name': dir_path, 'path': dir_path})

        # Extract dest/target from message patterns (复制X到Y, 重命名X为Y, 移动X到Y)
        sk_name = result['skill'][0]
        if sk_name in ('复制文件', '重命名', '移动文件'):
            # Pattern: 复制/重命名/移动 PATH 到/为 DEST
            _dest_match = re.search(r'(?:复制|拷贝|重命名|改名|移动|挪)\s*(.+?)\s*(?:到|为|至)\s*(.+)', message)
            if _dest_match:
                src = _dest_match.group(1).strip()
                dest = _dest_match.group(2).strip().rstrip('.,;!?。，；！？')
                # Resolve dest path
                for env_name, env_info in self.environment.items():
                    if env_name in dest:
                        dest = dest.replace(env_name, env_info['value'])
                        break
                result['params']['dest'] = dest
                # Update path to source
                if not result['paths']:
                    for env_name, env_info in self.environment.items():
                        if env_name in src:
                            result['paths'].append({'name': env_name, 'path': env_info['value']})
                            src = src.replace(env_name, env_info['value'])
                            break
                    if not result['paths']:
                        result['paths'].append({'name': src, 'path': src})

        # Fill params from entity/environment
        if result['skill'][0]:
            sd = result['skill'][1]
            for pname, pdef in sd.get('params', {}).items():
                ptype = pdef.get('type', '')
                if ptype == 'fixed':
                    result['params'][pname] = pdef.get('value', '')
                elif ptype == 'entity' and result['entities']:
                    ent = result['entities'][0]['info']
                    if pname == 'openid':
                        result['params'][pname] = ent.get('openid', '')
                    else:
                        result['params'][pname] = ent.get('email', ent.get('name', ''))
                elif ptype == 'path' and result['paths']:
                    # Prefer raw paths with file extensions over environment dirs
                    best = result['paths'][0]['path']
                    for p in result['paths']:
                        fname = p['path'].replace('\\', '/').rsplit('/', 1)[-1]
                        if '.' in fname:
                            best = p['path']
                            break
                    # Prepend natural language directory if detected
                    if '_nl_dir' in _nl_path_map and '/' not in best.replace('\\', '/'):
                        best = _nl_path_map['_nl_dir'].rstrip('/\\') + '/' + best
                    # Append filename if we have one and path looks like a directory
                    _fn = result.get('filename')
                    if _fn and '/' not in _fn:  # filename only, no directory prefix
                        # Check if best path is a directory (no extension)
                        _best_name = best.replace('\\', '/').rsplit('/', 1)[-1]
                        if '.' not in _best_name:
                            best = best.rstrip('/\\') + '/' + _fn
                    result['params'][pname] = best
                elif ptype == 'path' and result.get('filename'):
                    # Use extracted filename as path if no environment path
                    if pname not in result['params'] or not result['params'][pname]:
                        result['params'][pname] = result['filename']
                elif ptype == 'entity' and result['entities']:
                    # For push_notify: auto-inject openid from entity
                    ent_info = result['entities'][0]['info']
                    if pname == 'openid':
                        result['params'][pname] = ent_info.get('openid', '')
                    else:
                        result['params'][pname] = ent_info.get('email', ent_info.get('name', ''))
                elif ptype == 'string':
                    # Handled by _extract_param below — skip here
                    pass
                else:
                    # unknown type — provide sensible default
                    # Don't overwrite already-extracted values (like dest from regex)
                    if pname not in result['params'] or not result['params'].get(pname):
                        defaults = {'subject': '虎哥自动消息', 'body': '（虎哥自动发送）',
                                   'title': '虎哥通知', 'content': ''}
                        result['params'][pname] = defaults.get(pname, '')

        # ── Structured param extraction (replaces generic string extraction) ──
        if result['skill'][0]:
            self._extract_param(message, result['skill'][1], matched_keyword, result)
        
        # Auto-extract content from message if still empty
        _dbg_pre_content = result['params'].get('content')
        if not _dbg_pre_content:
            # Check if this is a write-like operation
            sk_action = result['params'].get('action', '')
            sk_name = result['skill'][0] or ''
            is_write = (sk_action == 'write' or sk_name in ('写文件',))
            # Try to extract content after keywords like 写入/内容/正文/写
            content_kw = ['写入', '内容', '正文']
            if is_write:
                content_kw = ['写入', '内容', '正文', '写']
            for kw in content_kw:
                if kw in message:
                    after = message.split(kw, 1)[-1].strip()
                    if after:
                        # Strip trailing save-to instructions
                        after = re.sub(r'(?:保存到|存到|写入到|放到)\s*\S+$', '', after).strip()
                        # Strip leading quantifiers: 一个, 一段, 个, 一篇, 一份, etc.
                        # ★ 2026-09-23 补: 原来漏了"一首/一幅/一封/一则…" ⇒
                        #   "写一首诗放桌面" 抽出的内容带上"一首"。
                        after = re.sub(r'^(?:一[个段篇份条句首幅部封则组套张页笔]|个|条|句)\s*', '', after)
                        # ★ 2026-09-22 修 (真实语料实测): 内容里**不许留目标路径** ——
                        #   "把结果写入F:\\data.txt，注意格式" 原样把路径吃进内容。
                        _pp = str(result['params'].get('path') or result.get('filename') or '')
                        if _pp and _pp in after:
                            after = after.replace(_pp, ' ')
                        _pbase = _pp.replace('\\', '/').rsplit('/', 1)[-1]
                        if _pbase and _pbase in after:
                            after = after.replace(_pbase, ' ')
                        after = re.sub(r'^[\s,，。、;；:：]+', '', after).strip()
                        if not after:
                            break            # 内容就是路径 → 没有正文, 不硬凑
                        result['params']['content'] = after
                        logger.debug(f"[KNOWLEDGE-DBG] content extracted: {repr(after)} from msg: {repr(message[:80])}")
                        break

        # ★ 2026-09-22 内容终检 (统一收口, 覆盖走 skills.json extract 规则那条分支):
        #   文件内容里**不许留目标路径** —— 实测 "把结果写入F:\\data.txt，注意格式"
        #   会把路径整体吃进内容。只剥**带分隔符**的路径 (裸名不剥, 免得动到 "hello.txt内容是xxx"
        #   这种既有行为)。
        _c = str(result['params'].get('content') or '')
        if _c:
            for _k in ('path', 'file', 'dest', 'to'):
                _v = str(result['params'].get(_k) or '')
                if _v and ('/' in _v or '\\' in _v) and _v in _c:
                    _c = _c.replace(_v, ' ')
            if result.get('filename'):
                _fv = str(result['filename'])
                if ('/' in _fv or '\\' in _fv) and _fv in _c:
                    _c = _c.replace(_fv, ' ')
            # ★ 2026-09-23 统一守卫 (用户实测 "写一首诗放桌面"/"…搁桌面" 写出 6 字垃圾文件):
            #   抽出来的"内容"若**以目录词收尾** (桌面/文档/下载…), 那它只是**指令的尾巴**,
            #   不是正文 —— 真实正文几乎不会以"桌面"结尾。
            #   ★ 判据用"结尾是目录词"而不是逐个列放置动词: 第一版列了(放/存/写/丢/摆/贴),
            #     实测漏掉口语的"搁桌面" ⇒ 又写出 6 字垃圾。收成一种形态才守得住。
            #   ★ 位置必须在**内容终检**: 内容有两条抽取路径 (skills.json extract 规则 /
            #     本函数的关键词抽取), 只在其中一条里堵 = 另一条照样漏 (实测漏的就是前者)。
            #   无显式文件名 (_cands 空) 才拦: 有文件名时按原行为写。
            _DIRW = r'(?:桌面|文档|下载|desktop|documents|downloads)'
            _act_tail = (
                re.search(_DIRW + r'\s*$', _c, re.I)              # ① 以目录词收尾
                or re.search(r'(?:保存到|存到|写入到|写到|放到|搁到|扔到|丢到|摆到|另存为)\s*\S*$', _c)
                # ② 内容里同时有「落盘动词 + 目录词」⇒ 还是指令, 不是正文
                #   实测 "随手写首诗摆桌面上" 抽出 "首诗摆桌面上" (结尾是"上") ⇒ ① 漏掉
                or (re.search(r'(?:放|搁|扔|丢|塞|摆|存|保存|写入|写到|写进|导出|另存)', _c)
                    and re.search(_DIRW, _c, re.I))
                # ③ 内容里同时有「产出动词 + 目录词」⇒ 同上
                or (re.search(r'(?:写|作|生成|创作|弄|整|来|搞|画|编)', _c)
                    and re.search(_DIRW, _c, re.I))
            )
            if (_act_tail and not _cands):
                logger.debug("[KB] 内容是指令尾巴(以目录词收尾) → 不写盘: %r", _c[:40])
                result['skill'] = (None, None, 0)
                result['params'] = {}
                result['filename'] = None
                logger.debug("[KNOWLEDGE-DBG] FINAL parse: skill=None, params={}, filename=None")
                return result
            _c2 = re.sub(r'^[\s,，。、;；:：]+', '', _c).strip()
            if _c2 != _c:
                result['params']['content'] = _c2

        logger.debug(f"[KNOWLEDGE-DBG] FINAL parse: skill={result['skill'][0]}, params={result['params']}, filename={result.get('filename')}")
        return result

    # ── Action execution ──

    async def execute(self, parsed, plugin_mgr):
        """Execute a parsed action via plugin gateway."""
        sk_name, sk_def, score = parsed['skill']
        if not sk_name or score < 0.02:
            return {'success': False, 'error': 'No matching skill'}

        tool_name = sk_def.get('tool', sk_name)
        params = dict(parsed.get('params', {}))
        # Only pass action if explicitly defined in skill or params
        action = params.pop('action', None) or sk_def.get('action')
        call_kwargs = {}
        if action:
            call_kwargs['action'] = action
        call_kwargs.update(params)
        # 用抽到的文件名补全 path —— 但**不能**用裸名覆盖用户已给的完整路径。
        # 旧写法 `call_kwargs['path'] = fname` 会把 "D:/x/tmp/a.txt" 覆盖成裸名 "a.txt",
        # 而 file_ops 对裸名默认落桌面 → 文件写错地方 (实测: 指定 D:\...\tmp 却落到 Desktop)。
        fname = parsed.get('filename')
        if fname and 'path' in call_kwargs:
            _p = str(call_kwargs.get('path') or '').strip()
            _pn = _p.replace('\\', '/').rstrip('/')
            _last = _pn.rsplit('/', 1)[-1] if _pn else ''
            # ★ 2026-09-22 修 (用户实测 + 本地复现的"双层桌面路径"): fname 可能**已经带目录**
            #   (parse 在识别到目录词时会把前缀加进 filename)。此时若再 join 一次, 就得到
            #   `C:/…/Desktop/C:\…\Desktop/note.txt` → 写盘报 Access denied。
            #   实测复现: "在桌面新建 note.txt 写入 你好" / "写一首诗到桌面大哥.txt" 全部命中。
            _fn_abs = bool(re.match(r'^[A-Za-z]:', fname) or fname.startswith('/'))
            if not _pn:
                call_kwargs['path'] = fname                 # 无路径 → 裸名 (file_ops 默认桌面)
            elif _fn_abs:
                call_kwargs['path'] = fname                 # fname 已是完整路径 → 用它, 不再拼
            elif '.' not in _last:
                call_kwargs['path'] = _pn + '/' + fname     # 路径指向目录 → 拼上文件名
            # 否则 path 已是完整文件路径 → 原样保留

        # Auto-analyze Excel files before any tiger_office operation
        pre_analysis = ""
        if tool_name == 'tiger_office' and action not in ('analyze_table', 'scan_excel', 'merge_base64', 'export_cards'):
            file_path = call_kwargs.get('path') or call_kwargs.get('folder') or call_kwargs.get('files')
            if file_path and isinstance(file_path, str) and file_path.endswith('.xlsx'):
                try:
                    import os as _osx
                    if _osx.path.isfile(file_path):
                        ana_result = await plugin_mgr.call(tool_name, action='analyze_table', path=file_path)
                        if ana_result.get('success'):
                            ana_output = ana_result.get('output', '')
                            pre_analysis = "[表结构]" + chr(10) + ana_output[:1200] + chr(10) + chr(10)
                except Exception:
                    pass

        try:
            result = await plugin_mgr.call(tool_name, **call_kwargs)
            if isinstance(result, dict) and result.get('ok', result.get('success')):
                output = pre_analysis + str(result.get('result', result.get('output', str(result))))
                # tiger_office import_cards: auto-learn entities from card list
                if tool_name == 'tiger_office' and action == 'import_cards' and isinstance(output, list):
                    cr = self.import_cards_from_list(output)
                    return {'success': True, 'output': f"Imported {cr['imported']}, updated {cr['updated']}"}
                return {'success': True, 'output': output}
            # tiger_office export_cards: inject entities from memory
            if tool_name == 'tiger_office' and action == 'export_cards':
                # Re-call with entities injected
                call_kwargs['entities'] = list(self.entities.values())
                result2 = await plugin_mgr.call(tool_name, **call_kwargs)
                if isinstance(result2, dict) and result2.get('success'):
                    return {'success': True, 'output': result2.get('output', str(result2))}
                error_msg = (result2.get('error') if isinstance(result2, dict) else None) or 'export failed'
            elif isinstance(result, dict):
                error_msg = result.get('error') or result.get('output') or str(result)
            else:
                error_msg = str(result)
            # 失败就是失败 —— 不再包成 success=True。
            # 旧写法('Return error as success output so pipeline shows it to user')会把错误
            # 配上 "OK <技能>:" 前缀显示给用户; 而且 skill_loader/workflow 靠 success 判断
            # 是否中断链条 → 失败被谎报成成功会让多步工作流继续往下跑。
            return {'success': False, 'error': str(error_msg)}
        except Exception as e:
            return {'success': False, 'error': str(e)}