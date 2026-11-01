# MusicPlayer 🎵

自托管音乐播放器 —— 一个用 Flask + 原生 HTML/JS 构建的单机音乐库，零构建链、低配设备可跑。最初跑在一台玩客云（OneCloud / Armbian）小主机上， anywhere 有 Python 就能跑。

> English README: [README_EN.md](README_EN.md)

## 功能特性

**音乐库**
- 🎲 **加权随机播放**：1~5 星评级决定歌曲被抽中的权重，你喜欢的歌出现得更频繁
- 🌌 **沉浸式播放页**：旋转唱片、LRC 歌词逐行滚动、免打扰模式（无操作自动淡出控制区）、单曲循环、播完自动切歌、底部迷你控制条
- ✏️ **编辑模式**：星级评分 / 重命名 / 删除 / 更换封面 / 编辑歌词
- 🖼️ **自动封面**：从音频文件内嵌标签提取封面（ID3 / FLAC / MP4）
- 📤 **独立上传页**：拖拽批量上传音乐与 .lrc 歌词

**歌词全家桶**
- 🎙️ **语音识别**：调用必剪（Bcut）接口，从歌曲音频直接识别带时间轴的歌词
- 🌐 **网站找词 + AI 校对**：到 lyrics.net.cn 搜索歌词，再用 AI（任何 OpenAI 兼容接口）以识别出的时间轴逐行对齐、纠错，原有作词作曲等元数据行原样保留
- 📝 **手动补词页**：粘贴 LRC 或上传 .lrc 文件，处理自动流程搞不定的歌
- 🧹 **歌词整理**：自动配对命名错位的 .lrc、清理孤儿/重复歌词（内容相同才删）

**网易云音乐集成**（可选）
- 🔐 扫码登录你的网易云账号（凭证只存在服务器本地）
- 📅 每日推荐、推荐歌单浏览
- ▶️ 在线播放：复用本地播放页的全部 UI（唱片 / 歌词 / 自动切歌）
- ⬇️ 一键下载到本地曲库：自动整理 `歌手 - 歌名.mp3` + 同名歌词 + 封面三件套

**其他**
- 💾 **优盘管理**（仅 Linux）：挂载 / 安全弹出优盘、显示剩余容量，带保护机制防止误动其他盘
- 🌙 深色主题、手机端适配

## 环境要求

| 依赖 | 说明 |
|---|---|
| Python | 3.9 及以上（ARM 设备如玩客云的 armv7 也可） |
| pip 包 | 见 [requirements.txt](requirements.txt)，共 5 个 |
| curl | 系统自带（Windows 10+、macOS、Linux 均有），歌词网站抓取用 |
| systemd | 仅 Linux 服务器部署为服务时需要，桌面运行不需要 |

> 本项目**不含任何密钥、密码或个人信息**。网易云登录凭证与 AI 密钥都只保存在你自己机器上的本地文件里（见下方「隐私与安全」）。

## 快速开始（直接在电脑上运行）

```bash
# 1. 获取代码
git clone <本仓库地址>
cd MusicPlayer

# 2. 创建虚拟环境并安装依赖（可选但推荐）
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt

# 3. 放入音乐
#    把 mp3 / flac / m4a 等音频放进项目根目录下的 music/ 文件夹
mkdir music
# 把你的歌拷进 music/ ……

# 4. 启动
python app.py
```

打开浏览器访问 **http://127.0.0.1:5003**，开始使用。

**在电脑上运行时需要注意：**
- 「优盘管理」卡片是 Linux 专属功能，Windows / macOS 上会显示为不可用，不影响其他功能
- 想让局域网其他设备访问：程序默认监听 `0.0.0.0`，用 `http://你的电脑IP:5003` 即可；注意 Windows 防火墙首次会弹放行提示

## 配置项（环境变量）

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `5003` | 监听端口 |
| `MUSIC_DIR` | 项目根下 `music/` | 音乐库目录（放音频文件的地方） |
| `USB_PROTECTED_MOUNTS` | 空 | 逗号分隔的受保护挂载点列表，这些挂载点上的分区永远不会出现在优盘管理里 |

示例：

```bash
# Windows (PowerShell)
$env:PORT = "8080"; $env:MUSIC_DIR = "D:\Music"; python app.py

# Linux / macOS
PORT=8080 MUSIC_DIR=/mnt/music python app.py
```

元数据（评分、封面标记等）保存在项目根的 `music_meta.json`，背景设置在 `settings.json`，均为运行时自动创建。

## 部署到 Linux 服务器（systemd）

```bash
# 1. 上传项目到服务器，例如 /opt/MusicPlayer
# 2. 建虚拟环境、装依赖
cd /opt/MusicPlayer
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 准备音乐目录
mkdir music    # 或指向已有目录，见下方环境变量

# 4. 编辑服务文件：修改 User / WorkingDirectory / ExecStart 三处
vim deploy/music.service

# 5. 安装并启动
cp deploy/music.service /etc/systemd/system/music.service
systemctl daemon-reload
systemctl enable --now music.service

# 6. 查看状态 / 日志
systemctl status music.service
journalctl -u music.service -f
```

如需反向代理（Nginx）对外提供访问，把 `proxy_pass http://127.0.0.1:5003;` 挂到你现有的站点即可，无特殊头要求。

## 部署到玩客云 / Armbian 等低配 ARM 设备

本项目最初就是为玩客云（armv7, 512MB 内存）写的，注意事项：

1. 全部依赖是纯 Python 或带 ARM 预编译轮子的包，`pip install -r requirements.txt` 可直接装完；国内网络建议加镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
2. 音乐放优盘时，建议在电脑上格成 **exFAT**（内核 5.4+ 原生支持）或 ext4
3. 优盘挂载：`MUSIC_DIR=/mnt/music` 指向挂载点；也可以直接用「优盘管理」卡片在网页上一键挂载（服务需以 root 运行，或给 systemd 服务加 `SupplementaryGroups=disk` 等权限）
4. 512MB 内存设备建议关闭 swap 依赖大的其他服务，本项目自身内存占用约 40~60MB

## 可选功能配置

### AI 歌词校对（可选）

「网站找词 + AI 校对补词」需要一个 OpenAI 兼容接口（DeepSeek、GLM、通义等均可）：

```bash
cp ai_config.example.json ai_config.json
# 编辑 ai_config.json，填入你自己的 base_url / api_key / model
```

不配置此文件时，其余功能完全不受影响；手动补词、语音识别照常可用。

### 网易云音乐

打开主页右上角「网易云」→ 用网易云音乐 APP 扫码登录。登录凭证保存在项目根的 `netease_session.json`（自动生成，注意不要提交或分享）。每日推荐需要登录后才有；在线播放和下载使用你账号本身有权播放的内容。

> ⚠️ 网易云集成仅供个人在局域网内收听自己账号有权限的内容，请勿用于公开服务或商业用途，请遵守网易云音乐的用户协议。

### 中文字体（可选）

播放页标题使用两套字体：英文用 Great Vibes（SIL OFL 1.1 开源授权，已随项目分发于 `static/fonts/`）；中文标题字体为商业授权字体，**不随本项目分发**。如需同款效果，请自行获取授权字体并命名为 `static/fonts/chuangcuhei.ttf` 放入；缺失时浏览器自动回退系统字体，功能不受影响。

## 目录结构

```
MusicPlayer/
├── app.py              # Flask 主程序：页面路由 + 全部 API
├── musiclib.py         # 音乐库扫描 / 元数据
├── storage.py          # JSON 原子写入存储
├── asr.py              # 必剪(Bcut)语音识别：音频 → 带时间轴歌词
├── lyr.py              # lyrics.net.cn 找词（curl 子进程）
├── aifix.py            # AI 歌词对齐校对（OpenAI 兼容接口）
├── covers.py           # 内嵌封面提取 (mutagen)
├── netease.py          # 网易云接口封装（weapi 加密 / 扫码登录 / 曲目 URL）
├── usbops.py           # 优盘挂载/弹出（仅 Linux）
├── templates/          # 页面模板（主页 / 上传页 / 歌词工坊）
├── static/             # 样式、脚本、默认背景、开源字体
├── deploy/music.service  # systemd 服务模板
└── ai_config.example.json
```

## 隐私与安全

- 仓库内**没有**任何 API 密钥、密码、个人信息或音乐文件
- `netease_session.json`（你的网易云登录凭证）、`ai_config.json`（你的 AI 密钥）已在 [.gitignore](.gitignore) 中排除，永远不会被提交
- 项目无认证功能：请只在你信任的局域网内使用；如需暴露公网，请自行在前面加一层带认证的反向代理（如 Nginx + Basic Auth、OAuth2 Proxy 等）

## 致谢

- [NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi) —— 网易云 weapi 加密与扫码登录的思路参考
- 必剪（Bcut）ASR 接口 —— 歌词语音识别
- [lyrics.net.cn](https://lyrics.net.cn/) —— 歌词来源站点
- [Great Vibes](https://fonts.google.com/specimen/Great+Vibes) —— SIL OFL 1.1 授权字体

## 许可证

[MIT](LICENSE)
