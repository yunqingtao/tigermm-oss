---
name: video-frames
version: "1.0"
description: "视频抽帧：从视频里均匀抽 6 张画面存 jpg，用于审片/封面挑选/素材截图。给出视频路径时使用。"
permission: file_read
timeout: 300
tags: [视频抽帧, 抽帧, 关键帧, 截图, 封面, 审片]
triggers: [视频抽帧, 抽几帧, 导出关键帧, 视频取帧, 视频截图帧, 从视频里抽帧, 抽帧出来, 提几张画面]
platforms: [windows, linux]
requires_tools: [shell_exec]
params:
  path:
    type: path
    required: false
    desc: 视频文件路径（mp4/mov/mkv, 会从原话里自动取）
  out:
    type: path
    required: false
    desc: 抽帧输出目录（缺省=视频同目录下 <名字>_frames）
steps:
  - id: grab
    tool: shell_exec
    input:
      command: python -c "import os,subprocess as S,shutil as U;v=r'$params.path';o=r'$params.out';d=o or os.path.join(os.path.dirname(v),os.path.splitext(os.path.basename(v))[0]+'_frames');os.makedirs(d,exist_ok=True);ex=U.which('ffmpeg') or 'c:/ffmpeg/bin/ffmpeg.exe';px=U.which('ffprobe') or 'c:/ffmpeg/bin/ffprobe.exe';pr=S.run([px,'-v','error','-select_streams','v:0','-show_entries','stream=duration','-of','default=nw=1:nk=1',v],capture_output=True,text=True);du=float(pr.stdout.strip() or 0) or 12;fps=max(0.05,6.0/du);r=S.run([ex,'-y','-i',v,'-vf','fps=%.4f'%fps,'-frames:v','6',os.path.join(d,'f_%03d.jpg')],capture_output=True,text=True);fs=sorted(f for f in os.listdir(d) if f.startswith('f_'));print(('OK' if r.returncode==0 else 'FAIL'),'dur=%.1fs'%du,'frames=%d'%len(fs),d.replace(chr(92),'/'))"
---

# video-frames

视频均匀抽 6 帧存 jpg。输出目录缺省 = 视频同目录下 `<名字>_frames/`。
命令内用正斜杠（shell_exec 走 shlex，反斜杠会被吃掉）。
