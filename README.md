# Linux 网页版文件共享与收集系统（含「一起看课件」）

一个零依赖的 Python 程序（仅用标准库），把当前目录下的文件夹搬到浏览器里：

| 目录 / 文件 | 作用                                              | 谁可以操作 |
| ----------- | ------------------------------------------------- | ---------- |
| `share/`    | 共享区：老师放文件，学生浏览/下载                | 只读      |
| `submit/`  | 收集区：学生网页上传的作业自动落盘              | 可上传/删除 |
| `ppt.pdf`   | 同步课件：老师控制页码，全班学生看同一页        | 只读      |

## 快速开始

```bash
cd linux_teacher
pip install -r requirements.txt      # 可选，但强烈建议（见下方「课件渲染」）
./start.sh                           # 或： python3 fileserver.py
```

启动后终端会显示访问地址：

```
本机访问 : http://127.0.0.1:8000
局域网   : http://192.168.x.x:8000
学生端   : http://192.168.x.x:8000/courseware
教师端   : http://192.168.x.x:8000/teacher
```

把「学生端」地址发到班级群（投屏或群发），学生手机/电脑打开即可。

### 常用参数

```bash
python3 fileserver.py --port 9000            # 换端口
python3 fileserver.py --host 127.0.0.1       # 只允许本机访问
python3 fileserver.py --share /data/pub --submit /data/inbox
python3 fileserver.py --teacher-pass 8888    # 给教师控制台加口令
```

| 参数            | 默认值    | 说明                        |
| --------------- | --------- | --------------------------- |
| `--host`        | `0.0.0.0` | 监听地址                    |
| `--port`        | `8000`    | 端口                        |
| `--share`       | `share`   | 共享目录                    |
| `--submit`      | `submit`  | 收集目录                    |
| `--ppt`         | `ppt.pdf` | 同步课件 PDF                |
| `--teacher-pass`| 空        | 教师翻页口令（可选）        |

## 三个网页

### 1. 首页 `/` —— 共享与收集

- **共享文件**：递归列出 `share/` 下所有文件（含子目录），点击即下载；「打包下载」把整个共享区打成 zip。
- **提交文件**：可填姓名/学号，拖拽或点选多个文件一起上传，带进度条；填了姓名会自动建 `submit/姓名/` 子目录。
- **已收集文件**：实时列出 `submit/` 内容，可单个删除（防误传），也可整包下载。

### 2. 学生端 `/courseware` —— 一起看课件

学生打开后是全屏课件视图，**自动跟随老师翻页**（每秒同步一次）：

- 老师翻页后，学生页面约 1 秒内自动跳到同一页；
- 右下角显示当前页码，按 `H` 隐藏/显示；
- 按 `Z` 或点击图片，可在「适应窗口（接近全屏）」与「原始大小」之间切换，原始大小下可滚动查看细节；
- 页码较多时建议学生用电脑或横屏手机。

### 3. 教师端 `/teacher` —— 翻页控制台

- 「上一页 / 下一页」按钮、页码输入框、页码滑块，改动立即推送给全部学生；
- 「自动裁白边（内容更满屏）」开关：去掉课件四周的大片空白，让内容更接近满屏；
- 左侧显示学生端地址，可直接投屏或发群；
- 「重新载入课件」按钮：换了新的 `ppt.pdf` 后点一下即可，无需重启服务；
- 设置了 `--teacher-pass` 时需要口令才能翻页，避免学生误操作。

## 课件渲染与页数识别

**页数识别**采用三级兜底，Linux 上没装任何第三方库也能正确数出页数：

| 优先级 | 方式                     | 说明                                        |
| ------ | ------------------------ | ------------------------------------------- |
| 1      | PyMuPDF (`fitz`)         | 装了 `pymupdf` 就用它，最准                  |
| 2      | pypdf                    | 装了 `pypdf` 就用它，纯 Python 也能读        |
| 3      | 内置扫描器               | 都不装时使用；能识别普通对象、压缩对象流（/ObjStm）、xref 流，PowerPoint / WPS 导出的 PDF 都能数对 |

启动时终端会打印识别结果与来源：

```
 同步课件 : /opt/linux_teacher/ppt.pdf  (共 24 页，识别方式: pypdf)
```

若显示「未能识别」，执行 `pip install -r requirements.txt` 后重启服务即可。

**页面渲染**：

- 装了 `pymupdf`：由服务端把每页渲染成图片推送给学生，显示效果一致、不受浏览器 PDF 插件影响；
- 没装 `pymupdf`：自动退回浏览器内置 PDF 阅读器，用 `#page=N` 定位，翻页同样正常；
- 渲染时默认裁掉课件四周的白边并保持原始比例（最多放大 1.6 倍），内容因此能铺满屏幕。

## 课件准备

把课件 PDF 放到程序根目录并命名为 `ppt.pdf`（或用 `--ppt` 指定其它路径）。
仓库里的 `ppt.pdf` 是 5 页示例课件，正式使用前替换成自己的。

```bash
pip install -r requirements.txt     # pymupdf（服务端渲染）+ pypdf（页数识别）
```

## 后台常驻运行

```bash
./run_bg.sh                 # nohup 后台启动，日志在 server.log
```

或使用 systemd（推荐）：

```ini
# /etc/systemd/system/fileshare.service
[Unit]
Description=File Share & Collect Server
After=network.target

[Service]
WorkingDirectory=/opt/linux_teacher
ExecStart=/usr/bin/python3 /opt/linux_teacher/fileserver.py --host 0.0.0.0 --port 8000 --teacher-pass 8888
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now fileshare
```

## 说明与安全

- 只依赖 Python 3.8+ 标准库；`pymupdf` / `pypdf` 为可选依赖，缺了自动降级。
- 上传文件名已净化（去路径分隔符、去掉 `..`），上传文件只落在 `submit/` 内，无法写出目录；已验证 `../` 穿越攻击会被拒绝。
- 删除操作仅作用于 `submit/` 目录；PDF 支持 Range 请求，大文件也能快速加载。
- 服务本身没有账号体系，适合实验室/局域网内网使用；若要暴露到公网，请在前面加 Nginx + 认证。
- 单次上传上限 2 GB，超出返回 413。

## 文件结构

```
linux_teacher/
├── fileserver.py    # 主程序（服务端 + 三个网页页面）
├── start.sh         # 前台启动脚本
├── run_bg.sh        # 后台启动脚本
├── requirements.txt # 可选依赖 pymupdf / pypdf
├── ppt.pdf          # 同步课件（示例，替换成自己的）
├── share/           # 共享文件放这里
└── submit/          # 上传的文件自动收集到这里
```
