import os

# 获取桌面路径
desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
file_path = os.path.join(desktop_path, "agent_test.txt")

# 写入内容
with open(file_path, "w", encoding="utf-8") as f:
    f.write("你好世界")

print(f"文件已创建: {file_path}")
print(f"文件内容: 你好世界")

# 验证文件内容
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()
    print(f"验证读取内容: {content}")