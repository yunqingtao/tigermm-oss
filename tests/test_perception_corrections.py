"""感知引擎的"纠正检测" — 测试 (2026-09-20 修)

为什么要有这一份:
    perception._detect_corrections() 的结果会被 get_correction_context() 注入**系统提示词**
    (`[Learned corrections — do NOT repeat these mistakes]`)。所以误报不是"多两条记录",
    而是**直接改变模型行为** —— 它会让模型以为"用户纠正过识别这件事, 别提了"。

修之前实测: 10 句正常话有 8 句被记成纠正。
  裸 `别` 命中 识**别** / 区**别** / 特**别** / 级**别** / 分**别** / 告**别** / 个**别**
  裸 `不是` 命中 **是不是**该… (那是提问)
  裸 `换`  命中 转**换** / 交**换**
修法: 否定环视排除复合词; 过松的裸 `换` 弃用 (漏判比误判安全 —— 误判会污染提示词)。

实测: 20 句正常话 0 误报 · 9 句真纠正 0 漏判。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.perception import PerceptionEngine

# 正常话 —— 一句都不许记成"用户在纠正我"
INNOCENT = [
    "你能识别我的电脑吗",
    "帮我识别图片文字",
    "这个和那个有什么区别",
    "特别想知道天气",
    "级别不够吧",
    "是不是该换个方案",          # 提问, 不是纠正
    "把文件转换成 PDF",
    "把 A 替换成 B",
    "分别处理这两个",
    "告别过去",
    "换个字体",
    "别人怎么说",
    "别的呢",
    "个别情况",
    "差别很大",
    "鉴别一下",
    "辨别真假",
    "性别的区别",
    "派别之争",
    "交换文件",
]

# 真纠正 —— 必须认出来
REAL = [
    "不对，重来",
    "不是这样，改成红色",
    "错了，应该是苏州",
    "别再忘了",
    "别这样",
    "不要那样做",
    "重新来一遍",
    "改成红色",
    "换成苏州",
]


@pytest.fixture
def pe():
    return PerceptionEngine.__new__(PerceptionEngine)


class TestNoFalsePositives:
    """★★ 核心: 正常话不得被记成纠正 (误报会污染系统提示词)。"""

    @pytest.mark.parametrize("msg", INNOCENT)
    def test_innocent_not_recorded(self, pe, msg):
        got = pe._detect_corrections(msg)
        assert got == [], f"{msg!r} 被误记成纠正: {got}"

    def test_bare_bie_in_compound(self, pe):
        """★ 裸 `别` 是误报主源 —— 复合词里都不算。"""
        for w in ("识别", "区别", "特别", "级别", "分别", "告别", "个别", "差别",
                  "性别", "派别", "鉴别", "辨别"):
            assert pe._detect_corrections(f"这是{w}的事") == [], w

    def test_bushi_question(self, pe):
        """★ `不是` 与提问 `是不是` 必须分开。"""
        assert pe._detect_corrections("是不是该换个方案") == []
        assert pe._detect_corrections("这个是不是有问题") == []
        assert pe._detect_corrections("不是这样") != []

    def test_bie_with_ren(self, pe):
        """`别人/别的` 是名词短语, 不是纠正。"""
        for w in ("别人怎么说", "别的呢", "别的东西"):
            assert pe._detect_corrections(w) == [], w

    def test_huancheng_in_compound(self, pe):
        """★ `换成` 不能命中 `转换成/替换成/变成`。"""
        for w in ("把文件转换成 PDF", "把 A 替换成 B", "把水变成冰"):
            assert pe._detect_corrections(w) == [], w


class TestRealCorrections:
    """真纠正必须认出来 (否则学习闭环形同虚设)。"""

    @pytest.mark.parametrize("msg", REAL)
    def test_real_recorded(self, pe, msg):
        got = pe._detect_corrections(msg)
        assert got and got[0]["user_said"] == msg[:200], f"{msg!r} 没被认出"

    def test_marker_is_human_readable(self, pe):
        m = pe._detect_corrections("不对，重来")[0]["marker"]
        assert m == "不对", m
        assert "?" not in m and "(" not in m, "marker 应是可读词, 不是正则"

    def test_at_most_one_marker(self, pe):
        """一句话只记一条 (旧行为保留)。"""
        assert len(pe._detect_corrections("不对，不是这样，错了")) == 1

    def test_empty_safe(self, pe):
        assert pe._detect_corrections("") == []


class TestMarkersAreGuarded:
    """★ 表结构约束: 每条标记都要有"可读词 + 正则"两个字段, 且正则能编译。"""

    def test_structure(self, pe):
        ms = PerceptionEngine._CORRECTION_MARKERS
        assert isinstance(ms, list) and len(ms) >= 8
        import re as _re
        for pair in ms:
            assert len(pair) == 2, pair
            word, pat = pair
            _re.compile(pat)                      # 编译不了就是坏表
            assert word and not any(c in word for c in "(?<"), f"marker 不该是正则: {word}"

    def test_no_naked_risky_markers(self, pe):
        """★ 回归守卫: 过松的裸标记禁止回到表里 (它们正是误报源)。"""
        pats = dict(PerceptionEngine._CORRECTION_MARKERS)
        assert pats.get("别") != "别", "裸 `别` 会命中 识别/特别/区别 —— 不许回来"
        assert pats.get("不是") != "不是", "裸 `不是` 会命中 是不是 —— 不许回来"
        assert "换" not in pats, "过松的裸 `换` 已弃用 (改用 换成)"
