# TMM 工具输出规范

每个工具被调用后返回一个 dict，pipeline 按以下规则提取最终显示文本。

## 必须字段

```python
{
    "success": True,       # bool — 调用是否成功
    "output": "...",       # str  — 【关键】最终显示给用户的中文文本，永远不要空
}
```

### 为什么必须有 `output`？

pipeline 的 chain 执行器（`core/pipeline.py` L1165-1170）对单工具调用的格式化逻辑：

```python
output = prev.get("output", prev.get("result", str(prev)))
```

如果没有 `output` 字段，也没有 `result` 字段，就 `str(prev)` —— 整坨 JSON 怼到用户脸上。

## output 怎么写

| 场景 | output 示例 |
|------|------------|
| 截图成功 | `"截图已保存 → C:\\Users\\<user>\\Desktop\\screenshot.png (439KB)"` |
| 截图失败 | `"截图失败：pyautogui 未安装"` |
| 文件读取 | `"已读取 xxx.py (120行)"` |
| 天气查询 | `"济南 多云 30°C 风速6.9km/h"` |
| 邮件查询 | `"收件箱 3/10 封匹配 '关键词'"` |
| 文件列表 | `"D:\\test\\ 共12个文件"` |
| 文件写入 | `"已写入 xxx.txt (1,234B)"` |
| 通用错误 | `"操作失败：权限不足"` |

**原则：**
- 一句中文说清楚结果
- 包含关键信息（路径、大小、数量）
- 不要吐 JSON
- 不要让用户去猜发生了什么

## 可选字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | bool | 兼容字段，同 `success` |
| `error` | str | 失败时的错误描述（pipeline 优先用 output） |
| `result` | str | 备选显示字段（次于 output） |
| `message` | str | 仅用于日志，不显示给用户 |
| `path` | str | 文件路径（截图等） |
| `size` | int | 文件大小（字节） |
| `meta` | str | 元信息（文件读取的行数等） |
| `total_lines` | int | 文件总行数 |
| `data` | any | 结构化数据（供后续工具消费，不显示） |

## 检查清单

改任何工具时自查：

- [ ] `success` / `ok` 是否正确设置？
- [ ] `output` 是中文、有信息量、一句说清？
- [ ] `error` 字段在失败时是否提供了有用信息？
- [ ] 返回的 dict 在 `str()` 后是否可读（兜底情况）？

## 已知已修的工具

| 工具 | 问题 | 修复 |
|------|------|------|
| `windows_desktop` | `action=screenshot` 走 capture 返回元素 JSON | 分离为两个 action；增加 `output` 字段 |
| `windows_desktop` | `action=screenshot_now` 无 `output` | 补 `output: "截图已保存 → path (KB)"` |
| `intent_router` | `build_chain` 把 action 放在 step 外层，pipeline 不传 | pipeline 增加 `s["action"] → params["action"]` 合并 |
| `browser` | keywords 含"截图"，抢了 desktop 的路由 | 移除"截图"关键词 |

## 端到端验证法

```
Tiger.M.M >>> 截图
截图已保存 → C:\Users\<user>\Desktop\screenshot.png (415KB)
```

不是这个格式就说明哪个环节漏了。
