"""Intent Reasoner v2.0 — Chinese-aware tokenization, alias matching, intent classification.
Zero external dependencies. Language-order independent.
"""
import re
import json
from pathlib import Path

# Action word mapping
_SEND_WORDS = {'发', '发给', '发送', '发邮件'}
_LIST_WORDS = {'列出', '有什么', '有哪些', '看看', '看', '显示'}
# 弱列表词: 单独出现时是疑问("有什么改进建议"), 不是列目录 → 需容器语义共同出现
_WEAK_LIST_WORDS = {'有什么', '有哪些', '看看', '看', '显示'}
# 容器语义词: 出现任一 → 弱列表词才算真的"列目录"
_CONTAINER_WORDS = ['目录', '文件夹', '桌面', '下载', '文档', '文件', '盘里',
                    '里面', '里头', '这里有', '下有什么', '下有哪些', '都有什么', '有啥']
_WRITE_WORDS = {'建', '新建', '创建', '写', '生成'}
_QUERY_WORDS = {'谁', '是什么', '查', '查看', '邮箱', '电话', '地址', '信息'}
_DELETE_WORDS = {'删除', '删', '去掉', '移除', '忘记'}
_READ_WORDS = {'读', '读取', '看', '打开'}
_TRAVERSE_WORDS = {'遍历', '扫描', '搜', '找', '搜索文件'}

_ACTION_MAP = {
    '发': 'send', '发给': 'send', '发送': 'send', '发邮件': 'send',
    '列出': 'list', '有什么': 'list', '有哪些': 'list', '看看': 'list',
    '建': 'write', '新建': 'write', '创建': 'write', '写': 'write', '生成': 'write',
    '谁': 'query', '是什么': 'query', '查': 'query', '查看': 'query',
    '邮箱': 'query', '电话': 'query', '地址': 'query', '信息': 'query',
    '删除': 'delete', '删': 'delete', '去掉': 'delete', '移除': 'delete', '忘记': 'delete',
    '读': 'read', '读取': 'read', '打开': 'read',
    '遍历': 'traverse', '扫描': 'traverse', '搜': 'traverse', '找': 'traverse', '搜索文件': 'traverse',
}


class IntentReasoner:
    """Token-based intent reasoning with entity + environment awareness."""

    def __init__(self, entities, environment):
        self.entities = entities
        self.environment = environment
        self._build_index()

    def _build_index(self):
        # Entity alias -> canonical name
        self._entity_alias = {}
        for name, info in self.entities.items():
            self._entity_alias[name.lower()] = name
            for alias in info.get('aliases', []):
                self._entity_alias[alias.lower()] = name

        # Environment alias -> canonical name
        self._env_alias = {}
        for name, info in self.environment.items():
            self._env_alias[name.lower()] = name
            for alias in info.get('aliases', []):
                self._env_alias[alias.lower()] = name

    def _tokenize_chinese(self, text):
        """Tokenize Chinese text: split by known words (entities, envs, actions)."""
        # Build set of known multi-char words
        known_words = set()
        for wl in [self._entity_alias, self._env_alias, _SEND_WORDS, _LIST_WORDS,
                    _WRITE_WORDS, _QUERY_WORDS, _DELETE_WORDS, _READ_WORDS, _TRAVERSE_WORDS]:
            for w in wl:
                if len(w) >= 2:
                    known_words.add(w)

        # Sort by length (longest first) for greedy matching
        known_sorted = sorted(known_words, key=len, reverse=True)

        tokens = []
        i = 0
        while i < len(text):
            matched = False
            for kw in known_sorted:
                if text[i:i+len(kw)] == kw:
                    tokens.append(kw)
                    i += len(kw)
                    matched = True
                    break
            if not matched:
                # Single character
                tokens.append(text[i])
                i += 1
        return tokens

    def reason(self, message):
        """Main entry: classify message into intent with slots."""
        if not message or not message.strip():
            return {'intent': 'unknown', 'confidence': 0, 'slots': {}, 'entities': {}}

        msg = message.strip()

        # Step 1: Chinese-aware tokenization
        tokens = self._tokenize_chinese(msg)

        # Step 2: Identify entities and environment references
        who = None
        where = None

        for token in tokens:
            tl = token.lower()
            if tl in self._entity_alias:
                who = self._entity_alias[tl]
            if tl in self._env_alias:
                where = self._env_alias[tl]

        # Also try substring matching for longer entity names (case-insensitive)
        msg_lower = msg.lower()
        if not who:
            for alias, name in self._entity_alias.items():
                if alias in msg_lower:
                    who = name
                    break
        if not where:
            for alias, name in self._env_alias.items():
                if alias in msg_lower:
                    where = name
                    break

        # Step 3: Classify intent from action words
        intent = 'unknown'
        action_found = None

        for token in tokens:
            tl = token.lower()
            if tl in _SEND_WORDS:
                intent = 'send'
                action_found = tl
                break
            elif tl in _LIST_WORDS:
                intent = 'list'
                action_found = tl
                break
            elif tl in _WRITE_WORDS:
                intent = 'write'
                action_found = tl
                break
            elif tl in _QUERY_WORDS:
                intent = 'query'
                action_found = tl
                break
            elif tl in _DELETE_WORDS:
                intent = 'delete'
                action_found = tl
                break
            elif tl in _READ_WORDS:
                intent = 'read'
                action_found = tl
                break
            elif tl in _TRAVERSE_WORDS:
                intent = 'traverse'
                action_found = tl
                break

        # Step 4: Multi-char pattern fallback
        if intent == 'unknown':
            for kw, it in sorted(_ACTION_MAP.items(), key=lambda x: -len(x[0])):
                if kw in msg:
                    intent = it
                    action_found = kw
                    break

        # ★★ 2026-09-19 修 (真缺陷): "有什么/有哪些/看看/看/显示" 单独出现时是**疑问**,
        #   不是列目录请求。实测 "这个项目有什么改进建议呢" 被判 list → 列了 PROJECT_ROOT。
        #   修法: 弱触发词必须**同时**出现容器语义(目录/文件夹/桌面/文件…)才算 list。
        #   强触发词(列出/列)豁免 —— "列出 D:/x" 不该被这条规则拦掉。
        #   ⚠ 必须在 Step 4 **之后** (Step 4 的 _ACTION_MAP 兜底也会把"有什么"设成 list)
        if intent == 'list' and action_found in _WEAK_LIST_WORDS:
            if not any(w in message for w in _CONTAINER_WORDS):
                intent = 'unknown'
                action_found = None
            else:
                action_found = '列出'   # 弱词转具体动作词, 便于调试看清是"列目录"

        # Step 5: Entity-only queries
        if intent == 'unknown' and who:
            intent = 'query'

        # Step 6: Calculate confidence
        score = 0.0
        if action_found:
            score += 0.5
        if who:
            score += 0.3
        if where:
            score += 0.2
        if intent == 'query' and who and not action_found:
            score = 0.6

        confidence = min(1.0, score)

        # Step 7: Fill slots
        slots = {}
        if intent == 'send':
            # Resolve to email
            to_addr = None
            if who and who in self.entities:
                ent = self.entities[who]
                if not ent.get('is_self'):
                    to_addr = ent.get('email', '')
            if to_addr:
                slots['to'] = to_addr
            # Resolve path
            if where and where in self.environment:
                slots['path'] = self.environment[where]['value']
            slots['subject'] = '文件发送'
            slots['body'] = msg

        elif intent == 'list':
            if where and where in self.environment:
                slots['path'] = self.environment[where]['value']
            slots['action'] = 'list'

        elif intent == 'write':
            if where and where in self.environment:
                slots['path'] = f"{self.environment[where]['value']}\\{msg}"
            else:
                slots['path'] = msg
            slots['action'] = 'write'
            slots['content'] = ''

        elif intent == 'query':
            if who:
                slots['entity'] = who

        elif intent == 'delete':
            if who:
                slots['entity'] = who

        elif intent == 'read':
            if where and where in self.environment:
                slots['path'] = self.environment[where]['value']
            slots['action'] = 'read'

        elif intent == 'traverse':
            if where and where in self.environment:
                slots['path'] = self.environment[where]['value']
            slots['action'] = 'walk'

        return {
            'intent': intent,
            'confidence': confidence,
            'slots': slots,
            'entities': {
                'who': who,
                'where': {'name': where, 'path': self.environment[where]['value']} if where and where in self.environment else None,
                'what': msg,
                'raw_tokens': tokens
            }
        }