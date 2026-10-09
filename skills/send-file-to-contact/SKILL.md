---
name: send-file-to-contact
version: "1.2"
description: 读取文件并把内容作为附件发送给指定联系人
permission: file_read
timeout: 60
tags: [附件, 桌面, 文档, 发送给, 发给, txt发给, doc发给]
triggers: [文件发给, 附件发给, 发文件, 把这个发给, 这些发给]
requires_tools: [file_ops, send_email]
params:
  file:
    type: path
    required: true
    desc: 要发送的文件路径或名称
  recipient:
    type: entity
    field: email
    required: true
    desc: 收件人姓名
steps:
  - id: read_file
    tool: file_ops
    action: read
    input:
      path: $params.file
    output: content
    on_error: stop
  - id: resolve_contact
    tool: knowledge
    action: query_entity
    input:
      name: $params.recipient
      field: email
    output: email
    on_error: stop
  - id: send
    tool: send_email
    input:
      to: $steps.resolve_contact.email
      subject: "文件: $params.file"
      body: $steps.read_file.content
      attachments: $params.file
    on_error: stop
---

# send-file-to-contact

读取文件内容并发给指定联系人（带附件）。

## 触发
- "把桌面文件发给涛哥"
- "这个文件发给老王"、"附件发给涛哥"

## 执行流程
1. 读取文件 (file_ops.read) → content
2. 查询联系人邮箱 (knowledge.query_entity) → email
3. 发送邮件并附上文件 (send_email)

## 参数
- file: 文件路径（支持桌面/文档等环境路径）
- recipient: 收件人姓名（需在 entities.json 中注册）

## 约束
- 必须先在消息里指明文件；没有文件线索时不要启用本技能
- 收件人查不到邮箱就报错，不要猜
