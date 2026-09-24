---
name: send-email-to-contact
version: "1.1"
description: 给联系人发邮件（纯文本）
permission: system
timeout: 30
tags: [发给, 告诉, 通知, 汇报, 联系人]
triggers: [发邮件, 写邮件, 发邮件给, 邮箱]
requires_tools: [send_email]
params:
  recipient:
    type: entity
    field: email
    required: true
  content:
    type: text
    required: false
steps:
  - id: resolve
    tool: knowledge
    action: query_entity
    input:
      name: $params.recipient
      field: email
    on_error: stop
  - id: send
    tool: send_email
    input:
      to: $steps.resolve.email
      subject: 来自虎哥
      body: $params.content
    on_error: stop
---

# send-email-to-contact

给联系人发纯文本邮件。

## 触发
- "给涛哥发邮件"、"发邮件给老王"
- "发邮件告诉涛哥 <内容>"

## 执行流程
1. 解析收件人邮箱 (knowledge.query_entity)
2. 发送邮件 (send_email)

## 参数
- recipient: 收件人姓名（需在 entities.json 中注册）
- content: 邮件正文

## 约束
- 收件人必须能在 entities.json 中查到邮箱，查不到就报错并说明
- 本技能只发纯文本；带附件请用 send-file-to-contact
